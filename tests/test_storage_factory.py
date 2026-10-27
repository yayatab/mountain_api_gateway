"""Tests for route storage factory and registry integration."""

from http import HTTPMethod
from pathlib import Path

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from gateway.main import create_app
from gateway.models.route import RouteDefinition
from gateway.routing.registry import RouteRegistry
from gateway.storage.factory import create_route_storage
from gateway.storage.file import FileRouteStorage
from gateway.storage.mongo import MongoRouteStorage


def test_factory_creates_file_storage(tmp_path: Path):
    path = tmp_path / "routes.json"
    storage = create_route_storage(backend="file", persistence_path=path)
    assert isinstance(storage, FileRouteStorage)
    assert storage.backend_type == "file"
    assert storage.file_path == path


def test_factory_creates_mongo_storage():
    storage = create_route_storage(
        backend="mongodb",
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database="custom_db",
        mongodb_collection="custom_col",
    )
    assert isinstance(storage, MongoRouteStorage)
    assert storage.backend_type == "mongodb"
    assert storage.db_name == "custom_db"
    assert storage.collection_name == "custom_col"


def test_factory_invalid_backend():
    with pytest.raises(ValueError, match="Unsupported storage backend: invalid"):
        create_route_storage(backend="invalid")


@pytest.mark.asyncio
async def test_registry_with_mongo_storage_crud_and_reload():
    client = AsyncMongoMockClient()
    storage = MongoRouteStorage(
        database="test_gateway",
        collection="test_routes",
        client=client,
    )
    registry = RouteRegistry(storage=storage)
    await registry.initialize()

    # Register route
    route = RouteDefinition(
        id="mongo-dyn-1",
        name="Dynamic Mongo Route",
        path_pattern="/mongo/test/**",
        methods={HTTPMethod.GET},
        upstream_url="http://echo-service",
        enabled=True,
    )
    await registry.register(route)

    # Verify present in registry
    assert await registry.exists("mongo-dyn-1")
    retrieved_route = await registry.get("mongo-dyn-1")
    assert retrieved_route is not None
    assert retrieved_route.name == "Dynamic Mongo Route"

    # Verify persisted in mongo
    stored_doc = await storage.get("mongo-dyn-1")
    assert stored_doc is not None
    assert stored_doc.id == "mongo-dyn-1"

    # Toggle status
    updated = await registry.set_status("mongo-dyn-1", False)
    assert updated is not None
    assert updated.enabled is False
    updated_doc = await storage.get("mongo-dyn-1")
    assert updated_doc is not None
    assert updated_doc.enabled is False

    # Simulate fresh registry loading from same storage
    fresh_registry = RouteRegistry(storage=storage)
    await fresh_registry.initialize()
    loaded_routes = await fresh_registry.list_all(include_disabled=True)
    assert len(loaded_routes) == 1
    assert loaded_routes[0].id == "mongo-dyn-1"
    assert loaded_routes[0].enabled is False

    # Delete route
    deleted = await fresh_registry.delete("mongo-dyn-1")
    assert deleted is True
    assert not await fresh_registry.exists("mongo-dyn-1")
    assert await storage.get("mongo-dyn-1") is None

    await storage.close()


@pytest.mark.asyncio
async def test_admin_api_reload_with_mongo_storage():
    client = AsyncMongoMockClient()
    storage = MongoRouteStorage(
        database="test_gateway",
        collection="test_routes",
        client=client,
    )
    registry = RouteRegistry(storage=storage)
    await registry.initialize()

    app = create_app(registry=registry)
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    )

    # Initially empty
    reload_resp = await http_client.post("/admin/routes/reload")
    assert reload_resp.status_code == 200
    assert reload_resp.json()["reloaded_count"] == 0
    assert "mongodb:test_gateway.test_routes" in reload_resp.json()["source"]

    # Insert a route directly in mongo storage
    new_route = RouteDefinition(
        id="ext-route-1",
        name="External Route",
        path_pattern="/ext/**",
        methods={HTTPMethod.GET},
        upstream_url="http://ext-service",
        enabled=True,
    )
    await storage.save(new_route)

    # Call reload via admin API
    reload_resp2 = await http_client.post("/admin/routes/reload")
    assert reload_resp2.status_code == 200
    assert reload_resp2.json()["reloaded_count"] == 1

    # Verify route is loaded in memory
    assert await registry.exists("ext-route-1")

    await storage.close()
