"""Automatic tests that verify live Redis and MongoDB database interactions.

These tests connect to real instances of Redis and MongoDB (e.g. running via Docker),
execute operations through the gateway pipeline and Admin API, and then inspect
the database engines directly to assert that records, keys, indexes, and states
are physically stored and updated.
"""

from __future__ import annotations

import uuid
from http import HTTPMethod
from pathlib import Path

import httpx
import pytest
import redis.asyncio as aioredis
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.discovery.load_balancer import LoadBalancer
from gateway.main import create_app
from gateway.middleware.base import MiddlewarePipeline
from gateway.middleware.rate_limit import RateLimitMiddleware
from gateway.models.context import GatewayContext
from gateway.models.route import (
    MiddlewareConfig,
    RateLimitConfig,
    RouteDefinition,
)
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry
from gateway.storage.mongo import MongoRouteStorage

REDIS_URI = "redis://localhost:6379/0"
MONGO_URI = "mongodb://localhost:27017"


async def _is_redis_available() -> bool:
    try:
        client = aioredis.from_url(REDIS_URI, socket_timeout=1.0)
        pong = await client.ping()
        await client.aclose()
        return bool(pong)
    except Exception:
        return False


async def _is_mongo_available() -> bool:
    try:
        client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=1000)
        res = await client.admin.command("ping")
        client.close()
        return bool(res.get("ok"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1. LIVE REDIS RATE LIMITING TEST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_redis_rate_limiting_stores_and_expires_keys():
    """Verifies that RateLimitMiddleware actually writes to and queries Redis.

    Directly inspects Redis keys, ZSET timestamps, TTLs, and limits.
    """
    if not await _is_redis_available():
        pytest.skip("Live Redis is not available on localhost:6379")

    raw_redis = aioredis.from_url(REDIS_URI)
    test_id = uuid.uuid4().hex[:8]
    route_id = f"live-redis-route-{test_id}"

    # Target route with a tight limit: 3 requests per 60s
    route = RouteDefinition(
        id=route_id,
        name="Live Redis Rate Limited Route",
        path_pattern="/live-limited/**",
        methods={HTTPMethod.GET},
        upstream_url="http://echo-service",
        middleware_chain=["rate_limit"],
        middleware=MiddlewareConfig(
            rate_limit=RateLimitConfig(
                enabled=True,
                requests_per_minute=3,
                burst_capacity=3,
                key_by="ip",
            )
        ),
    )

    # Initialize rate limit middleware pointing to real Redis
    rate_limit_mw = RateLimitMiddleware(redis_client=raw_redis)

    try:
        # Construct pipeline and dummy downstream handler
        pipeline = MiddlewarePipeline(
            middlewares=[rate_limit_mw],
            named_middlewares={"rate_limit": rate_limit_mw},
        )

        async def dummy_downstream(req: Request, ctx: GatewayContext):
            return JSONResponse({"status": "ok", "client_ip": ctx.client_ip})

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/live-limited/resource",
            "headers": [],
        }

        # Send 3 allowed requests with client IP 192.168.10.50
        for i in range(3):
            ctx = GatewayContext(
                request_id=f"req-{i}",
                client_ip="192.168.10.50",
                matched_route=route,
            )
            req = Request(scope)
            resp = await pipeline.execute(req, ctx, dummy_downstream)
            assert resp.status_code == 200, f"Request {i + 1} should have succeeded"

        # DIRECT DATABASE INSPECTION: Verify keys physically exist in Redis
        expected_key = f"rl:{route_id}:ip:192.168.10.50"
        key_exists = await raw_redis.exists(expected_key)
        assert key_exists == 1, f"Expected key '{expected_key}' to exist in live Redis"

        # Check that Redis contains exactly 3 ZSET entries
        card = await raw_redis.zcard(expected_key)
        assert card == 3, f"Expected 3 entries in Redis ZSET, got {card}"

        # Check key TTL is set by Redis pipeline
        ttl = await raw_redis.ttl(expected_key)
        assert 0 < ttl <= 120, f"Expected TTL between 1 and 120 seconds, got {ttl}"

        # Send 4th request: Must exceed limit and return 429
        ctx_4 = GatewayContext(
            request_id="req-4",
            client_ip="192.168.10.50",
            matched_route=route,
        )
        req_4 = Request(scope)
        resp_4 = await pipeline.execute(req_4, ctx_4, dummy_downstream)
        assert resp_4.status_code == 429, "Expected 4th request to trip rate limit"
        assert "Retry-After" in resp_4.headers
        assert int(resp_4.headers["Retry-After"]) >= 1

    finally:
        # Cleanup test keys in Redis
        keys = await raw_redis.keys(f"rl:{route_id}:*")
        if keys:
            await raw_redis.delete(*keys)
        await raw_redis.aclose()


# ---------------------------------------------------------------------------
# 2. LIVE MONGODB STORAGE & ADMIN API LIFECYCLE TEST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_mongodb_route_storage_and_admin_lifecycle(tmp_path: Path):
    """Verifies that the Gateway physically loads from and persists to MongoDB.

    Directly inspects MongoDB collections, indexes, and document mutations.
    """
    if not await _is_mongo_available():
        pytest.skip("Live MongoDB is not available on localhost:27017")

    db_name = f"test_gateway_{uuid.uuid4().hex[:8]}"
    col_name = "routes"
    raw_mongo = AsyncIOMotorClient(MONGO_URI)
    db = raw_mongo[db_name]
    collection = db[col_name]

    # Seed file for initial load
    seed_file = tmp_path / "seed_routes.json"
    seed_file.write_text(
        """[
        {
            "id": "seeded-live-route",
            "name": "Seeded Live Route",
            "path_pattern": "/live-seeded/**",
            "methods": ["GET"],
            "upstream_url": "http://echo-service",
            "enabled": true
        }
    ]""",
        encoding="utf-8",
    )

    storage = MongoRouteStorage(
        uri=MONGO_URI,
        database=db_name,
        collection=col_name,
        seed_routes_file=seed_file,
    )

    try:
        # Initialize storage: establishes indexes and seeds collection
        await storage.initialize()

        # DIRECT DATABASE INSPECTION 1: Verify indexes created in MongoDB
        index_info = await collection.index_information()
        assert "path_pattern_1" in index_info, "path_pattern index missing in MongoDB"
        assert "enabled_1" in index_info, "enabled index missing in MongoDB"

        # DIRECT DATABASE INSPECTION 2: Verify seed document in MongoDB
        seed_count = await collection.count_documents({})
        assert seed_count == 1
        seeded_doc = await collection.find_one({"_id": "seeded-live-route"})
        assert seeded_doc is not None
        assert seeded_doc["name"] == "Seeded Live Route"

        # Start Gateway with this live Mongo storage
        registry = RouteRegistry(storage=storage)
        await registry.initialize()

        app = create_app(registry=registry)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        )

        # 1. CREATE ROUTE VIA ADMIN API
        new_route_payload = {
            "id": "dynamic-live-mongo-route",
            "name": "Dynamic Live Mongo Route",
            "path_pattern": "/dynamic-mongo/**",
            "methods": ["GET"],
            "upstream_url": "http://echo-service",
            "enabled": True,
        }
        create_resp = await client.post("/admin/routes", json=new_route_payload)
        assert create_resp.status_code == 201

        # DIRECT DATABASE INSPECTION 3: Verify document physically inserted into MongoDB
        stored_doc = await collection.find_one({"_id": "dynamic-live-mongo-route"})
        assert stored_doc is not None
        assert stored_doc["id"] == "dynamic-live-mongo-route"
        assert stored_doc["path_pattern"] == "/dynamic-mongo/**"
        assert stored_doc["enabled"] is True

        # 2. TOGGLE STATUS VIA ADMIN API
        patch_resp = await client.patch(
            "/admin/routes/dynamic-live-mongo-route/status",
            json={"enabled": False},
        )
        assert patch_resp.status_code == 200

        # DIRECT DATABASE INSPECTION 4: Verify document updated in MongoDB
        updated_doc = await collection.find_one({"_id": "dynamic-live-mongo-route"})
        assert updated_doc is not None
        assert updated_doc["enabled"] is False

        # 3. HOT RELOAD FROM MONGODB VIA ADMIN API
        # Insert a route directly into MongoDB from external client
        external_route = {
            "_id": "external-mongo-route",
            "id": "external-mongo-route",
            "name": "External Route",
            "path_pattern": "/external-live/**",
            "methods": ["GET"],
            "upstream_url": "http://echo-service",
            "enabled": True,
        }
        await collection.insert_one(external_route)

        # Trigger reload via Admin API
        reload_resp = await client.post("/admin/routes/reload")
        assert reload_resp.status_code == 200
        data = reload_resp.json()
        assert data["success"] is True
        assert data["reloaded_count"] == 3  # seeded + dynamic + external
        assert f"mongodb:{db_name}.{col_name}" in data["source"]

        # Verify registry loaded the external route into memory
        assert await registry.exists("external-mongo-route")

        # 4. DELETE ROUTE VIA ADMIN API
        del_resp = await client.delete("/admin/routes/dynamic-live-mongo-route")
        assert del_resp.status_code == 200

        # DIRECT DATABASE INSPECTION 5: Verify document deleted from MongoDB
        deleted_doc = await collection.find_one({"_id": "dynamic-live-mongo-route"})
        assert deleted_doc is None

    finally:
        # Clean up test database
        await raw_mongo.drop_database(db_name)
        await storage.close()
        raw_mongo.close()


