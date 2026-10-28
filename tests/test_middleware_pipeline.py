import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.main import create_app
from gateway.middleware.base import BaseMiddleware, MiddlewarePipeline
from gateway.models.context import GatewayContext
from gateway.models.route import (
    HTTPMethod,
    MiddlewareConfig,
    RateLimitConfig,
    RouteDefinition,
    TransformConfig,
)
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry
from mock_backend_services.echo_service import app as echo_app


@pytest.fixture
def pipeline_registry() -> RouteRegistry:
    return RouteRegistry(
        routes=[
            RouteDefinition(
                id="transform-route",
                name="Transform Service",
                path_pattern="/api/transforms",
                methods={HTTPMethod.GET, HTTPMethod.POST},
                upstream_url="http://echo-service",
                middleware=MiddlewareConfig(
                    rate_limit=RateLimitConfig(
                        enabled=True,
                        requests_per_minute=100,
                    ),
                    transform=TransformConfig(
                        add_request_headers={"x-gateway-injected": "true", "x-env": "prod"},
                        remove_request_headers={"x-sensitive-client-token"},
                        add_response_headers={
                            "x-custom-gateway-version": "1.0.0",
                            "access-control-allow-origin": "*",
                        },
                    ),
                ),
            ),
        ]
    )


@pytest.fixture
def pipeline_client(pipeline_registry: RouteRegistry) -> httpx.AsyncClient:
    mock_echo_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=echo_app),
        base_url="http://echo-service",
    )
    engine = ProxyEngine(client=mock_echo_client)
    app = create_app(registry=pipeline_registry, proxy_engine=engine)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    )


@pytest.mark.asyncio
async def test_timing_and_trace_headers(pipeline_client: httpx.AsyncClient):
    resp = await pipeline_client.get("/api/transforms")
    assert resp.status_code == 200

    # Timing headers
    assert "X-Response-Time" in resp.headers
    assert resp.headers["X-Response-Time"].endswith("ms")
    assert "Server-Timing" in resp.headers
    assert "gateway;dur=" in resp.headers["Server-Timing"]

    # Trace ID
    assert "X-Request-ID" in resp.headers


@pytest.mark.asyncio
async def test_request_and_response_header_transforms(pipeline_client: httpx.AsyncClient):
    client_headers = {
        "X-Sensitive-Client-Token": "secret-12345",
        "X-Normal-Header": "regular-data",
    }
    resp = await pipeline_client.get("/api/transforms", headers=client_headers)
    assert resp.status_code == 200

    echoed = resp.json()
    upstream_received_headers = echoed["headers"]

    # Request transform: add_request_headers injected upstream
    assert upstream_received_headers.get("x-gateway-injected") == "true"
    assert upstream_received_headers.get("x-env") == "prod"

    # Request transform: remove_request_headers stripped from upstream
    assert "x-sensitive-client-token" not in upstream_received_headers
    assert upstream_received_headers.get("x-normal-header") == "regular-data"

    # Response transform: add_response_headers present on client response
    assert resp.headers["x-custom-gateway-version"] == "1.0.0"
    assert resp.headers["access-control-allow-origin"] == "*"


@pytest.mark.asyncio
async def test_prometheus_metrics_endpoint(pipeline_client: httpx.AsyncClient):
    # Send a couple requests to generate metrics
    await pipeline_client.get("/api/transforms")
    await pipeline_client.get("/api/transforms")

    # Scrape metrics
    metrics_resp = await pipeline_client.get("/metrics")
    assert metrics_resp.status_code == 200
    metrics_text = metrics_resp.text

    assert "gateway_requests_total" in metrics_text
    assert "gateway_request_duration_seconds" in metrics_text


class CustomDummyMiddleware(BaseMiddleware):
    async def __call__(self, request, context, call_next):
        resp = await call_next(request, context)
        resp.headers["X-Custom-Added"] = "true"
        return resp


class FailingMiddleware(BaseMiddleware):
    async def __call__(self, request, context, call_next):
        raise RuntimeError("Unexpected pipeline crash")


