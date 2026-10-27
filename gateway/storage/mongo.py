from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase

from gateway.models.route import RouteDefinition
from gateway.storage.base import RouteStorage

logger = logging.getLogger("gateway.storage.mongo")


class MongoRouteStorage(RouteStorage):
    """MongoDB route persistence backend using asynchronous motor driver."""

    def __init__(
        self,
        uri: str = "mongodb://localhost:27017",
        database: str = "mountain_gateway",
        collection: str = "routes",
        timeout_ms: int = 3000,
        seed_routes_file: str | Path | None = None,
        seed_file: str | Path | None = None,
        client: AsyncIOMotorClient[Any] | None = None,
    ) -> None:
        self.uri = uri
        self.db_name = database
        self.database_name = database
        self.collection_name = collection
        self.timeout_ms = timeout_ms
        resolved_seed = seed_file or seed_routes_file
        self.seed_routes_file = Path(resolved_seed) if resolved_seed else None

        self._client: AsyncIOMotorClient[Any] = client or AsyncIOMotorClient(
            self.uri,
            serverSelectionTimeoutMS=self.timeout_ms,
        )
        self._db: AsyncIOMotorDatabase[Any] = self._client[self.db_name]
        self._collection: AsyncIOMotorCollection[Any] = self._db[self.collection_name]

    @property
    def backend_type(self) -> str:
        return "mongodb"

    @property
    def collection(self) -> AsyncIOMotorCollection[Any]:
        return self._collection

    async def initialize(self) -> None:
        """Pings MongoDB, establishes indexes, and seeds if collection is empty."""
        try:
            await self._client.admin.command("ping")
            logger.info("Connected to MongoDB at %s (db: %s)", self.uri, self.db_name)
        except Exception as exc:
            logger.warning("MongoDB ping failed (%s); will attempt runtime operations", exc)

        # Create indexes
        try:
            await self._collection.create_index("path_pattern")
            await self._collection.create_index("enabled")
        except Exception as exc:
            logger.warning("Failed to create MongoDB indexes: %s", exc)

        # Seed initial routes if collection is empty and seed file exists
        await self._seed_if_empty()

    async def _seed_if_empty(self) -> None:
        if not self.seed_routes_file or not self.seed_routes_file.exists():
            return

        try:
            count = await self._collection.count_documents({})
            if count == 0:
                content = self.seed_routes_file.read_text(encoding="utf-8")
                if content.strip():
                    seed_data = json.loads(content)
                    if isinstance(seed_data, list) and seed_data:
                        docs: list[dict[str, Any]] = []
                        for item in seed_data:
                            route = RouteDefinition.model_validate(item)
                            doc = route.model_dump(mode="json")
                            doc["_id"] = route.id
                            docs.append(doc)
                        if docs:
                            await self._collection.insert_many(docs)
                            logger.info(
                                "Seeded %d routes into MongoDB from '%s'",
                                len(docs),
                                self.seed_routes_file,
                            )
        except Exception as exc:
            logger.error("Failed to seed routes into MongoDB: %s", exc)

    async def load_all(self) -> list[RouteDefinition]:
        routes: list[RouteDefinition] = []
        try:
            cursor = self._collection.find({})
            async for doc in cursor:
                # Remove MongoDB _id before Pydantic validation if needed, or map to id
                if "_id" in doc and "id" not in doc:
                    doc["id"] = str(doc["_id"])
                routes.append(RouteDefinition.model_validate(doc))
        except Exception as exc:
            logger.error("Error loading routes from MongoDB: %s", exc)
        return routes

    async def save(self, route: RouteDefinition) -> None:
        try:
            doc = route.model_dump(mode="json")
            doc["_id"] = route.id
            await self._collection.replace_one({"_id": route.id}, doc, upsert=True)
            logger.debug("Persisted route '%s' to MongoDB", route.id)
        except Exception as exc:
            logger.error("Failed to save route '%s' to MongoDB: %s", route.id, exc)
            raise

    async def delete(self, route_id: str) -> bool:
        try:
            result = await self._collection.delete_one({"_id": route_id})
            return result.deleted_count > 0
        except Exception as exc:
            logger.error("Failed to delete route '%s' from MongoDB: %s", route_id, exc)
            return False

    async def get(self, route_id: str) -> RouteDefinition | None:
        try:
            doc = await self._collection.find_one({"_id": route_id})
            if not doc:
                return None
            if "_id" in doc and "id" not in doc:
                doc["id"] = str(doc["_id"])
            return RouteDefinition.model_validate(doc)
        except Exception as exc:
            logger.error("Failed to get route '%s' from MongoDB: %s", route_id, exc)
            return None

    async def close(self) -> None:
        self._client.close()
