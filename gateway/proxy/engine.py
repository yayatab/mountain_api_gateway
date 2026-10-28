import logging
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import Headers

from gateway.models.context import GatewayContext
from gateway.proxy.client import client_pool
from gateway.proxy.headers import (
    prepare_client_response_headers,
    prepare_upstream_headers,
)

logger = logging.getLogger("gateway.proxy")


class ProxyEngine:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        on_result: Callable[[str, bool], None] | None = None,
    ) -> None:
        self._custom_client = client
        self._on_result = on_result

    def _get_client(self) -> httpx.AsyncClient:
        if self._custom_client is not None:
            return self._custom_client
        return client_pool.get_client()

    def build_upstream_url(self, base_url: str, target_path: str, query_string: str) -> str:
        parsed_base = urlparse(base_url)
        # Ensure base path and target path combine properly
        base_path = parsed_base.path.rstrip("/")
        normalized_target_path = "/" + target_path.lstrip("/")

        full_path = f"{base_path}{normalized_target_path}" if base_path else normalized_target_path

        # Rebuild origin with new path
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        url = urljoin(origin, full_path)
        if query_string:
            url = f"{url}?{query_string}"
        return url

    async def forward(
        self,
        request: Request,
        context: GatewayContext,
        target_base_url: str,
        target_path: str,
        timeout_seconds: float,
    ) -> Response:
        client = self._get_client()
        query_string = request.url.query
        full_upstream_url = self.build_upstream_url(target_base_url, target_path, query_string)

        current_headers = Headers(scope=request.scope)
        client_host = current_headers.get("host")
        client_ip = context.client_ip or (request.client.host if request.client else "127.0.0.1")
        scheme = request.url.scheme or "http"

        upstream_headers = prepare_upstream_headers(
            incoming_headers=current_headers,
            client_host=client_host,
            client_ip=client_ip,
            target_url=full_upstream_url,
            request_id=context.request_id,
            scheme=scheme,
            user_id=context.user_id,
            user_roles=context.user_roles,
        )

        has_body = (
            "content-length" in current_headers
            or "transfer-encoding" in current_headers
            or request.method not in ("GET", "HEAD", "OPTIONS")
        )
        content_stream = request.stream() if has_body else None

        try:
            upstream_req = client.build_request(
                method=request.method,
                url=full_upstream_url,
                headers=upstream_headers,
                content=content_stream,
                timeout=httpx.Timeout(timeout_seconds),
            )
            upstream_resp = await client.send(upstream_req, stream=True)
            if self._on_result:
                self._on_result(target_base_url, upstream_resp.status_code < 500)
        except httpx.TimeoutException as exc:
            logger.warning(
                "Upstream timeout [%s] for %s after %ss: %s",
                context.request_id,
                target_base_url,
                timeout_seconds,
                exc,
            )
            if self._on_result:
                self._on_result(target_base_url, False)
            return JSONResponse(
                status_code=504,
                content={
                    "error": "gateway_timeout",
                    "message": f"Upstream backend did not respond within {timeout_seconds:.1f}s",
                    "request_id": context.request_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            logger.warning(
                "Upstream connection failure [%s] for %s: %s",
                context.request_id,
                target_base_url,
                exc,
            )
            if self._on_result:
                self._on_result(target_base_url, False)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "bad_gateway",
                    "message": "Failed to connect to upstream backend",
                    "request_id": context.request_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as exc:
            logger.error(
                "Unexpected proxy error [%s] forwarding to %s: %s",
                context.request_id,
                target_base_url,
                exc,
                exc_info=True,
            )
            if self._on_result:
                self._on_result(target_base_url, False)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "bad_gateway",
                    "message": "Unexpected proxy error connecting to upstream backend",
                    "request_id": context.request_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

        client_headers = prepare_client_response_headers(
            upstream_resp.headers,
            is_streaming=True,
        )
        # Always propagate X-Request-ID back to client
        client_headers["X-Request-ID"] = context.request_id

        async def stream_generator():
            try:
                async for chunk in upstream_resp.aiter_raw():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            content=stream_generator(),
            status_code=upstream_resp.status_code,
            headers=client_headers,
        )