@pytest.mark.asyncio
async def test_middleware_pipeline_add_middleware():
    pipeline = MiddlewarePipeline()
    pipeline.add_middleware(CustomDummyMiddleware())

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [(b"host", b"localhost")],
    }
    req = Request(scope)
    ctx = GatewayContext()

    async def terminal(r, c):
        return JSONResponse({"ok": True})

    res = await pipeline.execute(req, ctx, terminal)
    assert res.status_code == 200
    assert res.headers["X-Custom-Added"] == "true"


@pytest.mark.asyncio
async def test_middleware_pipeline_unhandled_exception_returns_500():
    pipeline = MiddlewarePipeline([FailingMiddleware()])

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/crash",
        "headers": [(b"host", b"localhost")],
    }
    req = Request(scope)
    ctx = GatewayContext()

    async def terminal(r, c):
        return JSONResponse({"ok": True})

    res = await pipeline.execute(req, ctx, terminal)
    assert res.status_code == 500
    assert res.headers["X-Request-ID"] == ctx.request_id


# ---------------------------------------------------------------------------
# PER-ROUTE CONFIGURABLE MIDDLEWARE CHAIN & DUPLICATE RESTRICTION TESTS
# ---------------------------------------------------------------------------


def test_route_definition_rejects_duplicate_middleware():
    with pytest.raises(Exception) as exc_info:
        RouteDefinition(
            path_pattern="/duplicate-test",
            upstream_url="http://echo-service",
            middleware_chain=["auth", "rate_limit", "auth"],
        )
    assert "Cannot configure duplicate middleware 'auth'" in str(exc_info.value)


def test_route_definition_rejects_duplicate_middleware_case_insensitive():
    with pytest.raises(Exception) as exc_info:
        RouteDefinition(
            path_pattern="/case-dup-test",
            upstream_url="http://echo-service",
            middleware_chain=["Rate_Limit", "rate_limit"],
        )
    assert "Cannot configure duplicate middleware" in str(exc_info.value)


def test_route_definition_rejects_duplicate_middleware_via_alias():
    with pytest.raises(Exception) as exc_info:
        RouteDefinition(
            path_pattern="/alias-dup-test",
            upstream_url="http://echo-service",
            middleware_chain=["auth", "jwt"],
        )
    assert "Cannot configure duplicate middleware" in str(exc_info.value)


def test_route_definition_rejects_unknown_middleware():
    with pytest.raises(Exception) as exc_info:
        RouteDefinition(
            path_pattern="/unknown-mw-test",
            upstream_url="http://echo-service",
            middleware_chain=["auth", "nonexistent_firewall"],
        )
    assert "Unknown middleware 'nonexistent_firewall'" in str(exc_info.value)


