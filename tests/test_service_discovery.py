import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gateway.discovery.circuit_breaker import CircuitBreaker, CircuitState
from gateway.discovery.health_checker import HealthChecker
from gateway.discovery.load_balancer import LoadBalancer
from gateway.main import create_app
from gateway.models.backend import BackendInstance, HealthStatus
from gateway.models.route import HTTPMethod, RouteDefinition
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry


def make_mock_app(app_name: str) -> FastAPI:
    mock_app = FastAPI(title=f"Mock Backend {app_name}")

    @mock_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": app_name}

    @mock_app.api_route("/{full_path:path}", methods=["GET", "POST"])
    async def echo(full_path: str) -> dict[str, Any]:
        return {"app": app_name, "path": f"/{full_path}"}

    return mock_app


class MultiAppTransport(httpx.AsyncBaseTransport):
    """Dispatches in-memory requests to different mock apps based on hostname."""

    def __init__(self, apps: dict[str, FastAPI]) -> None:
        self.transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self.transports:
            return await self.transports[host].handle_async_request(request)
        raise httpx.ConnectError(f"Host '{host}' unreachable", request=request)


# ---------------------------------------------------------------------------
# HAPPY-FLOW TESTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_robin_distribution():
    app1 = make_mock_app("instance-1")
    app2 = make_mock_app("instance-2")
    transport = MultiAppTransport({"backend-1": app1, "backend-2": app2})

    routes = [
        RouteDefinition(
            id="balanced-route",
            path_pattern="/balanced/**",
            methods={HTTPMethod.GET},
            upstream_pool={
                BackendInstance(url="http://backend-1"),
                BackendInstance(url="http://backend-2"),
            },
        )
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

    # 4 sequential requests should cleanly alternate 1, 2, 1, 2
    r1 = await client.get("/balanced/items")
    r2 = await client.get("/balanced/items")
    r3 = await client.get("/balanced/items")
    r4 = await client.get("/balanced/items")

    assert r1.status_code == 200
    assert r1.json()["app"] == "instance-1"

    assert r2.status_code == 200
    assert r2.json()["app"] == "instance-2"

    assert r3.status_code == 200
    assert r3.json()["app"] == "instance-1"

    assert r4.status_code == 200
    assert r4.json()["app"] == "instance-2"


def test_circuit_breaker_normal_operation_closed():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=2.0)
    assert cb.state == CircuitState.CLOSED
    assert cb.can_execute() is True

    # Record repeated successes
    for _ in range(5):
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.consecutive_failures == 0
        assert cb.can_execute() is True


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_recovery():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.05)

    # Trip to OPEN
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.can_execute() is False

    # Wait for recovery timeout
    await asyncio.sleep(0.06)

    # State transitions to HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.can_execute() is True

    # Canary probe succeeds -> reset to CLOSED
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0


@pytest.mark.asyncio
async def test_health_checker_probes_healthy_backends():
    app1 = make_mock_app("healthy-app")
    transport = httpx.ASGITransport(app=app1)
    client = httpx.AsyncClient(transport=transport, base_url="http://healthy-backend")

    instance = BackendInstance(url="http://healthy-backend", status=HealthStatus.UNKNOWN)
    route = RouteDefinition(
        id="healthy-route",
        path_pattern="/health-route",
        upstream_pool={instance},
    )
    registry = RouteRegistry(routes=[route])

    checker = HealthChecker(registry=registry, timeout_seconds=1.0)
    await checker.check_backend(client, instance)

    assert instance.status == HealthStatus.HEALTHY
    assert instance.consecutive_failures == 0
    assert instance.last_checked_at is not None


@pytest.mark.asyncio
async def test_admin_backends_endpoint_reports_status():
    routes = [
        RouteDefinition(
            id="pool-route",
            path_pattern="/pool",
            upstream_pool={
                BackendInstance(url="http://server-1:8000", weight=2),
                BackendInstance(url="http://server-2:8000", weight=1),
            },
        )
    ]
    registry = RouteRegistry(routes=routes)
    lb = LoadBalancer()
    gateway_app = create_app(registry=registry, load_balancer=lb)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )
    resp = await client.get("/admin/backends")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    urls = {item["url"] for item in data}
    assert urls == {"http://server-1:8000", "http://server-2:8000"}
    for item in data:
        assert item["status"] == "healthy"
        assert item["circuit_state"] == "closed"


