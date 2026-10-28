"""End-to-End automated test suite verifying all services and middlewares

running inside the live Docker container stack.

Target environment:
  - Gateway: http://localhost:8000
  - Redis: localhost:6379
  - MongoDB: localhost:27017
  - Upstream services: Echo (:8001), Slow (:8002), Flaky (:8003), Hasher (:8004), Pipeline (:8005)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import httpx
import pytest
import redis.asyncio as aioredis
from motor.motor_asyncio import AsyncIOMotorClient

from gateway.config import settings
from scripts.generate_tokens import generate_token

GATEWAY_BASE = "http://localhost:8000"
REDIS_URI = "redis://localhost:6379/0"
MONGO_URI = "mongodb://localhost:27017"


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


async def check_container_stack_online() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{GATEWAY_BASE}/admin/health")
            return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture(autouse=True, scope="module")
async def require_container_stack():
    if not await check_container_stack_online():
        pytest.skip(
            "Containerized gateway is not running on http://localhost:8000. "
            "Start the stack with: docker compose up -d"
        )


@pytest.fixture
def http_client():
    return httpx.AsyncClient(base_url=GATEWAY_BASE, timeout=10.0)


# ===========================================================================
# 1. MIDDLEWARE: METRICS, TRACING & TIMING HEADERS
# ===========================================================================


@pytest.mark.asyncio
async def test_middleware_timing_and_trace_headers(http_client: httpx.AsyncClient):
    """Verifies MetricsMiddleware and Context injects X-Request-ID, X-Response-Time,

    and Server-Timing headers on all proxied responses.
    """
    resp = await http_client.get("/echo/headers-test")
    assert resp.status_code == 200
    assert "x-request-id" in resp.headers
    assert "x-response-time" in resp.headers
    assert "server-timing" in resp.headers
    assert "gateway;dur=" in resp.headers["server-timing"]

    # Verify custom X-Request-ID echo
    custom_id = f"test-trace-{uuid.uuid4().hex[:8]}"
    resp_custom = await http_client.get("/echo/headers-test", headers={"X-Request-ID": custom_id})
    assert resp_custom.headers["x-request-id"] == custom_id


@pytest.mark.asyncio
async def test_middleware_prometheus_metrics(http_client: httpx.AsyncClient):
    """Verifies Prometheus metrics endpoint exports request counts and durations."""
    resp = await http_client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert "gateway_requests_total" in text
    assert "gateway_request_duration_seconds" in text


# ===========================================================================
# 2. MIDDLEWARE: JWT AUTH & RBAC ENFORCEMENT
# ===========================================================================


@pytest.mark.asyncio
async def test_middleware_jwt_auth_and_rbac(http_client: httpx.AsyncClient):
    """Verifies JWT Auth middleware enforcement on containerized gateway."""
    # 1. Missing Token -> 401
    resp_anon = await http_client.get("/secure/admin-dashboard")
    assert resp_anon.status_code == 401
    assert resp_anon.json()["error"] == "unauthorized"

    # 2. Valid Admin Token -> 200 & upstream header injection
    admin_token = generate_token(
        sub="alice",
        roles=["admin"],
        scopes=["read:reports"],
        exp_minutes=15,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    resp_admin = await http_client.get(
        "/secure/admin-dashboard",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp_admin.status_code == 200
    echo_data = resp_admin.json()
    assert echo_data["headers"]["x-user-id"] == "alice"
    assert "admin" in echo_data["headers"]["x-user-roles"]

    # 3. Insufficient Role -> 403 Forbidden
    viewer_token = generate_token(
        sub="bob",
        roles=["viewer"],
        scopes=["read:reports"],
        exp_minutes=15,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    resp_viewer = await http_client.get(
        "/secure/admin-dashboard",
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert resp_viewer.status_code == 403
    assert resp_viewer.json()["error"] == "forbidden"

    # 4. Expired Token -> 401
    expired_token = generate_token(
        sub="eve",
        roles=["admin"],
        scopes=[],
        exp_minutes=-10,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    resp_expired = await http_client.get(
        "/secure/admin-dashboard",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert resp_expired.status_code == 401
    assert "expired" in resp_expired.text.lower()

    # 5. Tampered Secret -> 401
    tampered_token = generate_token(
        sub="mallory",
        roles=["admin"],
        scopes=[],
        exp_minutes=15,
        secret="wrong-secret-key-that-does-not-match-gateway",
        algorithm=settings.jwt_algorithm,
    )
    resp_tampered = await http_client.get(
        "/secure/admin-dashboard",
        headers={"Authorization": f"Bearer {tampered_token}"},
    )
    assert resp_tampered.status_code == 401


# ===========================================================================
# 3. MIDDLEWARE: REDIS DISTRIBUTED RATE LIMITING
# ===========================================================================


@pytest.mark.asyncio
async def test_middleware_redis_rate_limiting_in_container(http_client: httpx.AsyncClient):
    """Verifies that RateLimitMiddleware actually throttles and records in Redis."""
    raw_redis = aioredis.from_url(REDIS_URI)
    unique_path = f"/limited/test-{uuid.uuid4().hex[:6]}"

    # Send up to 5 requests (limit is 5 requests per minute)
    codes: list[int] = []
    for _ in range(7):
        r = await http_client.get(unique_path)
        codes.append(r.status_code)

    assert 429 in codes, f"Expected 429 rate limit exceeded in sequence: {codes}"

    # DIRECT REDIS VERIFICATION: Assert rate limit keys exist in live Redis container
    keys = await raw_redis.keys("rl:rate-limited-route:*")
    assert len(keys) > 0, "Expected rate limit key in live Redis container"

    # Verify TTL
    ttl = await raw_redis.ttl(keys[0])
    assert ttl > 0

    await raw_redis.aclose()


# ===========================================================================
# 4. SERVICE 1: ECHO SERVICE & RFC 7230 HEADER SANITIZATION
# ===========================================================================


@pytest.mark.asyncio
async def test_service_echo_and_rfc7230_sanitization(http_client: httpx.AsyncClient):
    """Verifies Echo service reflection and RFC 7230 header sanitization."""
    # Hop-by-hop headers to strip
    test_headers = {
        "Connection": "close, X-Dynamic-Hop",
        "X-Dynamic-Hop": "strip-me",
        "Keep-Alive": "timeout=5",
        "Upgrade": "websocket",
        "X-Custom-Client": "keep-me",
    }
    payload = {"greeting": "hello-container"}
    resp = await http_client.post("/echo/test-path", headers=test_headers, json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["path"] == "/test-path"
    assert data["body"] == payload

    reflected_headers = data["headers"]
    # Hop-by-hop headers MUST be stripped
    assert "keep-alive" not in reflected_headers
    assert "upgrade" not in reflected_headers
    assert "x-dynamic-hop" not in reflected_headers

    # Preserved headers & injected proxy headers
    assert reflected_headers.get("x-custom-client") == "keep-me"
    assert "x-forwarded-for" in reflected_headers
    assert reflected_headers.get("x-forwarded-proto") == "http"
    assert reflected_headers.get("host") == "echo-service:8001"


# ===========================================================================
# 5. SERVICE 2: SLOW SERVICE & TIMEOUT RESILIENCE
# ===========================================================================


@pytest.mark.asyncio
async def test_service_slow_timeout_handling(http_client: httpx.AsyncClient):
    """Verifies 504 Gateway Timeout when Slow Service exceeds route timeout (2.0s)."""
    # 1. Fast request succeeds
    resp_fast = await http_client.get("/slow/compute?delay=0.2")
    assert resp_fast.status_code == 200

    # 2. Slow request exceeding 2.0s timeout returns 504
    start = asyncio.get_event_loop().time()
    resp_slow = await http_client.get("/slow/compute?delay=3.0")
    duration = asyncio.get_event_loop().time() - start

    assert resp_slow.status_code == 504
    assert resp_slow.json()["error"] == "gateway_timeout"
    assert duration < 3.5


# ===========================================================================
# 6. SERVICE 3: FLAKY SERVICE & CIRCUIT BREAKER FSM
# ===========================================================================


@pytest.mark.asyncio
async def test_service_flaky_and_circuit_breaker(http_client: httpx.AsyncClient):
    """Verifies circuit breaker trips upon consecutive failures and protects upstream."""
    # 1. Success request
    r_ok = await http_client.get("/flaky/unstable?fail_rate=0.0")
    assert r_ok.status_code == 200

    # 2. Send consecutive error requests to trip circuit breaker (threshold = 3)
    for _ in range(5):
        await http_client.get("/flaky/unstable?fail_rate=1.0")

    # 3. Next request should be intercepted by Circuit Breaker with 503
    cb_resp = await http_client.get("/flaky/unstable?fail_rate=1.0")
    assert cb_resp.status_code == 503
    assert cb_resp.json()["error"] == "service_unavailable"


# ===========================================================================
# 7. SERVICE 4: STREAMING HASHER SERVICE
# ===========================================================================


@pytest.mark.asyncio
async def test_service_hasher_streaming_chunks(http_client: httpx.AsyncClient):
    """Verifies true chunked streaming upload and hashing (MD5, SHA1, SHA256)."""
    content = b"Streamed chunk 1; Streamed chunk 2; Streamed chunk 3!"
    expected_md5 = hashlib.md5(content).hexdigest()
    expected_sha256 = hashlib.sha256(content).hexdigest()

    async def chunk_generator():
        yield b"Streamed chunk 1; "
        yield b"Streamed chunk 2; "
        yield b"Streamed chunk 3!"

    resp = await http_client.post("/hasher/hash", content=chunk_generator())
    assert resp.status_code == 200
    data = resp.json()

    assert data["md5"] == expected_md5
    assert data["sha256"] == expected_sha256
    assert data["total_bytes"] == len(content)


# ===========================================================================
# 8. SERVICE 5: STREAMING DATA PIPELINE & IDEMPOTENT ORDERS
# ===========================================================================


@pytest.mark.asyncio
async def test_service_pipeline_ndjson_and_idempotency(http_client: httpx.AsyncClient):
    """Verifies NDJSON streaming event transforms and stateful idempotent orders."""
    # 1. NDJSON Streaming
    ndjson_payload = (
        b'{"id":"e-1","event":"user_login","data":{"ip":"10.0.0.1"}}\n'
        b'{"id":"e-2","event":"order_placed","data":{"amount":250}}\n'
    )
    resp_stream = await http_client.post(
        "/pipeline/pipeline/transform",
        content=ndjson_payload,
        headers={"Content-Type": "application/x-ndjson"},
    )
    assert resp_stream.status_code == 200
    lines = [json.loads(line) for line in resp_stream.text.strip().split("\n")]
    assert len(lines) == 2
    assert lines[0]["event"] == "user_login"
    assert lines[1]["event"] == "order_placed"

    # 2. Idempotent Order Creation
    idem_key = f"order-key-{uuid.uuid4().hex}"
    body = {"items": ["item-A", "item-B"], "tags": ["electronics"]}

    # First attempt: 201 Created (is_duplicate: False)
    r1 = await http_client.post(
        "/pipeline/pipeline/orders",
        json=body,
        headers={"Idempotency-Key": idem_key},
    )
    assert r1.status_code == 201
    data1 = r1.json()
    assert data1["is_duplicate"] is False
    assert data1["idempotency_key"] == idem_key

    # Replayed attempt with same key: 200 OK (is_duplicate: True)
    r2 = await http_client.post(
        "/pipeline/pipeline/orders",
        json=body,
        headers={"Idempotency-Key": idem_key},
    )
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["is_duplicate"] is True
    assert data2["order_id"] == data1["order_id"]


# ===========================================================================
# 9. LIVE MONGODB: ROUTE STORAGE, HOT RELOAD & RUNTIME MUTATIONS
# ===========================================================================


@pytest.mark.asyncio
async def test_live_mongodb_route_storage_in_container(http_client: httpx.AsyncClient):
    """Verifies runtime route creation, immediate hot-loading, status toggling,

    hot reloading, and deletion stored directly in the MongoDB container.
    """
    raw_mongo = AsyncIOMotorClient(MONGO_URI)
    db = raw_mongo["gateway"]
    col = db["routes"]

    route_id = f"cont-live-route-{uuid.uuid4().hex[:6]}"
    route_payload = {
        "id": route_id,
        "name": "Container Live Route",
        "path_pattern": f"/{route_id}/**",
        "methods": ["GET"],
        "upstream_url": "http://echo-service:8001",
        "strip_prefix": f"/{route_id}",
        "enabled": True,
    }

    try:
        # 1. CREATE ROUTE VIA ADMIN API
        res_create = await http_client.post("/admin/routes", json=route_payload)
        assert res_create.status_code == 201

        # DIRECT MONGO CHECK: verify document exists in MongoDB container
        mongo_doc = await col.find_one({"_id": route_id})
        assert mongo_doc is not None
        assert mongo_doc["name"] == "Container Live Route"

        # 2. IMMEDIATE PROXYING WITHOUT RESTART
        res_traffic = await http_client.get(f"/{route_id}/ping")
        assert res_traffic.status_code == 200
        assert res_traffic.json()["path"] == "/ping"

        # 3. TOGGLE STATUS: DISABLE
        res_disable = await http_client.patch(
            f"/admin/routes/{route_id}/status", json={"enabled": False}
        )
        assert res_disable.status_code == 200

        # Direct Mongo Check: verify enabled: False
        doc_disabled = await col.find_one({"_id": route_id})
        assert doc_disabled is not None
        assert doc_disabled["enabled"] is False

        # Traffic returns 404
        res_traffic_disabled = await http_client.get(f"/{route_id}/ping")
        assert res_traffic_disabled.status_code == 404

        # 4. HOT RELOAD FROM MONGODB
        res_reload = await http_client.post("/admin/routes/reload")
        assert res_reload.status_code == 200
        assert res_reload.json()["success"] is True

        # 5. DELETE ROUTE
        res_del = await http_client.delete(f"/admin/routes/{route_id}")
        assert res_del.status_code == 200

        # Direct Mongo Check: verify deleted from MongoDB
        doc_deleted = await col.find_one({"_id": route_id})
        assert doc_deleted is None

    finally:
        await col.delete_one({"_id": route_id})
        raw_mongo.close()
