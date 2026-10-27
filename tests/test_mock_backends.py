import asyncio
import json
import time
from http import HTTPMethod

import httpx
import pytest

from gateway.discovery.load_balancer import LoadBalancer
from gateway.main import create_app
from gateway.models.route import RouteDefinition
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry
from mock_backend_services.data_pipeline_service import app as data_pipeline_app
from mock_backend_services.echo_service import app as echo_app
from mock_backend_services.flaky_service import app as flaky_app
from mock_backend_services.hasher_service import app as hasher_app
from mock_backend_services.slow_service import app as slow_app

# ---------------------------------------------------------------------------
# ECHO SERVICE TESTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_echo_health_probe():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=echo_app), base_url="http://test")
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "echo-service"


@pytest.mark.asyncio
async def test_echo_get_reflection():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=echo_app), base_url="http://test")
    resp = await client.get("/api/v1/items?search=apples&page=2", headers={"X-Test": "EchoVal"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["method"] == "GET"
    assert data["path"] == "/api/v1/items"
    assert data["query_params"] == {"search": "apples", "page": "2"}
    assert data["headers"]["x-test"] == "EchoVal"
    assert data["body"] is None


@pytest.mark.asyncio
async def test_echo_post_json_and_plain_text():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=echo_app), base_url="http://test")

    # JSON body
    resp_json = await client.post("/submit", json={"key": "value", "count": 42})
    assert resp_json.status_code == 200
    assert resp_json.json()["body"] == {"key": "value", "count": 42}

    # Plain text body
    resp_text = await client.post(
        "/text", content="plain text payload", headers={"Content-Type": "text/plain"}
    )
    assert resp_text.status_code == 200
    assert resp_text.json()["body"] == "plain text payload"


@pytest.mark.asyncio
async def test_echo_custom_status_code():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=echo_app), base_url="http://test")

    resp_201 = await client.post("/resource?status_code=201")
    assert resp_201.status_code == 201

    resp_418 = await client.get("/teapot?status_code=418")
    assert resp_418.status_code == 418


@pytest.mark.asyncio
async def test_echo_streaming_chunks():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=echo_app), base_url="http://test")
    resp = await client.get("/stream?chunks=3&delay=0.01")
    assert resp.status_code == 200

    lines = [line for line in resp.text.strip().split("\n") if line]
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [p["chunk"] for p in parsed] == [1, 2, 3]
    assert all(p["total"] == 3 for p in parsed)


# ---------------------------------------------------------------------------
# SLOW SERVICE TESTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_service_instant_health_check():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=slow_app), base_url="http://test")
    start = time.perf_counter()
    resp = await client.get("/health")
    elapsed = time.perf_counter() - start

    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
    assert resp.json()["service"] == "slow-service"
    # Health check must return near-instantaneously
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_slow_service_delay():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=slow_app), base_url="http://test")
    start = time.perf_counter()
    resp = await client.get("/api/compute?delay=0.05")
    elapsed = time.perf_counter() - start

    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert resp.json()["delayed_seconds"] == 0.05
    assert elapsed >= 0.045


@pytest.mark.asyncio
async def test_slow_service_via_gateway_triggers_504_timeout():
    class BackendTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.transport = httpx.ASGITransport(app=slow_app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            timeout = request.extensions.get("timeout", {}).get("read")
            if timeout is not None:
                try:
                    async with asyncio.timeout(timeout):
                        return await self.transport.handle_async_request(request)
                except TimeoutError as exc:
                    raise httpx.ReadTimeout("Request timed out", request=request) from exc
            return await self.transport.handle_async_request(request)

    route = RouteDefinition(
        id="slow-route",
        path_pattern="/slow/**",
        methods={HTTPMethod.GET},
        upstream_url="http://slow-backend",
        timeout_seconds=0.05,  # Gateway timeout 50ms
    )
    registry = RouteRegistry(routes=[route])
    mock_client = httpx.AsyncClient(transport=BackendTransport())
    engine = ProxyEngine(client=mock_client)
    gateway_app = create_app(registry=registry, proxy_engine=engine)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )

    # Backend delays for 150ms > 50ms gateway timeout -> 504 Gateway Timeout
    resp = await client.get("/slow/endpoint?delay=0.15")
    assert resp.status_code == 504
    assert resp.json()["error"] == "gateway_timeout"


# ---------------------------------------------------------------------------
# FLAKY SERVICE TESTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flaky_service_health_probe():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=flaky_app), base_url="http://test")

    # Default healthy
    resp_healthy = await client.get("/health")
    assert resp_healthy.status_code == 200
    assert resp_healthy.json()["status"] == "healthy"

    # Simulated unhealthy probe
    resp_unhealthy = await client.get("/health?unhealthy=true")
    assert resp_unhealthy.status_code == 503


