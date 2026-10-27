import json
from http import HTTPMethod
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.discovery.load_balancer import LoadBalancer
from gateway.main import create_app
from gateway.models.route import RouteDefinition
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry


def make_echo_app(service_name: str):
    async def app(scope, receive, send):
        assert scope["type"] == "http"
        body = json.dumps({"service": service_name, "path": scope["path"]}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, apps: dict[str, Any]):
        self.transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self.transports:
            return await self.transports[host].handle_async_request(request)
        raise httpx.ConnectError(f"Host '{host}' unreachable", request=request)


@pytest.fixture
def admin_setup():
    app1 = make_echo_app("echo-1")
    app2 = make_echo_app("echo-2")
    transport = MockTransport({"service-1": app1, "service-2": app2})

    routes = [
        RouteDefinition(
            id="route-1",
            name="Service 1",
            path_pattern="/s1/**",
            methods={HTTPMethod.GET},
            upstream_url="http://service-1",
            enabled=True,
        ),
        RouteDefinition(
            id="route-2",
            name="Service 2",
            path_pattern="/s2/**",
            methods={HTTPMethod.GET},
            upstream_url="http://service-2",
            enabled=False,
        ),
    ]

    registry = RouteRegistry(routes=routes)
    lb = LoadBalancer()
    mock_client = httpx.AsyncClient(transport=transport)
    engine = ProxyEngine(client=mock_client, on_result=lb.record_result)
    gateway_app = create_app(registry=registry, proxy_engine=engine, load_balancer=lb)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )
    return client, registry, gateway_app


