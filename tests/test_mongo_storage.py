"""Unit tests for MongoRouteStorage using mongomock-motor."""

import json
from http import HTTPMethod
from pathlib import Path
from unittest.mock import patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from gateway.models.route import RouteDefinition
from gateway.storage.mongo import MongoRouteStorage


@pytest.fixture
def mock_mongo_client():
    return AsyncMongoMockClient()


@pytest.fixture
def sample_route():
    return RouteDefinition(
        id="test-route-1",
        name="Test Route 1",
        path_pattern="/api/v1/test/**",
        methods={HTTPMethod.GET, HTTPMethod.POST},
        upstream_url="http://test-service:8080",
        enabled=True,
    )


@pytest.mark.asyncio
async def test_mongo_storage_save_and_get(mock_mongo_client, sample_route):
    storage = MongoRouteStorage(
        database="test_db",
        collection="test_routes",
        client=mock_mongo_client,
    )
    await storage.initialize()

    # Get non-existent
    assert await storage.get("non-existent") is None

    # Save
    await storage.save(sample_route)

    # Get
    retrieved = await storage.get("test-route-1")
    assert retrieved is not None
    assert retrieved.id == "test-route-1"
    assert retrieved.name == "Test Route 1"
    assert retrieved.path_pattern == "/api/v1/test/**"
    assert retrieved.methods == {HTTPMethod.GET, HTTPMethod.POST}

    await storage.close()


@pytest.mark.asyncio
async def test_mongo_storage_load_all_and_delete(mock_mongo_client, sample_route):
    storage = MongoRouteStorage(
        database="test_db",
        collection="test_routes",
        client=mock_mongo_client,
    )
    await storage.initialize()

    route2 = RouteDefinition(
        id="test-route-2",
        name="Test Route 2",
        path_pattern="/api/v2/**",
        methods={HTTPMethod.GET},
        upstream_url="http://test-service-2:8080",
        enabled=False,
    )

    await storage.save(sample_route)
    await storage.save(route2)

    routes = await storage.load_all()
    assert len(routes) == 2
    route_ids = {r.id for r in routes}
    assert route_ids == {"test-route-1", "test-route-2"}

    # Delete non-existent
    deleted = await storage.delete("unknown")
    assert deleted is False

    # Delete existent
    deleted = await storage.delete("test-route-1")
    assert deleted is True

    routes = await storage.load_all()
    assert len(routes) == 1
    assert routes[0].id == "test-route-2"

    await storage.close()


@pytest.mark.asyncio
async def test_mongo_storage_seeding(mock_mongo_client, tmp_path: Path):
    seed_file = tmp_path / "seed_routes.json"
    seed_data = [
        {
            "id": "seed-1",
            "name": "Seeded Route",
            "path_pattern": "/seeded/**",
            "methods": ["GET"],
            "upstream_url": "http://seed-service",
            "enabled": True,
        }
    ]
    seed_file.write_text(json.dumps(seed_data), encoding="utf-8")

    storage = MongoRouteStorage(
        database="test_db",
        collection="seed_routes",
        seed_routes_file=seed_file,
        client=mock_mongo_client,
    )

    # First initialize should seed
    await storage.initialize()
    routes = await storage.load_all()
    assert len(routes) == 1
    assert routes[0].id == "seed-1"

    # Second initialize on non-empty collection does not re-seed or fail
    await storage.initialize()
    routes = await storage.load_all()
    assert len(routes) == 1

    await storage.close()


@pytest.mark.asyncio
async def test_mongo_storage_missing_seed_file(mock_mongo_client, tmp_path: Path):
    missing_seed = tmp_path / "does_not_exist.json"
    storage = MongoRouteStorage(
        database="test_db",
        collection="test_missing_seed",
        seed_routes_file=missing_seed,
        client=mock_mongo_client,
    )
    # Missing seed file should log warning and not crash
    await storage.initialize()
    routes = await storage.load_all()
    assert len(routes) == 0

    await storage.close()


@pytest.mark.asyncio
async def test_mongo_storage_error_handling(mock_mongo_client, sample_route):
    storage = MongoRouteStorage(
        database="test_db",
        collection="test_errors",
        client=mock_mongo_client,
    )

    with patch.object(storage.collection, "find", side_effect=Exception("DB Failure")):
        routes = await storage.load_all()
        assert routes == []

    with (
        patch.object(storage.collection, "replace_one", side_effect=Exception("Write Failure")),
        pytest.raises(Exception, match="Write Failure"),
    ):
        await storage.save(sample_route)

    with patch.object(storage.collection, "find_one", side_effect=Exception("Read Failure")):
        result = await storage.get("test-route-1")
        assert result is None

    with patch.object(storage.collection, "delete_one", side_effect=Exception("Delete Failure")):
        deleted = await storage.delete("test-route-1")
        assert deleted is False

    await storage.close()
