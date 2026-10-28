from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from gateway.models.route import RouteDefinition
from gateway.storage.base import RouteStorage

logger = logging.getLogger("gateway.storage.file")


class FileRouteStorage(RouteStorage):
    """File-based route persistence backend with atomic disk writes."""

    def __init__(self, file_path: str | Path | None = None) -> None:
        self.file_path: Path | None = Path(file_path) if file_path else None
        self._lock = asyncio.Lock()

    @property
    def backend_type(self) -> str:
        return "file"

    async def initialize(self) -> None:
        if self.file_path and not self.file_path.exists():
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text("[]", encoding="utf-8")

    async def load_all(self) -> list[RouteDefinition]:
        if not self.file_path or not self.file_path.exists():
            return []

        async with self._lock:
            content = self.file_path.read_text(encoding="utf-8")
            if not content.strip():
                return []
            try:
                data = json.loads(content)
                return [RouteDefinition.model_validate(item) for item in data]
            except Exception as exc:
                logger.error("Failed to load routes from file '%s': %s", self.file_path, exc)
                return []

    async def save(self, route: RouteDefinition) -> None:
        if not self.file_path:
            return

        async with self._lock:
            routes = await self._read_routes_unlocked()
            routes[route.id] = route
            await self._write_routes_unlocked(routes)

    async def delete(self, route_id: str) -> bool:
        if not self.file_path:
            return False

        async with self._lock:
            routes = await self._read_routes_unlocked()
            if route_id in routes:
                del routes[route_id]
                await self._write_routes_unlocked(routes)
                return True
            return False

    async def get(self, route_id: str) -> RouteDefinition | None:
        if not self.file_path:
            return None

        async with self._lock:
            routes = await self._read_routes_unlocked()
            return routes.get(route_id)

    async def close(self) -> None:
        pass

    async def _read_routes_unlocked(self) -> dict[str, RouteDefinition]:
        if not self.file_path or not self.file_path.exists():
            return {}
        content = self.file_path.read_text(encoding="utf-8")
        if not content.strip():
            return {}
        try:
            data = json.loads(content)
            return {item["id"]: RouteDefinition.model_validate(item) for item in data}
        except Exception:
            return {}

    async def _write_routes_unlocked(self, routes: dict[str, RouteDefinition]) -> None:
        if not self.file_path:
            return
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        data = [r.model_dump(mode="json") for r in routes.values()]
        tmp_path = self.file_path.with_name(f"{self.file_path.name}.tmp-{uuid.uuid4().hex[:8]}")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            tmp_path.replace(self.file_path)
        except OSError:
            # Handles bind-mounted single files in Docker (Errno 16 Device or resource busy)
            self.file_path.write_text(tmp_path.read_text(encoding="utf-8"), encoding="utf-8")
            tmp_path.unlink(missing_ok=True)