# ---------------------------------------------------------------------------
# ERROR HANDLING & RESILIENCE TESTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failover_when_one_backend_is_unhealthy():
    app1 = make_mock_app("live-1")
    app2 = make_mock_app("live-2")
    transport = MultiAppTransport({"node-1": app1, "node-2": app2})

    node1 = BackendInstance(url="http://node-1", status=HealthStatus.UNHEALTHY)
    node2 = BackendInstance(url="http://node-2", status=HealthStatus.HEALTHY)

    routes = [
        RouteDefinition(
            id="failover-route",
            path_pattern="/failover/**",
            methods={HTTPMethod.GET},
            upstream_pool={node1, node2},
        )
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

    # Both requests must be routed to node-2 since node-1 is UNHEALTHY
    r1 = await client.get("/failover/test")
    r2 = await client.get("/failover/test")

    assert r1.status_code == 200
    assert r1.json()["app"] == "live-2"
    assert r2.status_code == 200
    assert r2.json()["app"] == "live-2"


def test_circuit_breaker_trips_to_open_after_consecutive_failures():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=10.0)

    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 1

    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 2

    # 3rd failure trips the breaker to OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.consecutive_failures == 3
    assert cb.can_execute() is False


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_probe_failure_trips_back_to_open():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.04)

    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    await asyncio.sleep(0.05)
    assert cb.state == CircuitState.HALF_OPEN

    # Canary probe fails
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.can_execute() is False


@pytest.mark.asyncio
async def test_all_backends_unhealthy_returns_503():
    dead_node1 = BackendInstance(url="http://dead-1", status=HealthStatus.UNHEALTHY)
    dead_node2 = BackendInstance(url="http://dead-2", status=HealthStatus.UNHEALTHY)

    routes = [
        RouteDefinition(
            id="dead-pool-route",
            path_pattern="/dead-pool/**",
            methods={HTTPMethod.GET},
            upstream_pool={dead_node1, dead_node2},
        )
    ]
    registry = RouteRegistry(routes=routes)
    lb = LoadBalancer()
    gateway_app = create_app(registry=registry, load_balancer=lb)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )

    resp = await client.get("/dead-pool/data")
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"] == "service_unavailable"
    assert "No healthy upstream backends" in data["message"]
    assert "X-Request-ID" in resp.headers


@pytest.mark.asyncio
async def test_route_with_no_upstream_url_or_pool_returns_503():
    empty_route = RouteDefinition(
        id="empty-route",
        path_pattern="/no-upstream",
        methods={HTTPMethod.GET},
        upstream_url="",
        upstream_pool=set(),
    )
    registry = RouteRegistry(routes=[empty_route])
    lb = LoadBalancer()
    gateway_app = create_app(registry=registry, load_balancer=lb)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )

    resp = await client.get("/no-upstream")
    assert resp.status_code == 503
    assert resp.json()["error"] == "service_unavailable"


@pytest.mark.asyncio
async def test_health_checker_marks_unhealthy_after_consecutive_failures():
    # Mock client that raises connection error
    mock_client = httpx.AsyncClient(transport=MultiAppTransport({}))

    instance = BackendInstance(url="http://unreachable-host", status=HealthStatus.HEALTHY)
    route = RouteDefinition(
        id="unreachable-route",
        path_pattern="/unreachable",
        upstream_pool={instance},
    )
    registry = RouteRegistry(routes=[route])

    checker = HealthChecker(registry=registry, failure_threshold=2)

    # 1st failure: still healthy
    await checker.check_backend(mock_client, instance)
    assert instance.consecutive_failures == 1
    assert instance.status == HealthStatus.HEALTHY

    # 2nd failure: transitions to UNHEALTHY
    await checker.check_backend(mock_client, instance)
    assert instance.consecutive_failures == 2
    assert instance.status == HealthStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_health_checker_recovers_unhealthy_backend():
    app = make_mock_app("recovering-app")
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://recovering-host")

    instance = BackendInstance(
        url="http://recovering-host",
        status=HealthStatus.UNHEALTHY,
        consecutive_failures=5,
    )
    route = RouteDefinition(
        id="recover-route",
        path_pattern="/recover",
        upstream_pool={instance},
    )
    registry = RouteRegistry(routes=[route])

    checker = HealthChecker(registry=registry)
    await checker.check_backend(client, instance)

    # Successful probe restores HEALTHY and resets consecutive failures
    assert instance.status == HealthStatus.HEALTHY
    assert instance.consecutive_failures == 0