@pytest.mark.asyncio
async def test_flaky_service_deterministic_failure_and_success():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=flaky_app), base_url="http://test")

    # fail_rate=0.0 always succeeds
    for _ in range(5):
        resp = await client.get("/test?fail_rate=0.0")
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    # fail_rate=1.0 always fails with default 500
    resp_fail = await client.get("/test?fail_rate=1.0")
    assert resp_fail.status_code == 500

    # fail_rate=1.0 with custom fail_code=502
    resp_custom = await client.get("/test?fail_rate=1.0&fail_code=502")
    assert resp_custom.status_code == 502


@pytest.mark.asyncio
async def test_flaky_service_via_gateway_trips_circuit_breaker():
    class BackendTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.transport = httpx.ASGITransport(app=flaky_app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await self.transport.handle_async_request(request)

    route = RouteDefinition(
        id="flaky-route",
        path_pattern="/flaky/**",
        methods={HTTPMethod.GET},
        upstream_url="http://flaky-backend",
        timeout_seconds=2.0,
    )
    registry = RouteRegistry(routes=[route])
    lb = LoadBalancer(failure_threshold=2, recovery_timeout_seconds=5.0)
    mock_client = httpx.AsyncClient(transport=BackendTransport())
    engine = ProxyEngine(client=mock_client, on_result=lb.record_result)
    gateway_app = create_app(registry=registry, proxy_engine=engine, load_balancer=lb)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )

    # 1. First failure (500 from upstream)
    r1 = await client.get("/flaky/test?fail_rate=1.0")
    assert r1.status_code == 500

    # 2. Second failure (500 from upstream) trips circuit breaker
    r2 = await client.get("/flaky/test?fail_rate=1.0")
    assert r2.status_code == 500

    cb = lb.get_circuit_breaker("http://flaky-backend")
    assert cb.state.value == "open"

    # 3. Third request fails fast at gateway with 503 (Circuit breaker OPEN, no healthy backend)
    r3 = await client.get("/flaky/test?fail_rate=1.0")
    assert r3.status_code == 503
    assert r3.json()["error"] == "service_unavailable"


# ---------------------------------------------------------------------------
# HASHER SERVICE TESTS (STREAMING FILE PROCESSING)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hasher_service_health():
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hasher_app),
        base_url="http://test",
    )
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
    assert resp.json()["service"] == "hasher-service"


