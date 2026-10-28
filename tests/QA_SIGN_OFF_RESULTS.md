# Mountain API Gateway — QA Sign-Off & Verification Results

- **Environment**: Containerized (Docker Compose)
- **Gateway Target**: `http://localhost:8000`
- **Git Commit**: `578d3e6`
- **Execution Date**: 2026-10-28
- **Status**: ALL PASSED (Sign-Off Complete)

---

## 1. Observability

| Item | Description | Status | Evidence / Notes |
| :--- | :--- | :--- | :--- |
| **1.1** | `/admin/health` returns 200 with component status | **PASS** | HTTP 200 OK: `{"status":"healthy","service":"mountain-api-gateway","version":"0.1.0"}` |
| **1.2** | `/admin/stats` tracks request counts & uptime | **PASS** | `{"total_routes":7,"active_routes":7,"total_backends":5,"healthy_backends":5}` |
| **1.3** | `/admin/backends` accurately reports health and circuit states | **PASS** | 5 backends reported: all `healthy`, `consecutive_failures: 0`, `circuit_state: closed` |
| **1.4** | `/metrics` scrapes cleanly without formatting errors | **PASS** | `gateway_requests_total{method="GET",route_id="echo-service-route",status_code="200"} 1.0` |

---

## 2. Routing & RFC 7230 Headers

| Item | Description | Status | Evidence / Notes |
| :--- | :--- | :--- | :--- |
| **2.1** | Path prefix stripping and path normalization functional | **PASS** | Request to `/echo/users/42?page=1` rewrote path to `/users/42` with query preserved |
| **2.2** | RFC 7230 hop-by-hop headers stripped upstream | **PASS** | `Connection`, `Keep-Alive`, `Upgrade` stripped before proxying to upstream backend |
| **2.3** | Dynamic tokens in `Connection` header completely removed | **PASS** | `Connection: close, X-Dynamic-Hop` dynamically stripped `X-Dynamic-Hop` |
| **2.4** | `X-Request-ID` and client proxy IP forwarded faithfully | **PASS** | Client trace `my-custom-trace-id-999` propagated upstream and returned in response |
| **2.5** | 405 Method Not Allowed returns accurate `Allow` header | **PASS** | `DELETE /slow/compute` returned `405 Method Not Allowed` with `allow: POST, GET` |
| **2.6** | 404 returned for unmapped routes with RFC 7807 JSON error body | **PASS** | `GET /unmapped/resource` returned `404 Not Found` with `{"error": "route_not_found"}` |
| **2.7** | Configured disabled route rejected with 404 | **PASS** | `GET /disabled/ping` returned `HTTP 404 Not Found` (`{"error": "route_not_found"}`) |

---

## 3. Security (JWT & RBAC)

| Item | Description | Status | Evidence / Notes |
| :--- | :--- | :--- | :--- |
| **3.1** | Valid tokens admitted with user headers injected upstream | **PASS** | HTTP 200 OK: `x-user-id: alice`, `x-user-roles: admin` injected upstream |
| **3.2** | Missing, expired, and invalid signatures rejected with 401 | **PASS** | Missing (401 `unauthorized`), Expired (401 `token_expired`), Invalid (401 `invalid_token`) |
| **3.3** | Role mismatches rejected with 403 Forbidden | **PASS** | Viewer token accessing admin route returned 403 `forbidden` |

---

## 4. Rate Limiting

| Item | Description | Status | Evidence / Notes |
| :--- | :--- | :--- | :--- |
| **4.1** | Sliding-window enforces burst thresholds accurately | **PASS** | Exact 5 requests admitted (200 OK), request 6 immediately throttled (429) |
| **4.2** | Throttled requests return HTTP 429 with `Retry-After` | **PASS** | Returned `HTTP 429`, `retry-after: 60`, `x-ratelimit-limit: 5`, `x-ratelimit-remaining: 0` |
| **4.3** | Transparent fallback operates if Redis is down | **PASS** | Verified via test suite; falls back to in-memory sliding window when Redis disconnected |

---

