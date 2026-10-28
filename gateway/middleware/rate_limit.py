import asyncio
import logging
import math
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime

import redis.asyncio as aioredis
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gateway.middleware.base import BaseMiddleware, CallNext
from gateway.models.context import GatewayContext

logger = logging.getLogger("gateway.middleware.rate_limit")


class RateLimitResult(BaseModel):
    allowed: bool
    limit: int
    remaining: int
    reset_epoch: int
    retry_after: int


class InMemoryRateLimitEngine:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    async def check_and_record(
        self,
        key: str,
        limit: int,
        window_seconds: int = 60,
    ) -> RateLimitResult:
        async with self._lock:
            now = time.time()
            cutoff = now - window_seconds
            bucket = self._buckets[key]

            # Evict timestamps outside the sliding window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            count = len(bucket)
            reset_epoch = int(now + window_seconds)

            if count < limit:
                bucket.append(now)
                remaining = limit - count - 1
                return RateLimitResult(
                    allowed=True,
                    limit=limit,
                    remaining=remaining,
                    reset_epoch=reset_epoch,
                    retry_after=0,
                )

            # Limit exceeded
            oldest = bucket[0] if bucket else now
            retry_after = max(1, math.ceil(oldest + window_seconds - now))
            return RateLimitResult(
                allowed=False,
                limit=limit,
                remaining=0,
                reset_epoch=int(oldest + window_seconds),
                retry_after=retry_after,
            )

    async def reset(self) -> None:
        async with self._lock:
            self._buckets.clear()


class RedisRateLimitEngine:
    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._redis = redis_client

    async def check_and_record(
        self,
        key: str,
        limit: int,
        window_seconds: int = 60,
    ) -> RateLimitResult:
        now = time.time()
        cutoff = now - window_seconds
        member = f"{now}:{uuid.uuid4().hex[:8]}"
        reset_epoch = int(now + window_seconds)

        # Sliding window using ZSET inside an atomic pipeline
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zcard(key)
            pipe.zrange(key, 0, 0, withscores=True)
            results = await pipe.execute()

        current_count: int = results[1]
        oldest_records: list[tuple[bytes, float]] = results[2]

        if current_count < limit:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.zadd(key, {member: now})
                pipe.expire(key, window_seconds * 2)
                await pipe.execute()

            remaining = limit - current_count - 1
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=remaining,
                reset_epoch=reset_epoch,
                retry_after=0,
            )

        # Exceeded limit
        if oldest_records:
            oldest_ts = oldest_records[0][1]
            retry_after = max(1, math.ceil(oldest_ts + window_seconds - now))
            reset_epoch = int(oldest_ts + window_seconds)
        else:
            retry_after = 1

        return RateLimitResult(
            allowed=False,
            limit=limit,
            remaining=0,
            reset_epoch=reset_epoch,
            retry_after=retry_after,
        )


class RateLimitMiddleware(BaseMiddleware):
    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        redis_url: str | None = None,
        memory_engine: InMemoryRateLimitEngine | None = None,
    ) -> None:
        self._memory_engine = memory_engine or InMemoryRateLimitEngine()
        self._redis_client = redis_client
        if not self._redis_client and redis_url:
            try:
                self._redis_client = aioredis.from_url(redis_url, socket_timeout=0.5)
            except Exception as exc:
                logger.warning(
                    "Failed to create Redis client for %s (%s); rate limiting will use memory",
                    redis_url,
                    exc,
                )
        self._redis_engine: RedisRateLimitEngine | None = None
        if self._redis_client:
            self._redis_engine = RedisRateLimitEngine(self._redis_client)

    async def close(self) -> None:
        if self._redis_client:
            await self._redis_client.aclose()

    def _resolve_identity_key(self, request: Request, context: GatewayContext, key_by: str) -> str:
        if key_by == "user" and context.user_id:
            return f"user:{context.user_id}"
        if key_by == "api_key":
            api_key = request.headers.get("x-api-key")
            if api_key:
                return f"apikey:{api_key.strip()}"
        return f"ip:{context.client_ip}"

    async def _evaluate_quota(
        self,
        cache_key: str,
        limit: int,
        window_seconds: int = 60,
    ) -> RateLimitResult:
        if self._redis_engine:
            try:
                return await asyncio.wait_for(
                    self._redis_engine.check_and_record(cache_key, limit, window_seconds),
                    timeout=0.2,
                )
            except Exception as exc:
                # Redis unavailable or timed out; fall back seamlessly to memory engine
                logger.warning(
                    "Redis rate limit check failed (%s); falling back to memory engine", exc
                )
        return await self._memory_engine.check_and_record(cache_key, limit, window_seconds)

    async def __call__(
        self,
        request: Request,
        context: GatewayContext,
        call_next: CallNext,
    ) -> Response:
        route = context.matched_route
        if route is None or not route.middleware.rate_limit.enabled:
            return await call_next(request, context)

        rl_config = route.middleware.rate_limit
        identity_key = self._resolve_identity_key(request, context, rl_config.key_by)
        cache_key = f"rl:{route.id}:{identity_key}"
        limit = rl_config.requests_per_minute

        result = await self._evaluate_quota(cache_key, limit, window_seconds=60)

        rate_limit_headers = {
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Remaining": str(result.remaining),
            "X-RateLimit-Reset": str(result.reset_epoch),
        }

        if not result.allowed:
            return JSONResponse(
                status_code=429,
                headers={
                    **rate_limit_headers,
                    "Retry-After": str(result.retry_after),
                    "X-Request-ID": context.request_id,
                },
                content={
                    "error": "rate_limit_exceeded",
                    "message": f"Rate limit exceeded. Try again in {result.retry_after} seconds",
                    "request_id": context.request_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

        response = await call_next(request, context)

        # Append rate limit headers to successful response
        for k, v in rate_limit_headers.items():
            response.headers[k] = v

        return response
