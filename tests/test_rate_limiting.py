import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.config import settings
from gateway.main import create_app
from gateway.middleware.rate_limit import (
    InMemoryRateLimitEngine,
    RateLimitMiddleware,
    RedisRateLimitEngine,
)
from gateway.models.context import GatewayContext
from gateway.models.route import (
    AuthMiddlewareConfig,
    HTTPMethod,
    MiddlewareConfig,
    RateLimitConfig,
    RouteDefinition,
)
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry
from mock_backend_services.echo_service import app as echo_app


def create_token(user_id: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


@pytest.fixture
def rate_limit_registry() -> RouteRegistry:
    return RouteRegistry(
        routes=[
            RouteDefinition(
                id="ip-limited-route",
                name="IP Limited",
                path_pattern="/limited/ip",
                methods={HTTPMethod.GET},
                upstream_url="http://echo-service",
                middleware=MiddlewareConfig(
                    rate_limit=RateLimitConfig(
                        enabled=True,
                        requests_per_minute=3,
                        key_by="ip",
                    )
                ),
            ),
            RouteDefinition(
                id="user-limited-route",
                name="User Limited",
                path_pattern="/limited/user",
                methods={HTTPMethod.GET},
                upstream_url="http://echo-service",
                middleware=MiddlewareConfig(
                    auth=AuthMiddlewareConfig(
                        enabled=True,
                        required=True,
                    ),
                    rate_limit=RateLimitConfig(
                        enabled=True,
                        requests_per_minute=2,
                        key_by="user",
                    ),
                ),
            ),
            RouteDefinition(
                id="apikey-limited-route",
                name="API Key Limited",
                path_pattern="/limited/key",
                methods={HTTPMethod.GET},
                upstream_url="http://echo-service",
                middleware=MiddlewareConfig(
                    rate_limit=RateLimitConfig(
                        enabled=True,
                        requests_per_minute=2,
                        key_by="api_key",
                    )
                ),
            ),
        ]
    )


@pytest.fixture
def rate_limit_client(rate_limit_registry: RouteRegistry) -> httpx.AsyncClient:
    mock_echo_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=echo_app),
        base_url="http://echo-service",
    )
    engine = ProxyEngine(client=mock_echo_client)
    app = create_app(registry=rate_limit_registry, proxy_engine=engine)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    )


@pytest.mark.asyncio
async def test_sliding_window_ip_rate_limiting(rate_limit_client: httpx.AsyncClient):
    # Request 1 (remaining: 2)
    r1 = await rate_limit_client.get("/limited/ip")
    assert r1.status_code == 200
    assert r1.headers["X-RateLimit-Limit"] == "3"
    assert r1.headers["X-RateLimit-Remaining"] == "2"
    assert "X-RateLimit-Reset" in r1.headers

    # Request 2 (remaining: 1)
    r2 = await rate_limit_client.get("/limited/ip")
    assert r2.status_code == 200
    assert r2.headers["X-RateLimit-Remaining"] == "1"

    # Request 3 (remaining: 0)
    r3 = await rate_limit_client.get("/limited/ip")
    assert r3.status_code == 200
    assert r3.headers["X-RateLimit-Remaining"] == "0"

    # Request 4 -> 429 Too Many Requests
    r4 = await rate_limit_client.get("/limited/ip")
    assert r4.status_code == 429
    assert r4.headers["X-RateLimit-Remaining"] == "0"
    assert "Retry-After" in r4.headers
    retry_after = int(r4.headers["Retry-After"])
    assert retry_after >= 1

    data = r4.json()
    assert data["error"] == "rate_limit_exceeded"
    assert "Rate limit exceeded" in data["message"]


@pytest.mark.asyncio
async def test_user_based_rate_limiting_isolation(rate_limit_client: httpx.AsyncClient):
    token_alice = create_token("alice")
    token_bob = create_token("bob")

    # Alice sends 2 requests (reaches limit)
    a1 = await rate_limit_client.get(
        "/limited/user",
        headers={"Authorization": f"Bearer {token_alice}"},
    )
    assert a1.status_code == 200
    a2 = await rate_limit_client.get(
        "/limited/user",
        headers={"Authorization": f"Bearer {token_alice}"},
    )
    assert a2.status_code == 200

    # Alice 3rd request fails with 429
    a3 = await rate_limit_client.get(
        "/limited/user",
        headers={"Authorization": f"Bearer {token_alice}"},
    )
    assert a3.status_code == 429

    # Bob sends request -> Bob is not limited (quota is per-user)
    b1 = await rate_limit_client.get(
        "/limited/user",
        headers={"Authorization": f"Bearer {token_bob}"},
    )
    assert b1.status_code == 200


