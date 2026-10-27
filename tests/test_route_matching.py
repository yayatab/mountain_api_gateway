import asyncio
from http import HTTPMethod
from pathlib import Path

import pytest

from gateway.models.route import RouteDefinition
from gateway.routing.matcher import RouteMatcher
from gateway.routing.registry import RouteRegistry


class TestRouteMatcher:
    def test_exact_path_match(self):
        route = RouteDefinition(
            id="users-list",
            path_pattern="/api/v1/users",
            methods={HTTPMethod.GET, HTTPMethod.POST},
            upstream_url="http://user-service:8000",
        )
        matcher = RouteMatcher([route])

        result = matcher.match("GET", "/api/v1/users")
        assert result is not None
        assert result.route.id == "users-list"
        assert result.path_params == {}
        assert result.target_path == "/api/v1/users"

    def test_single_segment_wildcard(self):
        route = RouteDefinition(
            id="wildcard-items",
            path_pattern="/catalog/*",
            methods={HTTPMethod.GET},
            upstream_url="http://catalog-service:8000",
        )
        matcher = RouteMatcher([route])

        result = matcher.match("GET", "/catalog/electronics")
        assert result is not None
        assert result.route.id == "wildcard-items"

        # Should not match multi-segment path
        assert matcher.match("GET", "/catalog/electronics/laptops") is None

    def test_recursive_wildcard_glob(self):
        route = RouteDefinition(
            id="assets-glob",
            path_pattern="/static/**",
            methods={HTTPMethod.GET},
            upstream_url="http://static-service:8000",
        )
        matcher = RouteMatcher([route])

        result1 = matcher.match("GET", "/static/logo.png")
        assert result1 is not None

        result2 = matcher.match("GET", "/static/css/theme/dark.css")
        assert result2 is not None

    def test_path_parameter_extraction(self):
        route = RouteDefinition(
            id="order-detail",
            path_pattern="/orders/{order_id}/items/{item_id}",
            methods={HTTPMethod.GET},
            upstream_url="http://order-service:8000",
        )
        matcher = RouteMatcher([route])

        result = matcher.match("GET", "/orders/987/items/45")
        assert result is not None
        assert result.path_params == {"order_id": "987", "item_id": "45"}

    def test_specificity_ordering_exact_beats_wildcard(self):
        exact_route = RouteDefinition(
            id="exact",
            path_pattern="/api/v1/users",
            methods={HTTPMethod.GET},
            upstream_url="http://users-service:8000",
            priority=0,
        )
        wildcard_route = RouteDefinition(
            id="wildcard",
            path_pattern="/api/v1/*",
            methods={HTTPMethod.GET},
            upstream_url="http://wildcard-service:8000",
            priority=0,
        )
        glob_route = RouteDefinition(
            id="glob",
            path_pattern="/api/**",
            methods={HTTPMethod.GET},
            upstream_url="http://glob-service:8000",
            priority=0,
        )

        matcher = RouteMatcher([glob_route, wildcard_route, exact_route])
        result = matcher.match("GET", "/api/v1/users")
        assert result is not None
        assert result.route.id == "exact"

    def test_explicit_priority_overrides_specificity(self):
        exact_low_priority = RouteDefinition(
            id="exact-low",
            path_pattern="/api/v1/users",
            methods={HTTPMethod.GET},
            upstream_url="http://user-service:8000",
            priority=0,
        )
        glob_high_priority = RouteDefinition(
            id="glob-high",
            path_pattern="/api/**",
            methods={HTTPMethod.GET},
            upstream_url="http://override-service:8000",
            priority=10,
        )

        matcher = RouteMatcher([exact_low_priority, glob_high_priority])
        result = matcher.match("GET", "/api/v1/users")
        assert result is not None
        assert result.route.id == "glob-high"

    def test_method_filtering_and_allowed_methods(self):
        route = RouteDefinition(
            id="read-only",
            path_pattern="/api/v1/data",
            methods={HTTPMethod.GET, HTTPMethod.HEAD},
            upstream_url="http://data-service:8000",
        )
        matcher = RouteMatcher([route])

        assert matcher.match("GET", "/api/v1/data") is not None
        assert matcher.match("HEAD", "/api/v1/data") is not None
        assert matcher.match("DELETE", "/api/v1/data") is None

        allowed = matcher.get_allowed_methods_for_path("/api/v1/data")
        assert set(allowed) == {HTTPMethod.GET, HTTPMethod.HEAD}

    def test_path_normalization(self):
        route = RouteDefinition(
            id="normalize-test",
            path_pattern="/api/v1/reports",
            methods={HTTPMethod.GET},
            upstream_url="http://reports-service:8000",
        )
        matcher = RouteMatcher([route])

        assert matcher.match("GET", "/api/v1/reports/") is not None
        assert matcher.match("GET", "///api//v1//reports///") is not None

    def test_path_rewriting(self):
        route = RouteDefinition(
            id="rewrite-test",
            path_pattern="/gateway/v1/billing/**",
            methods={HTTPMethod.GET},
            upstream_url="http://billing-service:8000",
            strip_prefix="/gateway/v1",
            add_prefix="/internal",
        )
        matcher = RouteMatcher([route])

        result = matcher.match("GET", "/gateway/v1/billing/invoices/123")
        assert result is not None
        assert result.target_path == "/internal/billing/invoices/123"

    def test_disabled_route_ignored(self):
        route = RouteDefinition(
            id="disabled",
            path_pattern="/secret",
            upstream_url="http://secret-service:8000",
            enabled=False,
        )
        matcher = RouteMatcher([route])
        assert matcher.match("GET", "/secret") is None