@pytest.mark.asyncio
async def test_admin_create_route_success(admin_setup):
    client, registry, _ = admin_setup

    new_route_payload = {
        "id": "new-route",
        "name": "New Dynamic Service",
        "path_pattern": "/dynamic/**",
        "methods": ["GET"],
        "upstream_url": "http://service-1",
        "enabled": True,
    }

    resp = await client.post("/admin/routes", json=new_route_payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == "new-route"
    assert data["name"] == "New Dynamic Service"

    # Verify route is now immediately active and routable
    traffic_resp = await client.get("/dynamic/hello")
    assert traffic_resp.status_code == 200
    assert traffic_resp.json()["service"] == "echo-1"


@pytest.mark.asyncio
async def test_admin_create_route_conflict_409(admin_setup):
    client, _, _ = admin_setup

    duplicate_payload = {
        "id": "route-1",  # Already exists in initial routes
        "name": "Duplicate Route",
        "path_pattern": "/duplicate",
        "upstream_url": "http://service-1",
    }

    resp = await client.post("/admin/routes", json=duplicate_payload)
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_list_routes(admin_setup):
    client, _, _ = admin_setup

    # Default includes disabled
    resp_all = await client.get("/admin/routes")
    assert resp_all.status_code == 200
    all_routes = resp_all.json()
    assert len(all_routes) == 2

    # Filter only active
    resp_active = await client.get("/admin/routes?include_disabled=false")
    assert resp_active.status_code == 200
    active_routes = resp_active.json()
    assert len(active_routes) == 1
    assert active_routes[0]["id"] == "route-1"


@pytest.mark.asyncio
async def test_admin_get_route_by_id(admin_setup):
    client, _, _ = admin_setup

    resp = await client.get("/admin/routes/route-1")
    assert resp.status_code == 200
    assert resp.json()["id"] == "route-1"

    resp_404 = await client.get("/admin/routes/non-existent")
    assert resp_404.status_code == 404
    assert "not found" in resp_404.json()["detail"]


@pytest.mark.asyncio
async def test_admin_update_route(admin_setup):
    client, _, _ = admin_setup

    update_payload = {
        "name": "Updated Service 1",
        "path_pattern": "/s1/v2/**",
        "methods": ["GET"],
        "upstream_url": "http://service-1",
        "priority": 5,
        "enabled": True,
    }

    resp = await client.put("/admin/routes/route-1", json=update_payload)
    assert resp.status_code == 200
    assert resp.json()["path_pattern"] == "/s1/v2/**"
    assert resp.json()["priority"] == 5

    # Old path should no longer match
    old_traffic = await client.get("/s1/old")
    assert old_traffic.status_code == 404

    # New path must match
    new_traffic = await client.get("/s1/v2/new")
    assert new_traffic.status_code == 200

    # Updating non-existent route returns 404
    resp_404 = await client.put("/admin/routes/missing", json=update_payload)
    assert resp_404.status_code == 404


@pytest.mark.asyncio
async def test_admin_delete_route(admin_setup):
    client, _, _ = admin_setup

    resp = await client.delete("/admin/routes/route-1")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Subsequent traffic to that route returns 404
    traffic_resp = await client.get("/s1/test")
    assert traffic_resp.status_code == 404

    # Deleting missing route returns 404
    resp_404 = await client.delete("/admin/routes/route-1")
    assert resp_404.status_code == 404


@pytest.mark.asyncio
async def test_admin_patch_route_status_disable_and_enable(admin_setup):
    client, _, _ = admin_setup

    # Disable route-1
    resp_disable = await client.patch(
        "/admin/routes/route-1/status",
        json={"enabled": False},
    )
    assert resp_disable.status_code == 200
    assert resp_disable.json()["enabled"] is False

    # Traffic should now return 404
    traffic_disabled = await client.get("/s1/test")
    assert traffic_disabled.status_code == 404

    # Re-enable route-1
    resp_enable = await client.patch(
        "/admin/routes/route-1/status",
        json={"enabled": True},
    )
    assert resp_enable.status_code == 200
    assert resp_enable.json()["enabled"] is True

    # Traffic restored
    traffic_enabled = await client.get("/s1/test")
    assert traffic_enabled.status_code == 200

    # Patch non-existent route returns 404
    resp_404 = await client.patch(
        "/admin/routes/non-existent/status",
        json={"enabled": True},
    )
    assert resp_404.status_code == 404


@pytest.mark.asyncio
async def test_admin_health(admin_setup):
    client, _, _ = admin_setup
    resp = await client.get("/admin/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "mountain-api-gateway"
    assert data["uptime_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_admin_stats(admin_setup):
    client, _, _ = admin_setup
    resp = await client.get("/admin/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_routes"] == 2
    assert data["active_routes"] == 1
    assert data["disabled_routes"] == 1
    assert data["total_backends"] >= 1


@pytest.mark.asyncio
async def test_admin_backends(admin_setup):
    client, _, _ = admin_setup
    resp = await client.get("/admin/backends")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_admin_hot_reload_from_disk_without_restart(tmp_path: Path):
    routes_file = tmp_path / "routes.json"
    initial_routes = [
        {
            "id": "file-route-1",
            "name": "File Route 1",
            "path_pattern": "/v1/file/**",
            "methods": ["GET"],
            "upstream_url": "http://service-1",
            "enabled": True,
        }
    ]
    routes_file.write_text(json.dumps(initial_routes), encoding="utf-8")

    app1 = make_echo_app("echo-file-1")
    app2 = make_echo_app("echo-file-2")
    transport = MockTransport({"service-1": app1, "service-2": app2})

    registry = RouteRegistry(persistence_path=routes_file)
    await registry.load_from_file(routes_file)

    lb = LoadBalancer()
    mock_client = httpx.AsyncClient(transport=transport)
    engine = ProxyEngine(client=mock_client, on_result=lb.record_result)
    gateway_app = create_app(registry=registry, proxy_engine=engine, load_balancer=lb)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )

    # Initial route matches
    r1 = await client.get("/v1/file/hello")
    assert r1.status_code == 200

    # Modify file externally (adding a new route)
    updated_routes = initial_routes + [
        {
            "id": "file-route-2",
            "name": "File Route 2",
            "path_pattern": "/v2/file/**",
            "methods": ["GET"],
            "upstream_url": "http://service-2",
            "enabled": True,
        }
    ]
    routes_file.write_text(json.dumps(updated_routes), encoding="utf-8")

    # Trigger hot-reload via Admin API
    reload_resp = await client.post("/admin/routes/reload")
    assert reload_resp.status_code == 200
    assert reload_resp.json()["reloaded_count"] == 2
    assert reload_resp.json()["success"] is True

    # Newly added route is immediately live without process restart!
    r2 = await client.get("/v2/file/world")
    assert r2.status_code == 200
    assert r2.json()["service"] == "echo-file-2"


@pytest.mark.asyncio
async def test_admin_hot_reload_missing_file_returns_400():
    registry = RouteRegistry()
    gateway_app = create_app(registry=registry)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )
    resp = await client.post("/admin/routes/reload")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# RESTART PERSISTENCE VERIFICATION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configuration_persists_after_restart(tmp_path: Path):
    """Verifies that all mutations made via the Admin API (creation, update,

    status toggle, deletion) persist to disk and are correctly restored
    when the gateway process restarts.
    """
    persistence_file = tmp_path / "persisted_routes.json"

    # Initial route setup
    initial_route = RouteDefinition(
        id="base-route",
        name="Base Service",
        path_pattern="/base/**",
        methods={HTTPMethod.GET},
        upstream_url="http://service-1",
        enabled=True,
    )
    to_be_deleted = RouteDefinition(
        id="temp-route",
        name="Temporary Service",
        path_pattern="/temp/**",
        methods={HTTPMethod.GET},
        upstream_url="http://service-1",
        enabled=True,
    )

    app1 = make_echo_app("echo-live")
    transport = MockTransport({"service-1": app1})

    # --- SESSION 1: Start Gateway with persistence_path ---
    reg1 = RouteRegistry(routes=[initial_route, to_be_deleted], persistence_path=persistence_file)
    # Save initial state
    await reg1.save_to_file(persistence_file)

    lb1 = LoadBalancer()
    mock_client1 = httpx.AsyncClient(transport=transport)
    engine1 = ProxyEngine(client=mock_client1, on_result=lb1.record_result)
    gateway_app1 = create_app(registry=reg1, proxy_engine=engine1, load_balancer=lb1)

    client1 = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app1),
        base_url="http://gateway.test",
    )

    # 1. Create a new route
    create_resp = await client1.post(
        "/admin/routes",
        json={
            "id": "persisted-created",
            "name": "Created Via API",
            "path_pattern": "/persisted/**",
            "methods": ["GET"],
            "upstream_pool": [{"url": "http://service-1", "weight": 1}],
            "enabled": True,
        },
    )
    assert create_resp.status_code == 201

    # 2. Update base-route
    update_resp = await client1.put(
        "/admin/routes/base-route",
        json={
            "name": "Updated Base Service",
            "path_pattern": "/base-updated/**",
            "methods": ["GET"],
            "upstream_url": "http://service-1",
            "priority": 10,
            "enabled": True,
        },
    )
    assert update_resp.status_code == 200

    # 3. Disable persisted-created route
    disable_resp = await client1.patch(
        "/admin/routes/persisted-created/status",
        json={"enabled": False},
    )
    assert disable_resp.status_code == 200
    assert disable_resp.json()["enabled"] is False

    # 4. Delete temp-route
    delete_resp = await client1.delete("/admin/routes/temp-route")
    assert delete_resp.status_code == 200

    # Ensure client1 is closed (simulating process shutdown)
    await client1.aclose()
    await mock_client1.aclose()

    # --- SIMULATE PROCESS RESTART ---
    # File must exist on disk and contain valid JSON
    assert persistence_file.exists()
    content = json.loads(persistence_file.read_text(encoding="utf-8"))
    persisted_ids = {item["id"] for item in content}
    assert "persisted-created" in persisted_ids
    assert "base-route" in persisted_ids
    assert "temp-route" not in persisted_ids

    # --- SESSION 2: Start Brand New Gateway Process Loading from Same File ---
    reg2 = RouteRegistry(persistence_path=persistence_file)
    await reg2.load_from_file(persistence_file)

    lb2 = LoadBalancer()
    mock_client2 = httpx.AsyncClient(transport=transport)
    engine2 = ProxyEngine(client=mock_client2, on_result=lb2.record_result)
    gateway_app2 = create_app(registry=reg2, proxy_engine=engine2, load_balancer=lb2)

    client2 = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app2),
        base_url="http://gateway.test",
    )

    # Verify restored inventory via Admin API
    list_resp = await client2.get("/admin/routes")
    assert list_resp.status_code == 200
    restored_routes = {r["id"]: r for r in list_resp.json()}

    # 1. Created route restored with disabled status intact
    assert "persisted-created" in restored_routes
    assert restored_routes["persisted-created"]["enabled"] is False

    # 2. Updated route restored with updated pattern and priority
    assert "base-route" in restored_routes
    assert restored_routes["base-route"]["path_pattern"] == "/base-updated/**"
    assert restored_routes["base-route"]["priority"] == 10

    # 3. Deleted route remains deleted
    assert "temp-route" not in restored_routes

    # 4. Live traffic verification against restarted gateway
    # /base-updated/** works
    t1 = await client2.get("/base-updated/data")
    assert t1.status_code == 200
    assert t1.json()["service"] == "echo-live"

    # Old /base/** does not work (was updated)
    t2 = await client2.get("/base/data")
    assert t2.status_code == 404

    # /persisted/** does not work (was disabled)
    t3 = await client2.get("/persisted/data")
    assert t3.status_code == 404

    # /temp/** does not work (was deleted)
    t4 = await client2.get("/temp/data")
    assert t4.status_code == 404

    await client2.aclose()
    await mock_client2.aclose()


