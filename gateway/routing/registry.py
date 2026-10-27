import asyncio
import json
from pathlib import Path

from gateway.models.route import HttpMethod, MatchResult, RouteDefinition
from gateway.routing.matcher import RouteMatcher


class RouteRegistry:
    def __init__(self, routes: list[RouteDefinition] | None = None) -> None:
        self._lock = asyncio.Lock()
        self._routes: dict[str, RouteDefinition] = {}
        if routes:
            for route in routes:
                self._routes[route.id] = route
        self._matcher = RouteMatcher(list(self._routes.values()))

    def _rebuild_matcher(self) -> None:
        self._matcher = RouteMatcher(list(self._routes.values()))

    async def register(self, route: RouteDefinition) -> None:
        async with self._lock:
            self._routes[route.id] = route
            self._rebuild_matcher()

    async def update(self, route_id: str, route: RouteDefinition) -> None:
        async with self._lock:
            if route_id not in self._routes:
                raise KeyError(f"Route with id '{route_id}' not found")
            route.id = route_id
            self._routes[route_id] = route
            self._rebuild_matcher()

    async def delete(self, route_id: str) -> bool:
        async with self._lock:
            if route_id in self._routes:
                del self._routes[route_id]
                self._rebuild_matcher()
                return True
            return False

    async def get(self, route_id: str) -> RouteDefinition | None:
        return self._routes.get(route_id)

    async def list_all(self, include_disabled: bool = False) -> list[RouteDefinition]:
        if include_disabled:
            return list(self._routes.values())
        return [r for r in self._routes.values() if r.enabled]

    def find_match(self, method: str, path: str) -> MatchResult | None:
        return self._matcher.match(method=method, path=path)

    def get_allowed_methods(self, path: str) -> list[HttpMethod]:
        return self._matcher.get_allowed_methods_for_path(path)

    async def clear(self) -> None:
        async with self._lock:
            self._routes.clear()
            self._rebuild_matcher()

    async def save_to_file(self, file_path: str | Path) -> None:
        async with self._lock:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = [route.model_dump(mode="json") for route in self._routes.values()]
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def load_from_file(self, file_path: str | Path) -> None:
        async with self._lock:
            path = Path(file_path)
            if not path.exists():
                return
            content = path.read_text(encoding="utf-8")
            if not content.strip():
                return
            data = json.loads(content)
            routes: dict[str, RouteDefinition] = {}
            for item in data:
                route = RouteDefinition.model_validate(item)
                routes[route.id] = route
            self._routes = routes
            self._rebuild_matcher()
