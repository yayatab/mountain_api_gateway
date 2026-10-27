from __future__ import annotations

from abc import ABC, abstractmethod

from gateway.models.route import RouteDefinition


class RouteStorage(ABC):
    """Abstract base class for persistent route storage backends."""

    @property
    @abstractmethod
    def backend_type(self) -> str:
        """Storage backend type identifier (e.g. 'file', 'mongodb')."""
        ...

    @abstractmethod
    async def initialize(self) -> None:
        """Establish connections, verify indexes, and seed initial routes if empty."""
        ...

    @abstractmethod
    async def load_all(self) -> list[RouteDefinition]:
        """Load all route definitions from persistent storage."""
        ...

    @abstractmethod
    async def save(self, route: RouteDefinition) -> None:
        """Persist or upsert a route definition."""
        ...

    @abstractmethod
    async def delete(self, route_id: str) -> bool:
        """Delete a route definition by ID. Returns True if deleted."""
        ...

    @abstractmethod
    async def get(self, route_id: str) -> RouteDefinition | None:
        """Retrieve a single route definition by ID."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Cleanly release client connections and resources."""
        ...