@pytest.mark.asyncio
async def test_admin_create_route_rejects_duplicate_middleware_chain():
    registry = RouteRegistry()
    app = create_app(registry=registry)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    )

    resp = await client.post(
        "/admin/routes",
        json={
            "id": "duplicate-mw-route",
            "name": "Duplicate MW Route",
            "path_pattern": "/dup-chain/**",
            "methods": ["GET"],
            "upstream_url": "http://echo-service:8001",
            "middleware_chain": ["auth", "rate_limit", "auth"],
        },
    )
    assert resp.status_code == 422
    assert "Cannot configure duplicate middleware" in resp.text
    await client.aclose()


@pytest.mark.asyncio
async def test_admin_create_route_hot_loaded_and_permanently_stored(tmp_path: Path):
    persistence_file = tmp_path / "dynamic_routes.json"
    transport = MockTransport({"echo-target": make_echo_app("echo-target")})

    # Initialize gateway with clean empty storage file
    reg1 = RouteRegistry(persistence_path=persistence_file)
    lb1 = LoadBalancer()
    mock_client1 = httpx.AsyncClient(transport=transport)
    engine1 = ProxyEngine(client=mock_client1, on_result=lb1.record_result)
    gateway_app1 = create_app(registry=reg1, proxy_engine=engine1, load_balancer=lb1)

    client1 = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app1),
        base_url="http://gateway.test",
    )

    # 1. Verify path is not mapped initially (404)
    r_init = await client1.get("/dynamic-hot/**/ping")
    assert r_init.status_code == 404

    # 2. Add route via Admin API at runtime (NOT compile/build time)
    create_resp = await client1.post(
        "/admin/routes",
        json={
            "id": "hotloaded-route",
            "name": "Hot-Loaded Dynamic Route",
            "path_pattern": "/dynamic-hot/**",
            "methods": ["GET"],
            "upstream_url": "http://echo-target",
            "strip_prefix": "/dynamic-hot",
            "middleware_chain": ["auth"],
            "middleware": {
                "auth": {
                    "enabled": True,
                    "required": True,
                    "allowed_roles": ["admin"],
                }
            },
        },
    )
    assert create_resp.status_code == 201

    # 3. Verify IMMEDIATELY hot-loaded into running gateway without restart
    # Protected by auth middleware -> returns 401 Unauthorized
    r_hot = await client1.get("/dynamic-hot/ping")
    assert r_hot.status_code == 401
    assert "unauthorized" in r_hot.text

    # 4. Verify PERMANENTLY stored on disk
    assert persistence_file.exists()
    file_content = json.loads(persistence_file.read_text(encoding="utf-8"))
    assert len(file_content) == 1
    assert file_content[0]["id"] == "hotloaded-route"
    assert file_content[0]["middleware_chain"] == ["auth"]

    await client1.aclose()
    await mock_client1.aclose()

    # 5. Simulate Server Reboot: Start brand new gateway session loading file
    reg2 = RouteRegistry(persistence_path=persistence_file)
    await reg2.load_from_file(persistence_file)
    lb2 = LoadBalancer()
    mock_client2 = httpx.AsyncClient(transport=transport)
    engine2 = ProxyEngine(client=mock_client2, on_result=lb2.record_result)
    gateway_app2 = create_app(registry=reg2, proxy_engine=engine2, load_balancer=lb2)

    client2 = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app2),
        base_url="http://gateway.test",
    )

    # Re-verify route exists and retains custom middleware chain after restart
    r_restarted = await client2.get("/dynamic-hot/ping")
    assert r_restarted.status_code == 401
    assert "unauthorized" in r_restarted.text

    await client2.aclose()
    await mock_client2.aclose()
