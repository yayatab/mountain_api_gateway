from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest

from gateway.config import settings
from gateway.main import create_app
from gateway.models.route import (
    AuthMiddlewareConfig,
    HTTPMethod,
    MiddlewareConfig,
    RouteDefinition,
)
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry
from mock_backend_services.echo_service import app as echo_app


def create_token(
    payload: dict,
    secret: str = settings.jwt_secret,
    expires_in: timedelta = timedelta(hours=1),
) -> str:
    now = datetime.now(UTC)
    claims = {
        "iat": now,
        "exp": now + expires_in,
        **payload,
    }
    return jwt.encode(claims, secret, algorithm=settings.jwt_algorithm)


@pytest.fixture
def auth_routes() -> list[RouteDefinition]:
    return [
        RouteDefinition(
            id="required-auth-route",
            name="Required Auth",
            path_pattern="/secure/data",
            methods={HTTPMethod.GET},
            upstream_url="http://echo-service",
            middleware=MiddlewareConfig(
                auth=AuthMiddlewareConfig(
                    enabled=True,
                    required=True,
                )
            ),
        ),
        RouteDefinition(
            id="optional-auth-route",
            name="Optional Auth",
            path_pattern="/public/data",
            methods={HTTPMethod.GET},
            upstream_url="http://echo-service",
            middleware=MiddlewareConfig(
                auth=AuthMiddlewareConfig(
                    enabled=True,
                    required=False,
                )
            ),
        ),
        RouteDefinition(
            id="rbac-route",
            name="Admin Only",
            path_pattern="/admin/dashboard",
            methods={HTTPMethod.GET},
            upstream_url="http://echo-service",
            middleware=MiddlewareConfig(
                auth=AuthMiddlewareConfig(
                    enabled=True,
                    required=True,
                    allowed_roles={"admin", "superadmin"},
                )
            ),
        ),
        RouteDefinition(
            id="scope-route",
            name="Reports Scope Only",
            path_pattern="/reports/export",
            methods={HTTPMethod.GET},
            upstream_url="http://echo-service",
            middleware=MiddlewareConfig(
                auth=AuthMiddlewareConfig(
                    enabled=True,
                    required=True,
                    required_scopes={"read:reports", "export:data"},
                )
            ),
        ),
    ]


@pytest.fixture
def auth_client(auth_routes: list[RouteDefinition]) -> httpx.AsyncClient:
    registry = RouteRegistry(routes=auth_routes)
    mock_echo_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=echo_app),
        base_url="http://echo-service",
    )
    engine = ProxyEngine(client=mock_echo_client)
    app = create_app(registry=registry, proxy_engine=engine)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    )


