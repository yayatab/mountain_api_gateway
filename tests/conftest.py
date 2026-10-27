import pytest

from gateway.models.route import (
    HttpMethod,
    RouteDefinition,
)
from gateway.routing.matcher import RouteMatcher
from gateway.routing.registry import RouteRegistry


@pytest.fixture
def sample_routes() -> list[RouteDefinition]:
    return [
        RouteDefinition(
            id="exact-users",
            name="Exact Users",
            path_pattern="/api/v1/users",
            methods={HttpMethod.GET, HttpMethod.POST},
            upstream_url="http://user-service:8000",
            priority=0,
        ),
        RouteDefinition(
            id="user-by-id",
            name="User By ID",
            path_pattern="/api/v1/users/{id}",
            methods={HttpMethod.GET},
            upstream_url="http://user-service:8000",
            priority=0,
        ),
        RouteDefinition(
            id="wildcard-api",
            name="Wildcard API",
            path_pattern="/api/v1/*",
            methods={HttpMethod.GET},
            upstream_url="http://catchall-service:8000",
            priority=0,
        ),
        RouteDefinition(
            id="static-assets",
            name="Static Assets Glob",
            path_pattern="/static/**",
            methods={HttpMethod.GET},
            upstream_url="http://cdn-service:8000",
            priority=0,
        ),
        RouteDefinition(
            id="high-priority-override",
            name="High Priority Override",
            path_pattern="/api/**",
            methods={HttpMethod.GET},
            upstream_url="http://priority-service:8000",
            priority=100,
        ),
        RouteDefinition(
            id="rewritten-route",
            name="Rewritten Route",
            path_pattern="/service/orders/**",
            methods={HttpMethod.GET, HttpMethod.POST},
            upstream_url="http://order-service:8000",
            strip_prefix="/service",
            add_prefix="/v2",
        ),
        RouteDefinition(
            id="disabled-route",
            name="Disabled Route",
            path_pattern="/disabled/endpoint",
            upstream_url="http://disabled-service:8000",
            enabled=False,
        ),
    ]


@pytest.fixture
def registry(sample_routes: list[RouteDefinition]) -> RouteRegistry:
    return RouteRegistry(routes=sample_routes)


@pytest.fixture
def matcher(sample_routes: list[RouteDefinition]) -> RouteMatcher:
    return RouteMatcher(routes=sample_routes)