@pytest.mark.asyncio
async def test_per_route_middleware_chain_execution_order_and_bypass():
    from gateway.middleware.auth import AuthMiddleware
    from gateway.middleware.rate_limit import InMemoryRateLimitEngine, RateLimitMiddleware
    from gateway.models.route import AuthMiddlewareConfig

    # Route 1: rate_limit runs FIRST, before auth
    route_rate_first = RouteDefinition(
        id="rate-first-route",
        path_pattern="/rate-first/**",
        methods={HTTPMethod.GET},
        upstream_url="http://echo-service",
        strip_prefix="/rate-first",
        middleware_chain=["rate_limit", "auth"],
        middleware=MiddlewareConfig(
            rate_limit=RateLimitConfig(enabled=True, requests_per_minute=2),
            auth=AuthMiddlewareConfig(enabled=True, required=True, allowed_roles={"admin"}),
        ),
    )

    # Route 2: auth runs FIRST, before rate_limit
    route_auth_first = RouteDefinition(
        id="auth-first-route",
        path_pattern="/auth-first/**",
        methods={HTTPMethod.GET},
        upstream_url="http://echo-service",
        strip_prefix="/auth-first",
        middleware_chain=["auth", "rate_limit"],
        middleware=MiddlewareConfig(
            rate_limit=RateLimitConfig(enabled=True, requests_per_minute=2),
            auth=AuthMiddlewareConfig(enabled=True, required=True, allowed_roles={"admin"}),
        ),
    )

    # Route 3: only auth in chain (rate limit bypassed)
    route_only_auth = RouteDefinition(
        id="only-auth-route",
        path_pattern="/only-auth/**",
        methods={HTTPMethod.GET},
        upstream_url="http://echo-service",
        strip_prefix="/only-auth",
        middleware_chain=["auth"],
        middleware=MiddlewareConfig(
            rate_limit=RateLimitConfig(enabled=True, requests_per_minute=2),
            auth=AuthMiddlewareConfig(enabled=True, required=False),
        ),
    )

    registry = RouteRegistry(routes=[route_rate_first, route_auth_first, route_only_auth])
    auth_mw = AuthMiddleware()
    rl_mw = RateLimitMiddleware(memory_engine=InMemoryRateLimitEngine())
    pipeline = MiddlewarePipeline(
        middlewares=[auth_mw, rl_mw],
        named_middlewares={"auth": auth_mw, "rate_limit": rl_mw},
    )

    mock_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=echo_app), base_url="http://echo-service"
    )
    engine = ProxyEngine(client=mock_client)
    app = create_app(registry=registry, proxy_engine=engine, pipeline=pipeline)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    )

    # Route 1: rate_limit first -> request 1 (401), request 2 (401), request 3 -> 429
    r1 = await client.get("/rate-first/ping")
    assert r1.status_code == 401
    r2 = await client.get("/rate-first/ping")
    assert r2.status_code == 401
    r3 = await client.get("/rate-first/ping")
    assert r3.status_code == 429  # Hit rate limit before auth!

    # Route 3: only auth in chain -> rate limit is bypassed completely
    for _ in range(5):
        resp = await client.get("/only-auth/ping")
        assert resp.status_code == 200

    await client.aclose()
    await mock_client.aclose()


@pytest.mark.asyncio
async def test_full_pipeline_with_cache_and_compression() -> None:
    from fastapi import FastAPI
    from fastapi.responses import Response as StarletteResponse

    backend_app = FastAPI()
    upstream_hits = 0

    large_body = (
        b'{"status": "ok", "items": ["alpha", "beta", "gamma"], "extra": "' + b"x" * 600 + b'"}'
    )

    @backend_app.get("/catalog")
    async def get_catalog():
        nonlocal upstream_hits
        upstream_hits += 1
        return StarletteResponse(
            content=large_body,
            media_type="application/json",
            headers={"Content-Length": str(len(large_body))},
        )

    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app),
        base_url="http://catalog-service",
    )

    from gateway.models.route import CacheConfig, CompressionConfig

    route = RouteDefinition(
        id="catalog-route",
        path_pattern="/catalog/**",
        methods={HTTPMethod.GET},
        upstream_url="http://catalog-service",
        strip_prefix="/catalog",
        middleware=MiddlewareConfig(
            cache=CacheConfig(enabled=True, ttl_seconds=30),
            compression=CompressionConfig(enabled=True, min_size_bytes=200),
        ),
    )

    registry = RouteRegistry(routes=[route])
    engine = ProxyEngine(client=backend_client)
    app = create_app(registry=registry, proxy_engine=engine)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        # Request 1: MISS, compressed with gzip
        r1 = await client.get(
            "/catalog/catalog",
            headers={"Accept-Encoding": "gzip"},
        )
        assert r1.status_code == 200
        assert r1.headers.get("X-Cache") == "MISS"
        assert r1.headers.get("Content-Encoding") == "gzip"
        assert upstream_hits == 1

        # Request 2: HIT, served from cache, compressed with gzip
        r2 = await client.get(
            "/catalog/catalog",
            headers={"Accept-Encoding": "gzip"},
        )
        assert r2.status_code == 200
        assert r2.headers.get("X-Cache") == "HIT"
        assert r2.headers.get("Content-Encoding") == "gzip"
        # Upstream backend was not hit a second time
        assert upstream_hits == 1

        # Request 3: HIT, but client requests uncompressed (Accept-Encoding: identity)
        r3 = await client.get(
            "/catalog/catalog",
            headers={"Accept-Encoding": "identity"},
        )
        assert r3.status_code == 200
        assert r3.headers.get("X-Cache") == "HIT"
        assert "Content-Encoding" not in r3.headers
        assert upstream_hits == 1