@pytest.mark.asyncio
async def test_hasher_service_streaming_upload_and_hashes():
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hasher_app),
        base_url="http://test",
    )

    import hashlib

    # Multi-chunk payload
    chunks = [f"chunk-part-{i:03d}-payload-data\n".encode() for i in range(100)]
    full_content = b"".join(chunks)

    expected_md5 = hashlib.md5(full_content).hexdigest()
    expected_sha1 = hashlib.sha1(full_content).hexdigest()
    expected_sha256 = hashlib.sha256(full_content).hexdigest()

    async def stream_generator():
        for c in chunks:
            yield c

    resp = await client.post(
        "/hash?filename=sample_document.txt",
        content=stream_generator(),
        headers={"Content-Type": "application/octet-stream"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "sample_document.txt"
    assert data["total_bytes"] == len(full_content)
    assert data["chunk_count"] == 100
    assert data["md5"] == expected_md5
    assert data["sha1"] == expected_sha1
    assert data["sha256"] == expected_sha256
    assert data["elapsed_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_hasher_service_via_gateway_proxy_streaming():
    class BackendTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.transport = httpx.ASGITransport(app=hasher_app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await self.transport.handle_async_request(request)

    import hashlib

    file_bytes = b"Mountain-API-Gateway-Streaming-Test-Data" * 200
    expected_sha256 = hashlib.sha256(file_bytes).hexdigest()

    route = RouteDefinition(
        id="hasher-route",
        path_pattern="/files/**",
        methods={HTTPMethod.POST, HTTPMethod.GET},
        upstream_url="http://hasher-backend",
        strip_prefix="/files",
    )
    registry = RouteRegistry(routes=[route])
    mock_client = httpx.AsyncClient(transport=BackendTransport())
    engine = ProxyEngine(client=mock_client)
    gateway_app = create_app(registry=registry, proxy_engine=engine)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )

    # Post streaming through gateway to /files/hash -> forwarded to /hash
    resp = await client.post(
        "/files/hash",
        content=file_bytes,
        headers={"X-File-Name": "uploaded_report.pdf"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "uploaded_report.pdf"
    assert data["total_bytes"] == len(file_bytes)
    assert data["sha256"] == expected_sha256


@pytest.mark.asyncio
async def test_hasher_service_generate_stream_download():
    class BackendTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.transport = httpx.ASGITransport(app=hasher_app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await self.transport.handle_async_request(request)

    route = RouteDefinition(
        id="hasher-download-route",
        path_pattern="/download-stream",
        methods={HTTPMethod.GET},
        upstream_url="http://hasher-backend",
        strip_prefix="/download-stream",
        add_prefix="/generate",
    )
    registry = RouteRegistry(routes=[route])
    mock_client = httpx.AsyncClient(transport=BackendTransport())
    engine = ProxyEngine(client=mock_client)
    gateway_app = create_app(registry=registry, proxy_engine=engine)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )

    resp = await client.get("/download-stream?size_bytes=65536&chunk_size=8192")
    assert resp.status_code == 200
    assert len(resp.content) == 65536
    # Verify deterministic pattern
    assert resp.content.startswith(b"0123456789abcdef")


# ---------------------------------------------------------------------------
# DATA PIPELINE SERVICE TESTS (STREAMING TRANSFORM & IDEMPOTENT WORKFLOW)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_pipeline_health():
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=data_pipeline_app),
        base_url="http://test",
    )
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
    assert resp.json()["service"] == "data-pipeline-service"


@pytest.mark.asyncio
async def test_data_pipeline_streaming_transform_via_gateway():
    class BackendTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.transport = httpx.ASGITransport(app=data_pipeline_app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await self.transport.handle_async_request(request)

    route = RouteDefinition(
        id="pipeline-route",
        path_pattern="/stream-pipeline/**",
        methods={HTTPMethod.POST},
        upstream_url="http://pipeline-backend",
        strip_prefix="/stream-pipeline",
    )
    registry = RouteRegistry(routes=[route])
    mock_client = httpx.AsyncClient(transport=BackendTransport())
    engine = ProxyEngine(client=mock_client)
    gateway_app = create_app(registry=registry, proxy_engine=engine)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )

    # 3 NDJSON events
    ndjson_body = (
        json.dumps({"id": "evt-1", "event": "user_signup", "data": {"plan": "pro"}})
        + "\n"
        + json.dumps({"id": "evt-2", "event": "order_placed", "data": {"amount": 99}})
        + "\n"
        + json.dumps({"id": "evt-3", "event": "payment_received", "data": {"currency": "USD"}})
        + "\n"
    )

    resp = await client.post(
        "/stream-pipeline/pipeline/transform",
        content=ndjson_body.encode("utf-8"),
        headers={
            "Content-Type": "application/x-ndjson",
            "X-Request-ID": "gateway-trace-12345",
        },
    )

    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line]
    assert len(lines) == 3

    parsed = [json.loads(line) for line in lines]
    assert [p["id"] for p in parsed] == ["evt-1", "evt-2", "evt-3"]
    assert [p["event"] for p in parsed] == ["user_signup", "order_placed", "payment_received"]
    # Verify gateway injected request ID was forwarded to and read by the backend
    assert all(p["gateway_request_id"] == "gateway-trace-12345" for p in parsed)


@pytest.mark.asyncio
async def test_data_pipeline_idempotent_order_processing():
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=data_pipeline_app),
        base_url="http://test",
    )
    await client.post("/pipeline/reset")

    # 1. Missing Idempotency-Key returns 400
    resp_no_key = await client.post("/pipeline/orders", json={"items": ["item1"]})
    assert resp_no_key.status_code == 400

    # 2. First call with Idempotency-Key returns 201 Created
    headers = {"Idempotency-Key": "order-key-abc-123", "X-User-Id": "user-42"}
    resp_first = await client.post(
        "/pipeline/orders",
        json={"items": ["laptop", "mouse"], "tags": ["priority", "hardware"]},
        headers=headers,
    )
    assert resp_first.status_code == 201
    data_first = resp_first.json()
    assert data_first["idempotency_key"] == "order-key-abc-123"
    assert data_first["user_id"] == "user-42"
    assert data_first["total_items"] == 2
    assert set(data_first["tags"]) == {"priority", "hardware"}
    assert data_first["is_duplicate"] is False
    order_id = data_first["order_id"]

    # 3. Duplicate call with same Idempotency-Key returns 200 OK with is_duplicate=True
    resp_second = await client.post(
        "/pipeline/orders",
        json={"items": ["different", "items"]},
        headers=headers,
    )
    assert resp_second.status_code == 200
    data_second = resp_second.json()
    assert data_second["order_id"] == order_id  # Same order ID returned
    assert data_second["is_duplicate"] is True
    assert data_second["total_items"] == 2  # Original items preserved