@pytest.mark.asyncio
async def test_valid_jwt_authenticates_and_injects_upstream_headers(
    auth_client: httpx.AsyncClient,
):
    token = create_token({"sub": "user_42", "roles": ["user", "analyst"]})
    resp = await auth_client.get(
        "/secure/data",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["headers"]["x-user-id"] == "user_42"
    assert set(data["headers"]["x-user-roles"].split(",")) == {"user", "analyst"}


@pytest.mark.asyncio
async def test_missing_token_on_required_route_returns_401(auth_client: httpx.AsyncClient):
    resp = await auth_client.get("/secure/data")
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "unauthorized"
    assert "Bearer token is required" in data["message"]


@pytest.mark.asyncio
async def test_expired_jwt_returns_401_token_expired(auth_client: httpx.AsyncClient):
    expired_token = create_token({"sub": "user_42"}, expires_in=timedelta(seconds=-10))
    resp = await auth_client.get(
        "/secure/data",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "token_expired"
    assert "expired" in data["message"]


@pytest.mark.asyncio
async def test_invalid_signature_returns_401_invalid_token(auth_client: httpx.AsyncClient):
    tampered_token = create_token(
        {"sub": "user_42"},
        secret="wrong-secret-key-that-is-at-least-32-bytes-long",
    )
    resp = await auth_client.get(
        "/secure/data",
        headers={"Authorization": f"Bearer {tampered_token}"},
    )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_optional_auth_allows_anonymous_and_authenticated(
    auth_client: httpx.AsyncClient,
):
    # Anonymous request
    anon_resp = await auth_client.get("/public/data")
    assert anon_resp.status_code == 200
    anon_data = anon_resp.json()
    assert "x-user-id" not in anon_data["headers"]

    # Authenticated request
    token = create_token({"sub": "user_99"})
    auth_resp = await auth_client.get(
        "/public/data",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert auth_resp.status_code == 200
    auth_data = auth_resp.json()
    assert auth_data["headers"]["x-user-id"] == "user_99"


@pytest.mark.asyncio
async def test_role_based_access_control(auth_client: httpx.AsyncClient):
    # User role lacks "admin"
    user_token = create_token({"sub": "user_1", "roles": ["viewer"]})
    forbidden_resp = await auth_client.get(
        "/admin/dashboard",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert forbidden_resp.status_code == 403
    assert forbidden_resp.json()["error"] == "forbidden"

    # Admin role succeeds
    admin_token = create_token({"sub": "admin_1", "roles": ["admin"]})
    allowed_resp = await auth_client.get(
        "/admin/dashboard",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert allowed_resp.status_code == 200


@pytest.mark.asyncio
async def test_scope_based_access_control(auth_client: httpx.AsyncClient):
    # Missing export:data scope
    partial_scope_token = create_token({"sub": "user_2", "scopes": ["read:reports"]})
    forbidden_resp = await auth_client.get(
        "/reports/export",
        headers={"Authorization": f"Bearer {partial_scope_token}"},
    )
    assert forbidden_resp.status_code == 403
    assert forbidden_resp.json()["error"] == "forbidden"

    # Full scopes succeed
    full_scope_token = create_token(
        {"sub": "user_2", "scopes": ["read:reports", "export:data", "share:data"]}
    )
    allowed_resp = await auth_client.get(
        "/reports/export",
        headers={"Authorization": f"Bearer {full_scope_token}"},
    )
    assert allowed_resp.status_code == 200


@pytest.mark.asyncio
async def test_unauthorized_empty_authorization_header(auth_client: httpx.AsyncClient):
    resp = await auth_client.get("/secure/data", headers={"Authorization": ""})
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "unauthorized"
    assert "X-Request-ID" in resp.headers


@pytest.mark.asyncio
async def test_unauthorized_wrong_scheme_basic(auth_client: httpx.AsyncClient):
    resp = await auth_client.get(
        "/secure/data",
        headers={"Authorization": "Basic dXNlcjpwYXNzd29yZA=="},
    )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "unauthorized"
    assert "Bearer token is required" in data["message"]


@pytest.mark.asyncio
async def test_unauthorized_bearer_without_token(auth_client: httpx.AsyncClient):
    # Header with only "Bearer"
    resp1 = await auth_client.get("/secure/data", headers={"Authorization": "Bearer"})
    assert resp1.status_code == 401
    assert resp1.json()["error"] == "unauthorized"

    # Header with "Bearer   " (whitespace only)
    resp2 = await auth_client.get("/secure/data", headers={"Authorization": "Bearer    "})
    assert resp2.status_code == 401
    assert resp2.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_unauthorized_raw_token_without_bearer_prefix(auth_client: httpx.AsyncClient):
    valid_token = create_token({"sub": "user_42"})
    resp = await auth_client.get("/secure/data", headers={"Authorization": valid_token})
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_unauthorized_malformed_jwt_garbage_string(auth_client: httpx.AsyncClient):
    resp = await auth_client.get(
        "/secure/data",
        headers={"Authorization": "Bearer completely-not-a-jwt"},
    )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_unauthorized_truncated_jwt(auth_client: httpx.AsyncClient):
    # Truncated token missing signature part
    truncated = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    resp = await auth_client.get(
        "/secure/data",
        headers={"Authorization": f"Bearer {truncated}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_unauthorized_algorithm_none_rejected(auth_client: httpx.AsyncClient):
    # Craft an unsigned token with 'none' algorithm
    none_token = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJhdHRhY2tlciJ9."
    resp = await auth_client.get(
        "/secure/data",
        headers={"Authorization": f"Bearer {none_token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_unauthorized_optional_auth_with_corrupted_token(auth_client: httpx.AsyncClient):
    # When a token is supplied to an optional route, but the token is corrupted/invalid,
    # it must fail with 401 rather than quietly passing as anonymous
    resp = await auth_client.get(
        "/public/data",
        headers={"Authorization": "Bearer bad-corrupted-token"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_token"