@pytest.mark.asyncio
async def test_api_key_based_rate_limiting(rate_limit_client: httpx.AsyncClient):
    # Key A sends 2 requests
    k1 = await rate_limit_client.get("/limited/key", headers={"X-API-Key": "key-alpha"})
    assert k1.status_code == 200
    k2 = await rate_limit_client.get("/limited/key", headers={"X-API-Key": "key-alpha"})
    assert k2.status_code == 200

    # Key A 3rd request -> 429
    k3 = await rate_limit_client.get("/limited/key", headers={"X-API-Key": "key-alpha"})
    assert k3.status_code == 429

    # Key B has independent quota
    kb1 = await rate_limit_client.get("/limited/key", headers={"X-API-Key": "key-beta"})
    assert kb1.status_code == 200


@pytest.mark.asyncio
async def test_in_memory_engine_direct():
    engine = InMemoryRateLimitEngine()
    key = "test:client:1"

    # Check 2 allowed in limit of 2
    res1 = await engine.check_and_record(key, limit=2, window_seconds=60)
    assert res1.allowed is True
    assert res1.remaining == 1

    res2 = await engine.check_and_record(key, limit=2, window_seconds=60)
    assert res2.allowed is True
    assert res2.remaining == 0

    # 3rd should be rejected
    res3 = await engine.check_and_record(key, limit=2, window_seconds=60)
    assert res3.allowed is False
    assert res3.remaining == 0
    assert res3.retry_after >= 1

    # Reset
    await engine.reset()
    res4 = await engine.check_and_record(key, limit=2, window_seconds=60)
    assert res4.allowed is True
    assert res4.remaining == 1


class MockRedisPipeline:
    def __init__(self, execute_return_value):
        self.execute_return_value = execute_return_value
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def zremrangebyscore(self, *args, **kwargs):
        self.calls.append(("zremrangebyscore", args, kwargs))

    def zcard(self, *args, **kwargs):
        self.calls.append(("zcard", args, kwargs))

    def zrange(self, *args, **kwargs):
        self.calls.append(("zrange", args, kwargs))

    def zadd(self, *args, **kwargs):
        self.calls.append(("zadd", args, kwargs))

    def expire(self, *args, **kwargs):
        self.calls.append(("expire", args, kwargs))

    async def execute(self):
        return self.execute_return_value


@pytest.mark.asyncio
async def test_redis_rate_limit_engine_under_limit():
    mock_redis = MagicMock()
    pipe = MockRedisPipeline(execute_return_value=[None, 1, [(b"user1", time.time() - 10)]])
    mock_redis.pipeline.return_value = pipe

    engine = RedisRateLimitEngine(redis_client=mock_redis)
    result = await engine.check_and_record("test:key", limit=5, window_seconds=60)

    assert result.allowed is True
    assert result.limit == 5
    assert result.remaining == 3
    assert result.retry_after == 0


@pytest.mark.asyncio
async def test_redis_rate_limit_engine_over_limit_with_oldest():
    now = time.time()
    mock_redis = MagicMock()
    pipe = MockRedisPipeline(execute_return_value=[None, 5, [(b"oldest_req", now - 20)]])
    mock_redis.pipeline.return_value = pipe

    engine = RedisRateLimitEngine(redis_client=mock_redis)
    result = await engine.check_and_record("test:key", limit=5, window_seconds=60)

    assert result.allowed is False
    assert result.limit == 5
    assert result.remaining == 0
    assert result.retry_after >= 39


@pytest.mark.asyncio
async def test_redis_rate_limit_engine_over_limit_without_oldest():
    mock_redis = MagicMock()
    pipe = MockRedisPipeline(execute_return_value=[None, 5, []])
    mock_redis.pipeline.return_value = pipe

    engine = RedisRateLimitEngine(redis_client=mock_redis)
    result = await engine.check_and_record("test:key", limit=5, window_seconds=60)

    assert result.allowed is False
    assert result.retry_after == 1


@pytest.mark.asyncio
async def test_rate_limit_middleware_redis_fallback():
    mock_redis = MagicMock()
    # Configure pipeline to raise connection error
    mock_pipe = MagicMock()
    mock_pipe.__aenter__.side_effect = ConnectionError("Redis unreachable")
    mock_redis.pipeline.return_value = mock_pipe

    memory_engine = InMemoryRateLimitEngine()
    middleware = RateLimitMiddleware(redis_client=mock_redis, memory_engine=memory_engine)

    route = RouteDefinition(
        id="fallback-route",
        name="Fallback Route",
        path_pattern="/test",
        methods={HTTPMethod.GET},
        upstream_url="http://echo",
        middleware=MiddlewareConfig(
            rate_limit=RateLimitConfig(enabled=True, requests_per_minute=2)
        ),
    )
    context = GatewayContext(matched_route=route, client_ip="1.2.3.4")

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [(b"host", b"localhost")],
    }
    request = Request(scope)

    async def call_next(req, ctx):
        return JSONResponse({"status": "ok"})

    # First call succeeds via in-memory fallback
    r1 = await middleware(request, context, call_next)
    assert r1.status_code == 200

    # Second call succeeds
    r2 = await middleware(request, context, call_next)
    assert r2.status_code == 200

    # Third call is rate limited by in-memory fallback
    r3 = await middleware(request, context, call_next)
    assert r3.status_code == 429