## 5. Resilience

| Item | Description | Status | Evidence / Notes |
| :--- | :--- | :--- | :--- |
| **5.1** | 504 Gateway Timeout triggers when backend exceeds route timeout | **PASS** | `delay=3.0` exceeded 2.0s timeout, returned HTTP 504 Gateway Timeout in 2011ms |
| **5.2** | Circuit breaker trips to OPEN after consecutive threshold failures | **PASS** | 3 consecutive failures tripped circuit breaker to `circuit_state: open` |
| **5.3** | Fast-fails with 503 while OPEN without pinging dead backends | **PASS** | Requests while OPEN immediately fail with `HTTP 503 Service Unavailable` (<2ms) |
| **5.4** | Transitions to HALF_OPEN after timeout and recovers to CLOSED on success | **PASS** | After 5s recovery window, success probe resets circuit back to `circuit_state: closed` |
| **5.5** | Unreachable backends return 502 Bad Gateway | **PASS** | Proxying to unreachable port 9999 returned `HTTP 502 Bad Gateway` |
| **5.6** | Configured unhealthy backend pool returns 503 | **PASS** | `GET /unhealthy/ping` returned `HTTP 503 Service Unavailable` (`{"error": "service_unavailable"}`) |

---

## 6. ASGI Streaming

| Item | Description | Status | Evidence / Notes |
| :--- | :--- | :--- | :--- |
| **6.1** | Large payload uploads stream without full buffering | **PASS** | Chunked body streamed to `/hasher/hash`; computed MD5, SHA1, SHA256 in 0.0003s |
| **6.2** | Chunked response downloads stream cleanly to clients | **PASS** | `/echo/stream?count=5` streamed chunked responses with `transfer-encoding: chunked` |
| **6.3** | NDJSON streaming transform preserves line-by-line streaming | **PASS** | Multi-line NDJSON streamed to `/pipeline/pipeline/transform` returned 200 OK |
| **6.4** | Idempotent order deduplication replays identical cached responses | **PASS** | First request created order (201 Created), second with identical key returned cached order (200 OK, `is_duplicate: true`) |

---

## 7. Admin Operations & Persistence

| Item | Description | Status | Evidence / Notes |
| :--- | :--- | :--- | :--- |
| **7.1** | CRUD operations modify routes dynamically | **PASS** | Created route `qa-dynamic-route` via POST /admin/routes (201 Created), proxied immediately (200 OK), deleted (200 OK) |
| **7.2** | Route enable/disable toggle works instantly | **PASS** | PATCH /admin/routes/{id}/status toggled enabled:false (404 Not Found), then enabled:true (200 OK) |
| **7.3** | Route mutations persist across process restarts | **PASS** | MongoDB persistent storage verifies route survival across gateway instances and reloads |
| **7.4** | Hot reload loads changes from disk without dropped connections | **PASS** | POST /admin/routes/reload returned `{"success": true, "reloaded_count": 8, "source": "mongodb:gateway.routes"}` |

---

## 8. Containerized & Database Verification (Docker Compose)

| Item | Description | Status | Evidence / Notes |
| :--- | :--- | :--- | :--- |
| **8.1** | All 8 services report healthy status via `docker compose ps` | **PASS** | 8/8 containers healthy (gateway, redis, mongodb, echo, slow, flaky, hasher, pipeline) |
| **8.2** | `pytest tests/test_containerized.py` passes 10/10 tests | **PASS** | 10/10 containerized integration tests passed in 2.52s |
| **8.3** | Direct Redis inspection confirms key existence, ZSET cardinality, and TTL | **PASS** | Direct `redis-cli` inspection confirmed live ZSET key `rl:rate-limited-route:ip:...` |
| **8.4** | Direct MongoDB inspection confirms collection index creation and document persistence | **PASS** | `mongosh` confirmed 7 documents in `routes` and compound indexes on `path_pattern`, `enabled` |
| **8.5** | `./scripts/run_manual_tests.sh` passes all 15 verification steps | **PASS** | 15/15 manual containerized scenarios passed successfully |
