from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from gateway.config import settings
from gateway.main import create_app
from gateway.models.backend import BackendInstance, HealthStatus
from gateway.models.route import RouteDefinition
from gateway.routing.registry import RouteRegistry
from mock_backend_services.echo_service import app as echo_app


@pytest.fixture
def test_jwt_secret() -> str:
    return settings.jwt_secret


def make_token(
    secret: str,
    sub: str = "alice",
    roles: list[str] | None = None,
    scopes: list[str] | None = None,
) -> str:
    payload = {
        "sub": sub,
        "roles": roles or ["user"],
        "scopes": scopes or [],
        "exp": datetime.now(UTC) + timedelta(hours=1),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.mark.asyncio
async def test_pdf_assignment_page_3_route_json_and_auth_scopes(test_jwt_secret: str) -> None:
    """Verifies that the exact JSON route configuration from Page 3 of the PDF

    assignment parses correctly and enforces auth scopes and rate limiting.
    """
    raw_pdf_route = {
        "path": "/api/users/*",
        "methods": ["GET", "POST", "PUT"],
        "target": "http://user-service:8080",
        "strip_prefix": "/api",
        "middlewares": ["auth", "rate_limit"],
        "middleware_config": {
            "auth": {"required": True, "scopes": ["users:read"]},
            "rate_limit": {"requests_per_minute": 100},
        },
    }

    # Verify model parses properly
    route = RouteDefinition.model_validate(raw_pdf_route)
    assert route.path_pattern == "/api/users/*"
    assert route.upstream_url == "http://user-service:8080"
    assert route.strip_prefix == "/api"
    assert route.middleware_chain == ["auth", "rate_limit"]
    assert route.middleware.auth.enabled is True
    assert route.middleware.auth.required is True
    assert route.middleware.auth.required_scopes == {"users:read"}
    assert route.middleware.rate_limit.enabled is True
    assert route.middleware.rate_limit.requests_per_minute == 100
    assert route.created_at is not None
    assert route.updated_at is not None

    # Now verify gateway behavior when proxying through this route
    echo_transport = ASGITransport(app=echo_app)
    mock_client = AsyncClient(transport=echo_transport, base_url="http://user-service:8080")

    from gateway.proxy.engine import ProxyEngine

    engine = ProxyEngine(client=mock_client)
    registry = RouteRegistry()
    await registry.register(route)

    gateway_app = create_app(registry=registry, proxy_engine=engine)
    gw_transport = ASGITransport(app=gateway_app)

    async with AsyncClient(transport=gw_transport, base_url="http://gateway.local") as client:
        # 1. Unauthenticated request -> 401 Unauthorized
        res_unauth = await client.get("/api/users/123")
        assert res_unauth.status_code == 401
        assert res_unauth.json()["error"] == "unauthorized"

        # 2. Authenticated but missing required scope 'users:read' -> 403 Forbidden
        token_no_scope = make_token(test_jwt_secret, sub="bob", scopes=["users:write"])
        res_forbidden = await client.get(
            "/api/users/123",
            headers={"Authorization": f"Bearer {token_no_scope}"},
        )
        assert res_forbidden.status_code == 403
        assert res_forbidden.json()["error"] == "forbidden"
        assert "required scopes" in res_forbidden.json()["message"].lower()

        # 3. Authenticated with required scope 'users:read' -> 200 OK
        token_valid = make_token(test_jwt_secret, sub="charlie", scopes=["users:read"])
        res_ok = await client.get(
            "/api/users/123",
            headers={"Authorization": f"Bearer {token_valid}"},
        )
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert data["path"] == "/users/123"
        assert data["headers"]["x-user-id"] == "charlie"


@pytest.mark.asyncio
async def test_pdf_response_transformation_cors_and_header_removal() -> None:
    """Verifies Response Transform capabilities from Page 4 of PDF:

    - Remove response headers
    - Add custom response headers
    - Inject timing headers (X-Response-Time, Server-Timing)
    - CORS header injection
    """
    echo_transport = ASGITransport(app=echo_app)
    mock_client = AsyncClient(transport=echo_transport, base_url="http://echo-backend:8001")

    from gateway.proxy.engine import ProxyEngine

    engine = ProxyEngine(client=mock_client)
    registry = RouteRegistry()

    cors_route_data = {
        "id": "cors-test-route",
        "path": "/public-api/**",
        "target": "http://echo-backend:8001",
        "strip_prefix": "/public-api",
        "middleware_config": {
            "transform": {
                "cors_enabled": True,
                "cors_allow_origin": "https://frontend.example.com",
                "cors_allow_methods": "GET, POST, OPTIONS",
                "cors_allow_headers": "Authorization, Content-Type",
                "cors_allow_credentials": True,
                "cors_max_age": 7200,
                "add_response_headers": {
                    "X-Custom-Response-Header": "Mountain-Active",
                },
                "remove_response_headers": [
                    "server",  # strip server signature header
                ],
            }
        },
    }

    route = RouteDefinition.model_validate(cors_route_data)
    await registry.register(route)

    gateway_app = create_app(registry=registry, proxy_engine=engine)
    gw_transport = ASGITransport(app=gateway_app)

    async with AsyncClient(transport=gw_transport, base_url="http://gateway.local") as client:
        res = await client.get(
            "/public-api/ping",
            headers={"Origin": "https://frontend.example.com"},
        )
        assert res.status_code == 200
        # Gateway timing headers
        assert "X-Response-Time" in res.headers
        assert "Server-Timing" in res.headers
        assert "X-Request-ID" in res.headers

        # Custom added response header
        assert res.headers["X-Custom-Response-Header"] == "Mountain-Active"

        # CORS injected headers
        assert res.headers["Access-Control-Allow-Origin"] == "https://frontend.example.com"
        assert res.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
        assert res.headers["Access-Control-Allow-Headers"] == "Authorization, Content-Type"
        assert res.headers["Access-Control-Allow-Credentials"] == "true"
        assert res.headers["Access-Control-Max-Age"] == "7200"


@pytest.mark.asyncio
async def test_pdf_backend_data_model_fields_and_health_check() -> None:
    """Verifies Backend Data Model completeness from Page 6 of PDF:

    - Unique identifier (id)
    - Name
    - Base URL
    - Health check endpoint
    - Health check interval
    - Current status (healthy, unhealthy, unknown)
    - Last health check time
    - Timestamps (created_at, updated_at)
    """
    backend = BackendInstance(
        name="User Microservice",
        base_url="http://user-svc:8080",
        health_check_endpoint="/healthz",
        health_check_interval=15.0,
    )

    assert backend.id is not None
    assert backend.name == "User Microservice"
    assert backend.base_url == "http://user-svc:8080"
    assert backend.url == "http://user-svc:8080"
    assert backend.health_check_endpoint == "/healthz"
    assert backend.health_check_interval == 15.0
    assert backend.status == HealthStatus.HEALTHY
    assert backend.created_at is not None
    assert backend.updated_at is not None

    # Test health check execution uses custom health_check_endpoint
    from gateway.discovery.health_checker import HealthChecker

    class MockProbeTransport(ASGITransport):
        def __init__(self):
            self.requested_paths = []
            super().__init__(app=echo_app)

    registry = RouteRegistry()
    called_paths = []

    from fastapi import FastAPI

    probe_app = FastAPI()

    @probe_app.get("/healthz")
    async def custom_probe():
        called_paths.append("/healthz")
        return {"status": "ok"}

    custom_transport = ASGITransport(app=probe_app)
    async with AsyncClient(
        transport=custom_transport,
        base_url="http://user-svc:8080",
    ) as probe_client:
        checker = HealthChecker(registry=registry)
        await checker.check_backend(probe_client, backend)

    assert "/healthz" in called_paths
    assert backend.status == HealthStatus.HEALTHY
    assert backend.last_health_check_time is not None
    assert backend.last_checked_at is not None
