from __future__ import annotations

import logging
from pathlib import Path

from gateway.config import settings
from gateway.storage.base import RouteStorage
from gateway.storage.file import FileRouteStorage
from gateway.storage.mongo import MongoRouteStorage

logger = logging.getLogger("gateway.storage.factory")


def create_route_storage(
    storage_type: str | None = None,
    persistence_path: str | Path | None = None,
    backend: str | None = None,
    mongodb_uri: str | None = None,
    mongodb_database: str | None = None,
    mongodb_collection: str | None = None,
    mongodb_timeout_ms: int | None = None,
    seed_routes_file: str | Path | None = None,
) -> RouteStorage:
    """Factory creating the appropriate RouteStorage backend."""
    backend_choice = (backend or storage_type or settings.storage_backend).lower().strip()

    if backend_choice == "mongodb":
        uri = mongodb_uri or settings.mongodb_uri
        db = mongodb_database or settings.mongodb_database
        col = mongodb_collection or settings.mongodb_collection
        timeout = (
            mongodb_timeout_ms if mongodb_timeout_ms is not None else settings.mongodb_timeout_ms
        )
        seed = seed_routes_file if seed_routes_file is not None else settings.seed_routes_file
        logger.info("Initializing MongoRouteStorage backend at %s (db: %s)", uri, db)
        return MongoRouteStorage(
            uri=uri,
            database=db,
            collection=col,
            timeout_ms=timeout,
            seed_routes_file=seed,
        )

    if backend_choice == "file":
        path = persistence_path if persistence_path is not None else settings.routes_file_path
        logger.info("Initializing FileRouteStorage backend at '%s'", path)
        return FileRouteStorage(file_path=path)

    raise ValueError(f"Unsupported storage backend: {backend_choice}")
