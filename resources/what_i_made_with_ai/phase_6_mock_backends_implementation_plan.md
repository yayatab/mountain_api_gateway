# Phase 6: Mock Backend Services — Implementation Plan

## Goal Description
Enhance and solidify the **Mock Backend Services Subsystem** (`mock_backend_services/`) into production-grade, standalone services for local development, integration testing, and Docker Compose orchestration:
1. **Echo Service** (`mock_backend_services/echo_service.py`):
   - Returns full request inspection: method, path, headers, query params, parsed JSON or raw text body, and client IP.
   - Supports configurable response status code (`?status_code=XXX`).
   - Supports simulated chunked streaming (`/stream?chunks=N&delay=X`) for testing streaming proxy mechanics.
   - Dedicated zero-latency `/health` probe.
2. **Slow Service** (`mock_backend_services/slow_service.py`):
   - Configurable delay (`?delay=X.X` seconds) for testing gateway timeouts (504 Gateway Timeout).
   - Dedicated zero-latency `/health` probe to allow health checkers to monitor the instance without false-positive timeouts.
3. **Flaky Service** (`mock_backend_services/flaky_service.py`):
   - Configurable failure rate (`?fail_rate=0.5`) and custom failure status code (`?fail_code=500` / `502` / `503`).
   - Deterministic testing modes (`fail_rate=1.0` always fails, `fail_rate=0.0` always succeeds).
   - Dedicated zero-latency `/health` probe with optional simulation toggle (`?unhealthy=true`).
4. **Standalone Port Configuration & Entrypoints**:
   - Runnable via `python -m mock_backend_services.<service>` or `uvicorn` with configurable environment variables (`ECHO_PORT=8001`, `SLOW_PORT=8002`, `FLAKY_PORT=8003`).
5. **Strict Pydantic Response Modeling**:
   - All response schemas modeled with Pydantic `BaseModel` (`EchoResponse`, `SlowResponse`, `FlakyResponse`, `ServiceHealth`).

---

## User Review Required

> [!NOTE]
> All response bodies returned by the mock backend services will use Pydantic `BaseModel` classes, guaranteeing type-safety, automatic JSON serialization, and OpenAPI docs generation at their respective `/docs` endpoints.

> [!IMPORTANT]
> **Health Check Route Isolation**:
> In `slow_service.py` and `flaky_service.py`, `@app.get("/health")` is registered **before** the catch-all wildcard route `/{full_path:path}`. This ensures health checks return instantly (`HTTP 200 {"status": "healthy"}`) without incurring artificial delays or random failures, unless specifically requested.

---

## Proposed Changes

### 1. Mock Models & Shared Schemas ([mock_backend_services/models.py](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/mock_backend_services/models.py))
#### [NEW] `mock_backend_services/models.py`
Define Pydantic models for responses:
- `EchoResponse(BaseModel)`: `method: str`, `path: str`, `query_params: dict[str, str]`, `headers: dict[str, str]`, `body: Any`, `client_ip: str | None`.
- `SlowResponse(BaseModel)`: `status: str = "completed"`, `delayed_seconds: float`, `path: str`.
- `FlakyResponse(BaseModel)`: `status: str = "success"`, `path: str`, `attempt_rate: float`.
- `ServiceHealth(BaseModel)`: `status: str = "healthy"`, `service: str`.

---

### 2. Echo Service ([mock_backend_services/echo_service.py](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/mock_backend_services/echo_service.py))
#### [MODIFY] `mock_backend_services/echo_service.py`
- Add `/health` endpoint returning `ServiceHealth(service="echo")`.
- Add `/stream` endpoint returning a `StreamingResponse` yielding chunked JSON lines with optional sleep intervals.
- Support `?status_code=XXX` query parameter allowing clients to simulate `201`, `204`, `400`, `418`, etc.
- Add `if __name__ == "__main__": uvicorn.run(...)` with `ECHO_PORT` env support.

---

### 3. Slow Service ([mock_backend_services/slow_service.py](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/mock_backend_services/slow_service.py))
#### [MODIFY] `mock_backend_services/slow_service.py`
- Add dedicated instant `/health` endpoint returning `ServiceHealth(service="slow")`.
- Implement `/{full_path:path}` with async `asyncio.sleep(delay)` and `SlowResponse`.
- Add `if __name__ == "__main__": uvicorn.run(...)` with `SLOW_PORT` env support.

---

### 4. Flaky Service ([mock_backend_services/flaky_service.py](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/mock_backend_services/flaky_service.py))
#### [MODIFY] `mock_backend_services/flaky_service.py`
- Add dedicated `/health` endpoint with optional `?unhealthy=true` query parameter for simulating backend outages to the gateway's health checker.
- Implement `/{full_path:path}` with configurable `fail_rate` (default 0.5) and `fail_code` (default 500).
- Add `if __name__ == "__main__": uvicorn.run(...)` with `FLAKY_PORT` env support.

---

### 5. Automated Mock Backends Test Suite ([tests/test_mock_backends.py](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/tests/test_mock_backends.py))
#### [NEW] `tests/test_mock_backends.py`
Comprehensive test suite testing:
- **Echo Service**: GET reflection, POST body reflection (JSON & plain text), custom query status code, header reflection, streaming chunks.
- **Slow Service**: Instant `/health`, simulated delay verification, gateway proxy integration triggering 504 on timeout.
- **Flaky Service**: Instant `/health`, `fail_rate=0.0` (100% success), `fail_rate=1.0` (100% failure with custom code), stochastic failover via load balancer.

---

## Verification Plan

### Automated Tests
Run mock backend tests and the full suite:
```bash
uv run pytest -v tests/test_mock_backends.py
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run pyright
```
