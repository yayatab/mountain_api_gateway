import httpx
import pytest

from gateway.main import create_app
from gateway.models.context import GatewayContext
from gateway.models.route import HttpMethod, RouteDefinition
from gateway.proxy.client import ProxyHttpClientPool
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry
from mock_backend_services.echo_service import app as echo_app


@pytest.fixture
def mock_echo_client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=echo_app)
    return httpx.AsyncClient(transport=transport, base_url="http://echo-service")


@pytest.fixture
def proxy_registry() -> RouteRegistry:
    routes = [
        RouteDefinition(
            id="echo-route",
            name="Echo Service",
            path_pattern="/api/v1/echo",
            methods={
                HttpMethod.GET,
                HttpMethod.POST,
                HttpMethod.PUT,
                HttpMethod.DELETE,
                HttpMethod.PATCH,
            },
            upstream_url="http://echo-service",
        ),
        RouteDefinition(
            id="prefix-rewrite-route",
            name="Prefix Rewrite",
            path_pattern="/legacy/api/**",
            methods={HttpMethod.GET, HttpMethod.POST},
            upstream_url="http://echo-service/upstream",
            strip_prefix="/legacy",
            add_prefix="/modern",
        ),
    ]
    return RouteRegistry(routes=routes)


@pytest.fixture
def gateway_test_client(
    proxy_registry: RouteRegistry,
    mock_echo_client: httpx.AsyncClient,
) -> httpx.AsyncClient:
    engine = ProxyEngine(client=mock_echo_client)
    app = create_app(registry=proxy_registry, proxy_engine=engine)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://gateway.test")


@pytest.mark.asyncio
async def test_get_request_proxying(gateway_test_client: httpx.AsyncClient):
    response = await gateway_test_client.get(
        "/api/v1/echo?filter=active&limit=10",
        headers={"X-Custom-Client": "FrontendApp"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "GET"
    assert data["path"] == "/api/v1/echo"
    assert data["query_params"] == {"filter": "active", "limit": "10"}
    assert data["headers"]["x-custom-client"] == "FrontendApp"
    assert "x-request-id" in response.headers


@pytest.mark.asyncio
async def test_post_request_body_streaming(gateway_test_client: httpx.AsyncClient):
    payload = {"message": "Hello upstream!", "count": 42}
    response = await gateway_test_client.post(
        "/api/v1/echo",
        json=payload,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "POST"
    assert data["body"] == payload


@pytest.mark.asyncio
async def test_put_and_delete_methods(gateway_test_client: httpx.AsyncClient):
    put_resp = await gateway_test_client.put(
        "/api/v1/echo",
        content="plain text update",
        headers={"Content-Type": "text/plain"},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["body"] == "plain text update"

    del_resp = await gateway_test_client.delete("/api/v1/echo")
    assert del_resp.status_code == 200
    assert del_resp.json()["method"] == "DELETE"


@pytest.mark.asyncio
async def test_path_rewriting_proxying(gateway_test_client: httpx.AsyncClient):
    # Route: /legacy/api/** -> strip '/legacy' -> '/api/**', add '/modern' -> '/modern/api/**'
    response = await gateway_test_client.get("/legacy/api/items/99")
    assert response.status_code == 200
    data = response.json()
    assert data["path"] == "/upstream/modern/api/items/99"


@pytest.mark.asyncio
async def test_proxy_http_client_pool_lifecycle():
    pool = ProxyHttpClientPool(max_connections=50, max_keepalive_connections=10)
    assert pool._client is None
    await pool.startup()
    assert pool._client is not None
    client = pool.get_client()
    assert not client.is_closed

    # Second startup is idempotent
    await pool.startup()
    assert pool._client is client

    await pool.shutdown()
    assert pool._client is None

    # Shutdown again is safe
    await pool.shutdown()
    assert pool._client is None


@pytest.mark.asyncio
async def test_proxy_http_client_pool_auto_init():
    pool = ProxyHttpClientPool()
    assert pool._client is None
    # Calling get_client before startup initializes the client on-demand
    client = pool.get_client()
    assert client is not None
    assert not client.is_closed
    await pool.shutdown()


@pytest.mark.asyncio
async def test_proxy_engine_on_result_callback_on_connect_error():
    callback_results: list[tuple[str, bool]] = []

    def on_result(url: str, success: bool) -> None:
        callback_results.append((url, success))

    engine = ProxyEngine(on_result=on_result)
    # Target unreachable port
    context = GatewayContext()

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/fail",
        "headers": [(b"host", b"localhost")],
        "query_string": b"",
    }
    request = Request(scope)

    resp = await engine.forward(
        request=request,
        context=context,
        target_base_url="http://127.0.0.1:54321",
        target_path="/fail",
        timeout_seconds=0.5,
    )
    assert resp.status_code == 502
    assert len(callback_results) == 1
    assert callback_results[0] == ("http://127.0.0.1:54321", False)


@pytest.mark.asyncio
async def test_proxy_engine_on_result_callback_on_timeout():
    callback_results: list[tuple[str, bool]] = []

    def on_result(url: str, success: bool) -> None:
        callback_results.append((url, success))

    class TimeoutTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("Timeout exceeded", request=request)

    custom_client = httpx.AsyncClient(transport=TimeoutTransport())
    engine = ProxyEngine(client=custom_client, on_result=on_result)
    context = GatewayContext()

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/timeout",
        "headers": [(b"host", b"localhost")],
        "query_string": b"",
    }
    request = Request(scope)

    resp = await engine.forward(
        request=request,
        context=context,
        target_base_url="http://timeout-service",
        target_path="/timeout",
        timeout_seconds=0.1,
    )
    assert resp.status_code == 504
    assert len(callback_results) == 1
    assert callback_results[0] == ("http://timeout-service", False)
    await custom_client.aclose()
