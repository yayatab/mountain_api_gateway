from __future__ import annotations

import asyncio
import json
import uuid
from http import HTTPMethod
from pathlib import Path

from gateway.models.route import MatchResult, RouteDefinition
from gateway.routing.matcher import RouteMatcher
from gateway.storage.base import RouteStorage
from gateway.storage.factory import create_route_storage
from gateway.storage.file import FileRouteStorage


class RouteRegistry:
    def __init__(
        self,
        routes: list[RouteDefinition] | None = None,
        persistence_path: str | Path | None = None,
        storage: RouteStorage | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._routes: dict[str, RouteDefinition] = {}

        if storage is not None:
            self.storage: RouteStorage = storage
        else:
            self.storage = create_route_storage(persistence_path=persistence_path)

        if routes:
            for route in routes:
                self._routes[route.id] = route
        self._matcher = RouteMatcher(list(self._routes.values()))

    @property
    def persistence_path(self) -> Path | None:
        if isinstance(self.storage, FileRouteStorage):
            return self.storage.file_path
        return None

    @persistence_path.setter
    def persistence_path(self, path: Path | str | None) -> None:
        self.storage = FileRouteStorage(file_path=path)

    def _rebuild_matcher(self) -> None:
        self._matcher = RouteMatcher(list(self._routes.values()))

    async def initialize(self) -> None:
        """Initializes storage backend and loads active routes into memory."""
        await self.storage.initialize()
        await self.load_from_storage()

    async def load_from_storage(self) -> list[RouteDefinition]:
        """Loads all stored routes into memory and updates matcher."""
        async with self._lock:
            stored_routes = await self.storage.load_all()
            self._routes = {r.id: r for r in stored_routes}
            self._rebuild_matcher()
            return list(self._routes.values())

    async def register(self, route: RouteDefinition) -> None:
        async with self._lock:
            self._routes[route.id] = route
            self._rebuild_matcher()
            await self.storage.save(route)

    async def update(self, route_id: str, route: RouteDefinition) -> None:
        async with self._lock:
            if route_id not in self._routes:
                raise KeyError(f"Route with id '{route_id}' not found")
            route.id = route_id
            self._routes[route_id] = route
            self._rebuild_matcher()
            await self.storage.save(route)

    async def delete(self, route_id: str) -> bool:
        async with self._lock:
            if route_id in self._routes:
                del self._routes[route_id]
                self._rebuild_matcher()
                await self.storage.delete(route_id)
                return True
            return False

    async def set_status(self, route_id: str, enabled: bool) -> RouteDefinition | None:
        async with self._lock:
            route = self._routes.get(route_id)
            if not route:
                return None
            route.enabled = enabled
            self._rebuild_matcher()
            await self.storage.save(route)
            return route

    async def get(self, route_id: str) -> RouteDefinition | None:
        return self._routes.get(route_id)

    async def exists(self, route_id: str) -> bool:
        return route_id in self._routes

    async def list_all(self, include_disabled: bool = False) -> list[RouteDefinition]:
        if include_disabled:
            return list(self._routes.values())
        return [r for r in self._routes.values() if r.enabled]

    def find_match(self, method: str, path: str) -> MatchResult | None:
        return self._matcher.match(method=method, path=path)

    def get_allowed_methods(self, path: str) -> set[HTTPMethod]:
        return self._matcher.get_allowed_methods_for_path(path)

    async def clear(self) -> None:
        async with self._lock:
            self._routes.clear()
            self._rebuild_matcher()

    async def save_to_file(self, file_path: str | Path) -> None:
        async with self._lock:
            path = Path(file_path)
            self.storage = FileRouteStorage(file_path=path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = [route.model_dump(mode="json") for route in self._routes.values()]
            tmp_path = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp_path.replace(path)

    async def load_from_file(self, file_path: str | Path) -> None:
        async with self._lock:
            path = Path(file_path)
            self.storage = FileRouteStorage(file_path=path)
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
