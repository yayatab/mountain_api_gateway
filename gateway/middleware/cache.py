import asyncio
import base64
import hashlib
import json
import logging
import time
from typing import Any, cast
from urllib.parse import urlencode

import redis.asyncio as aioredis
from fastapi import Request, Response
from fastapi.responses import Response as RawResponse
from fastapi.responses import StreamingResponse
from starlette.datastructures import Headers

from gateway.middleware.base import BaseMiddleware, CallNext
from gateway.models.context import GatewayContext
from gateway.models.route import CacheConfig

logger = logging.getLogger("gateway.middleware.cache")


class CacheMiddleware(BaseMiddleware):
    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        redis_url: str | None = None,
    ) -> None:
        self._redis_client = redis_client
        if not self._redis_client and redis_url:
            try:
                self._redis_client = aioredis.from_url(redis_url, socket_timeout=0.5)
            except Exception as exc:
                logger.warning(
                    "Failed to create Redis client for cache at %s (%s); will use memory",
                    redis_url,
                    exc,
                )
        self._memory_cache: dict[str, tuple[float, bytes, int, dict[str, str]]] = {}
        self._memory_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._redis_client:
            await self._redis_client.aclose()

    @staticmethod
    def generate_cache_key(
        route_id: str,
        method: str,
        path: str,
        query_items: list[tuple[str, str]],
        headers: Headers,
        config: CacheConfig,
    ) -> str:
        # Sort query params deterministically
        sorted_query = urlencode(sorted(query_items))

        # Build vary signature
        vary_parts: list[str] = []
        for vh in config.vary_headers:
            val = headers.get(vh.lower(), "")
            vary_parts.append(f"{vh.lower()}={val.strip()}")
        vary_str = ";".join(sorted(vary_parts))

        normalized_path = "/" + path.strip("/") if path.strip("/") else "/"
        raw_key = f"{method.upper()}|{normalized_path}|{sorted_query}|{vary_str}"
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return f"{config.key_prefix}:{route_id}:{digest}"

    async def _get_from_cache(self, key: str) -> dict[str, Any] | None:
        # Check Redis first if available
        if self._redis_client:
            try:
                cached_data = await asyncio.wait_for(self._redis_client.get(key), timeout=0.3)
                if cached_data:
                    return cast(dict[str, Any], json.loads(cached_data))
            except Exception as exc:
                logger.warning("Redis cache get error for key '%s': %s", key, exc)

        # Fallback to in-memory cache
        async with self._memory_lock:
            record = self._memory_cache.get(key)
            if record:
                expire_at, body, status_code, headers = record
                if time.time() < expire_at:
                    return {
                        "status_code": status_code,
                        "headers": headers,
                        "body_b64": base64.b64encode(body).decode("ascii"),
                        "created_at": expire_at - 60,  # approximate
                        "ttl": int(expire_at - time.time()),
                    }
                del self._memory_cache[key]
        return None

    async def _save_to_cache(
        self,
        key: str,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
        ttl_seconds: int,
    ) -> None:
        payload = {
            "status_code": status_code,
            "headers": headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "created_at": time.time(),
            "ttl": ttl_seconds,
        }
        json_data = json.dumps(payload)

        # Save to Redis
        if self._redis_client:
            try:
                await asyncio.wait_for(
                    self._redis_client.set(key, json_data, ex=ttl_seconds),
                    timeout=0.3,
                )
                return
            except Exception as exc:
                logger.warning("Redis cache set error for key '%s': %s", key, exc)

        # Fallback to in-memory cache
        async with self._memory_lock:
            expire_at = time.time() + ttl_seconds
            self._memory_cache[key] = (expire_at, body, status_code, headers)

    async def __call__(
        self,
        request: Request,
        context: GatewayContext,
        call_next: CallNext,
    ) -> Response:
        route = context.matched_route
        if route is None or not route.middleware.cache.enabled:
            return await call_next(request, context)

        cache_config = route.middleware.cache
        method_upper = request.method.upper()

        # Check method eligibility
        if method_upper not in cache_config.methods:
            response = await call_next(request, context)
            response.headers["X-Cache"] = "BYPASS"
            return response

        # Check client Cache-Control headers
        client_cc = request.headers.get("cache-control", "").lower()
        if cache_config.respect_cache_control and (
            "no-cache" in client_cc or "no-store" in client_cc
        ):
            response = await call_next(request, context)
            response.headers["X-Cache"] = "BYPASS"
            return response

        cache_key = self.generate_cache_key(
            route_id=route.id,
            method=method_upper,
            path=request.url.path,
            query_items=list(request.query_params.multi_items()),
            headers=request.headers,
            config=cache_config,
        )

        try:
            cached = await self._get_from_cache(cache_key)
        except Exception as exc:
            logger.error(
                "Unexpected error retrieving cache [%s]: %s",
                context.request_id,
                exc,
                exc_info=True,
            )
            cached = None

        if cached is not None:
            # Cache HIT
            body_bytes = base64.b64decode(str(cached["body_b64"]))
            raw_headers = cached.get("headers")
            resp_headers: dict[str, str] = (
                {str(k): str(v) for k, v in raw_headers.items()}
                if isinstance(raw_headers, dict)
                else {}
            )
            created_at_val = cached.get("created_at")
            created_at = (
                float(created_at_val)
                if isinstance(created_at_val, (int, float, str))
                else time.time()
            )
            age = max(0, int(time.time() - created_at))

            resp_headers["X-Cache"] = "HIT"
            resp_headers["Age"] = str(age)
            if "X-Request-ID" not in resp_headers:
                resp_headers["X-Request-ID"] = context.request_id

            status_val = cached.get("status_code", 200)
            status_code = int(status_val) if isinstance(status_val, (int, str)) else 200

            return RawResponse(
                content=body_bytes,
                status_code=status_code,
                headers=resp_headers,
            )

        # Cache MISS: delegate upstream
        response = await call_next(request, context)

        # Check if upstream response is cacheable
        if response.status_code not in cache_config.status_codes:
            response.headers["X-Cache"] = "MISS"
            return response

        upstream_cc = response.headers.get("cache-control", "").lower()
        if cache_config.respect_cache_control and (
            "no-store" in upstream_cc or "private" in upstream_cc
        ):
            response.headers["X-Cache"] = "BYPASS"
            return response

        # Determine TTL (respecting max-age if present and configured)
        ttl = cache_config.ttl_seconds
        if cache_config.respect_cache_control and "max-age=" in upstream_cc:
            try:
                for part in upstream_cc.split(","):
                    part = part.strip()
                    if part.startswith("max-age="):
                        ttl = max(1, int(part.split("=")[1]))
                        break
            except (ValueError, IndexError):
                ttl = cache_config.ttl_seconds

        # Clean cacheable headers
        cacheable_headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"content-length", "connection", "transfer-encoding", "set-cookie"}
        }

        # Handle StreamingResponse safely
        if isinstance(response, StreamingResponse):
            body_chunks: list[bytes] = []
            exceeded = False
            max_bytes = cache_config.max_payload_bytes
            original_body_iterator = response.body_iterator

            async def caching_chunk_generator():
                nonlocal exceeded
                try:
                    async for chunk in original_body_iterator:
                        if not exceeded:
                            chunk_bytes = (
                                chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                            )
                            body_chunks.append(chunk_bytes)
                            if sum(len(c) for c in body_chunks) > max_bytes:
                                exceeded = True
                                body_chunks.clear()
                        yield chunk
                finally:
                    if not exceeded and body_chunks:
                        full_body = b"".join(body_chunks)
                        try:
                            await self._save_to_cache(
                                key=cache_key,
                                status_code=response.status_code,
                                headers=cacheable_headers,
                                body=full_body,
                                ttl_seconds=ttl,
                            )
                        except Exception as exc:
                            logger.error(
                                "Unexpected error caching streaming response [%s]: %s",
                                context.request_id,
                                exc,
                                exc_info=True,
                            )

            response.body_iterator = caching_chunk_generator()
            response.headers["X-Cache"] = "MISS"
            return response

        # Regular buffered response
        body_content = getattr(response, "body", b"")
        if len(body_content) <= cache_config.max_payload_bytes:
            try:
                await self._save_to_cache(
                    key=cache_key,
                    status_code=response.status_code,
                    headers=cacheable_headers,
                    body=body_content,
                    ttl_seconds=ttl,
                )
            except Exception as exc:
                logger.error(
                    "Unexpected error caching response [%s]: %s",
                    context.request_id,
                    exc,
                    exc_info=True,
                )

        response.headers["X-Cache"] = "MISS"
        return response
