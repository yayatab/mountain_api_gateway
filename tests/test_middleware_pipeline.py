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
