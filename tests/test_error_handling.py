import httpx
import pytest

from gateway.main import create_app
from gateway.models.route import HttpMethod, RouteDefinition
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry


class TimeoutMockTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Request timed out", request=request)


class ConnectionErrorMockTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)


@pytest.fixture
def error_registry() -> RouteRegistry:
    routes = [
        RouteDefinition(
            id="read-only-route",
            name="Read Only Service",
            path_pattern="/api/v1/readonly",
            methods=[HttpMethod.GET],
            upstream_url="http://readonly-service",
            timeout_seconds=0.1,
        ),
        RouteDefinition(
            id="timeout-route",
            name="Timeout Service",
            path_pattern="/api/v1/slow",
            methods=[HttpMethod.GET],
            upstream_url="http://slow-service",
            timeout_seconds=0.05,
        ),
    ]
    return RouteRegistry(routes=routes)


@pytest.mark.asyncio
async def test_upstream_timeout_returns_504(error_registry: RouteRegistry):
    timeout_client = httpx.AsyncClient(transport=TimeoutMockTransport())
    engine = ProxyEngine(client=timeout_client)
    app = create_app(registry=error_registry, proxy_engine=engine)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    )
    response = await client.get("/api/v1/slow")

    assert response.status_code == 504
    data = response.json()
    assert data["error"] == "gateway_timeout"
    assert "did not respond" in data["message"]
    assert "request_id" in data
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_upstream_connection_failure_returns_502(error_registry: RouteRegistry):
    conn_error_client = httpx.AsyncClient(transport=ConnectionErrorMockTransport())
    engine = ProxyEngine(client=conn_error_client)
    app = create_app(registry=error_registry, proxy_engine=engine)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    )
    response = await client.get("/api/v1/readonly")

    assert response.status_code == 502
    data = response.json()
    assert data["error"] == "bad_gateway"
    assert "Failed to connect" in data["message"]
    assert "request_id" in data
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_route_not_found_returns_404(error_registry: RouteRegistry):
    app = create_app(registry=error_registry)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    )

    response = await client.get("/api/v1/missing-endpoint")
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "route_not_found"
    assert "/api/v1/missing-endpoint" in data["message"]
    assert "request_id" in data


@pytest.mark.asyncio
async def test_method_not_allowed_returns_405_with_allow_header(error_registry: RouteRegistry):
    app = create_app(registry=error_registry)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    )

    response = await client.post("/api/v1/readonly", json={"data": "test"})
    assert response.status_code == 405
    assert response.headers["Allow"] == "GET"
    data = response.json()
    assert data["error"] == "method_not_allowed"
    assert "POST" in data["message"]


@pytest.mark.asyncio
async def test_reserved_system_and_docs_endpoints(error_registry: RouteRegistry):
    app = create_app(registry=error_registry)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    )

    # Health check
    health_resp = await client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json() == {"status": "healthy", "service": "api-gateway"}

    # Interactive Swagger UI /docs
    docs_resp = await client.get("/docs")
    assert docs_resp.status_code == 200
    assert "swagger-ui" in docs_resp.text.lower() or "html" in docs_resp.headers.get(
        "content-type", ""
    )

    # OpenAPI schema /openapi.json
    openapi_resp = await client.get("/openapi.json")
    assert openapi_resp.status_code == 200
    schema = openapi_resp.json()
    assert schema["info"]["title"] == "Mountain API Gateway"
    assert "Dynamic Routing" in schema["info"]["description"]
    assert "/health" in schema["paths"]
    assert "/api/guide" in schema["paths"]

    # Programmatic API Guide
    guide_resp = await client.get("/api/guide")
    assert guide_resp.status_code == 200
    guide_data = guide_resp.json()
    assert guide_data["name"] == "Mountain API Gateway"
    assert "matching_rules" in guide_data
    assert "injected_headers" in guide_data