class TestRouteRegistry:
    @pytest.mark.asyncio
    async def test_crud_operations(self):
        registry = RouteRegistry()

        route = RouteDefinition(
            id="route-1",
            path_pattern="/v1/status",
            methods={HTTPMethod.GET},
            upstream_url="http://status-service:8000",
        )

        # Create
        await registry.register(route)
        retrieved = await registry.get("route-1")
        assert retrieved is not None
        assert retrieved.upstream_url == "http://status-service:8000"

        # Match works
        match = registry.find_match("GET", "/v1/status")
        assert match is not None
        assert match.route.id == "route-1"

        # Update
        updated_route = RouteDefinition(
            id="route-1",
            path_pattern="/v1/status",
            methods={HTTPMethod.GET},
            upstream_url="http://new-status-service:8000",
        )
        await registry.update("route-1", updated_route)
        match_updated = registry.find_match("GET", "/v1/status")
        assert match_updated is not None
        assert match_updated.route.upstream_url == "http://new-status-service:8000"

        # Delete
        deleted = await registry.delete("route-1")
        assert deleted is True
        assert await registry.get("route-1") is None
        assert registry.find_match("GET", "/v1/status") is None

    @pytest.mark.asyncio
    async def test_file_persistence_roundtrip(self, tmp_path: Path):
        file_path = tmp_path / "routes.json"
        registry1 = RouteRegistry()

        r1 = RouteDefinition(
            id="r1",
            path_pattern="/service1/**",
            upstream_url="http://svc1:8000",
        )
        r2 = RouteDefinition(
            id="r2",
            path_pattern="/service2/*",
            upstream_url="http://svc2:8000",
            enabled=False,
        )
        await registry1.register(r1)
        await registry1.register(r2)

        await registry1.save_to_file(file_path)
        assert file_path.exists()

        registry2 = RouteRegistry()
        await registry2.load_from_file(file_path)

        all_routes = await registry2.list_all(include_disabled=True)
        assert len(all_routes) == 2
        active_routes = await registry2.list_all(include_disabled=False)
        assert len(active_routes) == 1
        assert active_routes[0].id == "r1"

    @pytest.mark.asyncio
    async def test_concurrent_mutations(self):
        registry = RouteRegistry()

        async def register_route(index: int):
            route = RouteDefinition(
                id=f"concurrent-{index}",
                path_pattern=f"/endpoint/{index}",
                upstream_url=f"http://service-{index}:8000",
            )
            await registry.register(route)

        await asyncio.gather(*(register_route(i) for i in range(50)))

        routes = await registry.list_all()
        assert len(routes) == 50

        match = registry.find_match("GET", "/endpoint/25")
        assert match is not None
        assert match.route.id == "concurrent-25"

    @pytest.mark.asyncio
    async def test_registry_load_missing_file_noop(self, tmp_path: Path):
        registry = RouteRegistry()
        missing = tmp_path / "does_not_exist.json"
        await registry.load_from_file(missing)
        assert len(await registry.list_all()) == 0

    @pytest.mark.asyncio
    async def test_registry_load_empty_file_noop(self, tmp_path: Path):
        registry = RouteRegistry()
        empty_file = tmp_path / "empty.json"
        empty_file.write_text("   \n  ", encoding="utf-8")
        await registry.load_from_file(empty_file)
        assert len(await registry.list_all()) == 0

    @pytest.mark.asyncio
    async def test_registry_update_nonexistent_raises(self):
        registry = RouteRegistry()
        route = RouteDefinition(
            id="nope",
            path_pattern="/test",
            upstream_url="http://localhost:8000",
        )
        with pytest.raises(KeyError, match="Route with id 'nope' not found"):
            await registry.update("nope", route)

    @pytest.mark.asyncio
    async def test_registry_clear(self):
        registry = RouteRegistry()
        route = RouteDefinition(
            id="to-clear",
            path_pattern="/clear-me",
            upstream_url="http://localhost:8000",
        )
        await registry.register(route)
        assert len(await registry.list_all()) == 1
        await registry.clear()
        assert len(await registry.list_all()) == 0
        assert registry.find_match("GET", "/clear-me") is None

    def test_route_matcher_normalization_edge_cases(self):
        matcher = RouteMatcher()
        assert matcher._normalize_path("") == "/"
        assert matcher._normalize_path("users/me") == "/users/me"
        assert matcher._normalize_path("///api///v1////") == "/api/v1"

    def test_route_matcher_empty_methods_allows_all(self):
        route = RouteDefinition(
            id="all-methods",
            path_pattern="/open",
            methods=set(),
            upstream_url="http://localhost:8000",
        )
        matcher = RouteMatcher([route])
        allowed = matcher.get_allowed_methods_for_path("/open")
        assert allowed == set(HTTPMethod)