def test_backend_instance_set_deduplication():
    b1 = BackendInstance(url="http://node-1:8000")
    b2 = BackendInstance(url="http://node-1:8000/")  # same normalized URL
    b3 = BackendInstance(url="http://node-2:8000")
    pool = {b1, b2, b3}
    assert len(pool) == 2

    route = RouteDefinition(
        id="dedup-route",
        path_pattern="/dedup",
        upstream_pool={b1, b2, b3},
    )
    assert len(route.upstream_pool) == 2
    assert isinstance(route.upstream_pool, set)


def test_route_definition_sets_validation():
    from gateway.models.route import AuthMiddlewareConfig, MiddlewareConfig, TransformConfig

    route = RouteDefinition(
        id="sets-test",
        path_pattern="/test",
        methods={HTTPMethod.GET, HTTPMethod.POST},
        middleware=MiddlewareConfig(
            auth=AuthMiddlewareConfig(
                enabled=True,
                allowed_roles={"admin", "superadmin"},
                required_scopes={"read:all", "write:all"},
            ),
            transform=TransformConfig(
                remove_request_headers={"x-foo", "x-bar"},
            ),
        ),
        upstream_pool={
            BackendInstance(url="http://node-1"),
            BackendInstance(url="http://node-2"),
        },
    )
    assert isinstance(route.methods, set)
    assert isinstance(route.upstream_pool, set)
    assert isinstance(route.middleware.auth.allowed_roles, set)
    assert isinstance(route.middleware.auth.required_scopes, set)
    assert isinstance(route.middleware.transform.remove_request_headers, set)


def test_pydantic_models_inheritance():
    import re

    from pydantic import BaseModel

    from gateway.discovery.circuit_breaker import CircuitBreaker
    from gateway.middleware.rate_limit import RateLimitResult
    from gateway.models.backend import BackendStatus
    from gateway.routing.matcher import CompiledRoute

    cb = CircuitBreaker(failure_threshold=5, recovery_timeout_seconds=2.0)
    assert isinstance(cb, BaseModel)
    assert cb.failure_threshold == 5

    rlr = RateLimitResult(allowed=True, limit=100, remaining=99, reset_epoch=12345, retry_after=0)
    assert isinstance(rlr, BaseModel)
    assert rlr.allowed is True

    status = BackendStatus(
        url="http://node:8080", status=HealthStatus.HEALTHY, circuit_state="closed"
    )
    assert isinstance(status, BaseModel)
    assert status.url == "http://node:8080"

    cr = CompiledRoute(
        route=RouteDefinition(path_pattern="/test"),
        regex=re.compile(r"^/test$"),
        specificity_score=10,
        param_names=[],
    )
    assert isinstance(cr, BaseModel)
    assert cr.specificity_score == 10


def test_empty_containers_default_never_none():
    """Verifies that all container fields default to empty collections (never None)."""
    import re

    from gateway.models.backend import TargetPool
    from gateway.models.context import GatewayContext
    from gateway.models.route import (
        AuthMiddlewareConfig,
        MatchResult,
        RouteDefinition,
        TransformConfig,
    )
    from gateway.routing.matcher import CompiledRoute

    route = RouteDefinition(path_pattern="/default-test")
    assert route.methods == set()
    assert route.upstream_pool == set()
    assert route.methods is not None
    assert route.upstream_pool is not None

    auth = AuthMiddlewareConfig()
    assert auth.allowed_roles == set()
    assert auth.required_scopes == set()
    assert auth.allowed_roles is not None
    assert auth.required_scopes is not None

    transform = TransformConfig()
    assert transform.add_request_headers == {}
    assert transform.remove_request_headers == set()
    assert transform.add_response_headers == {}
    assert transform.add_request_headers is not None
    assert transform.remove_request_headers is not None
    assert transform.add_response_headers is not None

    pool = TargetPool()
    assert pool.backends == set()
    assert pool.backends is not None

    ctx = GatewayContext()
    assert ctx.user_roles == set()
    assert ctx.auth_claims == {}
    assert ctx.state == {}
    assert ctx.user_roles is not None
    assert ctx.auth_claims is not None
    assert ctx.state is not None

    mr = MatchResult(route=route, target_path="/default-test")
    assert mr.path_params == {}
    assert mr.path_params is not None

    compiled = CompiledRoute(
        route=route,
        regex=re.compile(r"^/default-test$"),
        specificity_score=10,
    )
    assert compiled.param_names == []
    assert compiled.param_names is not None