# ---------------------------------------------------------------------------
# 3. END-TO-END GATEWAY WITH BOTH LIVE REDIS AND LIVE MONGODB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_gateway_traffic_with_both_live_databases(tmp_path: Path):
    """Verifies complete end-to-end gateway proxy traffic when backed simultaneously

    by live MongoDB for routing and live Redis for distributed rate limiting.
    """
    if not await _is_redis_available() or not await _is_mongo_available():
        pytest.skip("Both live Redis and live MongoDB are required for this test")

    test_id = uuid.uuid4().hex[:8]
    db_name = f"test_gateway_e2e_{test_id}"
    col_name = "routes"
    raw_redis = aioredis.from_url(REDIS_URI)
    raw_mongo = AsyncIOMotorClient(MONGO_URI)

    # Route in MongoDB configured with rate limiting
    route = RouteDefinition(
        id=f"e2e-live-route-{test_id}",
        name="E2E Live Route",
        path_pattern="/e2e-live/**",
        methods={HTTPMethod.GET},
        upstream_url="http://echo-service",
        strip_prefix="/e2e-live",
        middleware_chain=["rate_limit"],
        middleware=MiddlewareConfig(
            rate_limit=RateLimitConfig(
                enabled=True,
                requests_per_minute=2,
                burst_capacity=2,
                key_by="ip",
            )
        ),
    )

    storage = MongoRouteStorage(uri=MONGO_URI, database=db_name, collection=col_name)
    await storage.initialize()
    await storage.save(route)

    # Initialize gateway with live mongo storage and live redis rate limiting
    registry = RouteRegistry(storage=storage)
    await registry.initialize()

    rl_mw = RateLimitMiddleware(redis_client=raw_redis)

    pipeline = MiddlewarePipeline(
        middlewares=[rl_mw],
        named_middlewares={"rate_limit": rl_mw},
    )

    # Mock echo upstream backend
    async def mock_echo_app(scope, receive, send):
        resp = JSONResponse({"echo": "success", "path": scope["path"]})
        await resp(scope, receive, send)

    mock_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_echo_app),
        base_url="http://echo-service",
    )
    proxy_engine = ProxyEngine(client=mock_client)
    load_balancer = LoadBalancer()

    app = create_app(
        registry=registry,
        proxy_engine=proxy_engine,
        pipeline=pipeline,
        load_balancer=load_balancer,
    )

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    )

    try:
        # Request 1: 200 OK
        r1 = await client.get("/e2e-live/resource")
        assert r1.status_code == 200
        assert r1.json()["echo"] == "success"

        # Request 2: 200 OK
        r2 = await client.get("/e2e-live/resource")
        assert r2.status_code == 200

        # Request 3: 429 Too Many Requests (enforced by live Redis Lua script)
        r3 = await client.get("/e2e-live/resource")
        assert r3.status_code == 429
        assert "Retry-After" in r3.headers

        # DIRECT INSPECTION: Verify Redis keys and Mongo document exist
        mongo_doc = await raw_mongo[db_name][col_name].find_one({"_id": route.id})
        assert mongo_doc is not None
        assert mongo_doc["name"] == "E2E Live Route"

        redis_keys = await raw_redis.keys(f"rl:{route.id}:*")
        assert len(redis_keys) == 1

    finally:
        # Clean up live databases
        await raw_mongo.drop_database(db_name)
        await storage.close()
        raw_mongo.close()

        keys = await raw_redis.keys(f"rl:{route.id}:*")
        if keys:
            await raw_redis.delete(*keys)
        await raw_redis.aclose()


# ---------------------------------------------------------------------------
# 4. SYSTEM TEST SCENARIOS EXECUTED AGAINST LIVE DATABASES
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_scenarios_run_against_live_databases():
    """Verifies that SystemTestRunner executes scenarios directly backed by live

    Redis and live MongoDB, physically creating keys in Redis and documents in Mongo.
    """
    if not await _is_redis_available() or not await _is_mongo_available():
        pytest.skip("Both live Redis and live MongoDB are required for this test")

    import yaml

    from tests.system_test.models import SystemTestScenario
    from tests.system_test.runner import SystemTestRunner

    raw_redis = aioredis.from_url(REDIS_URI)
    raw_mongo = AsyncIOMotorClient(MONGO_URI)
    db_name = f"test_system_live_{uuid.uuid4().hex[:8]}"

    try:
        # 1. Execute Rate Limiting scenario against live Redis
        raw_03 = yaml.safe_load(
            Path("tests/system_test/scenarios/03_rate_limiting.yaml").read_text()
        )
        scenario_03 = SystemTestScenario.model_validate(raw_03)
        runner_03 = SystemTestRunner(scenario_03)
        await runner_03.run(redis_client=raw_redis)

        # Direct Redis check: verify rate limit keys were created in live Redis
        keys = await raw_redis.keys("rl:*")
        assert len(keys) > 0, "Expected rate limit keys in live Redis after running scenario 03"

        # 2. Execute Dynamic Route Storage scenario against live MongoDB
        raw_12 = yaml.safe_load(
            Path("tests/system_test/scenarios/12_dynamic_route_storage.yaml").read_text()
        )
        scenario_12 = SystemTestScenario.model_validate(raw_12)
        storage_12 = MongoRouteStorage(uri=MONGO_URI, database=db_name, collection="routes")
        runner_12 = SystemTestRunner(scenario_12)
        await runner_12.run(storage=storage_12)

        # Direct Mongo check: verify base route exists in live MongoDB
        base_doc = await raw_mongo[db_name]["routes"].find_one({"_id": "base-echo-route"})
        assert base_doc is not None, "Expected base route to be persisted in live MongoDB"

        await storage_12.close()
    finally:
        await raw_mongo.drop_database(db_name)
        raw_mongo.close()
        keys = await raw_redis.keys("rl:*")
        if keys:
            await raw_redis.delete(*keys)
        await raw_redis.aclose()
