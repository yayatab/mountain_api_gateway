# Mountain API Gateway — Manual Testing Plan & Verification Playbook

This document is the definitive manual testing plan for the **Mountain API Gateway**. It is structured for QA engineers, developers, and evaluators to systematically verify every functional, performance, resilience, security, and administrative capability of the gateway against a live running system.

---

## 1. Test Environment Setup & Prerequisites

### Prerequisites
- Docker & Docker Compose (or Python 3.12+ with `uv`)
- `curl` (command-line HTTP client)
- `jq` (JSON processor for command line)
- Optional: `httpie`

### Running the System

#### Option A: Docker Compose (All Services & Redis) — Recommended
Start the gateway, Redis server, and all 5 mock backend services in isolated containers:
```bash
docker compose up --build
```

**Port Allocations:**
| Service | URL | Role / Purpose |
| :--- | :--- | :--- |
| **API Gateway** | `http://localhost:8000` | Main entry point & proxy dispatcher |
| **Echo Service** | `http://localhost:8001` | Request reflection, headers, payload echo |
| **Slow Service** | `http://localhost:8002` | Configurable latency for timeout testing |
| **Flaky Service** | `http://localhost:8003` | Synthetic error injection & circuit breaker testing |
| **Hasher Service** | `http://localhost:8004` | Chunked upload hashing & streaming download generation |
| **Pipeline Service** | `http://localhost:8005`| Streaming NDJSON transformation & idempotent order cache |
| **Redis Server** | `localhost:6379` | Distributed sliding-window rate limit store |
| **MongoDB Server** | `localhost:27017` | Persistent dynamic route storage & index collection |

#### Option B: Local Python Multi-Process (uv)
```bash
# Terminal 1: Start Gateway
GATEWAY_ROUTES_FILE_PATH=config/routes.json uv run uvicorn gateway.main:app --port 8000 --reload

# Terminal 2: Start Redis (if using local rate limiting with Redis)
redis-server --port 6379 &

# Terminal 3: Start Upstream Backends
uv run uvicorn mock_backend_services.echo_service:app --port 8001 &
uv run uvicorn mock_backend_services.slow_service:app --port 8002 &
uv run uvicorn mock_backend_services.flaky_service:app --port 8003 &
uv run uvicorn mock_backend_services.hasher_service:app --port 8004 &
uv run uvicorn mock_backend_services.data_pipeline_service:app --port 8005 &
```

---

## 2. Test Execution Matrix & Quick Reference

| ID | Category | Scenario | Method & Endpoint | Expected Status | Verification Target |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **TC-01** | Observability | Health Probe | `GET /admin/health` | `200 OK` | Gateway status healthy & up |
| **TC-02** | Observability | System Stats | `GET /admin/stats` | `200 OK` | Active route count & request metrics |
| **TC-03** | Observability | Backend Inventory | `GET /admin/backends` | `200 OK` | Backend URLs & circuit states |
| **TC-04** | Observability | Prometheus Scraping | `GET /metrics` | `200 OK` | `gateway_requests_total`, histograms |
| **TC-05** | Routing | Path Rewriting | `GET /echo/v1/users?k=v` | `200 OK` | Prefix `/echo` stripped; query preserved |
| **TC-06** | Routing | Route Precedence | `GET /secure/dashboard` | `401 Unauthorized` | Secure route (P=20) beats Echo (P=10) |
| **TC-07** | Routing | Method Not Allowed | `DELETE /slow/compute` | `405 Method Not Allowed` | Returns `Allow: GET, POST` header |
| **TC-08** | Routing | Route Not Found | `GET /nonexistent/route` | `404 Not Found` | RFC 7807 JSON error body |
| **TC-09** | Headers | RFC 7230 Hop-by-Hop | `GET /echo/headers` | `200 OK` | Strips `Connection`, `Upgrade`, tokens |
| **TC-10** | Headers | Host & Client IP | `GET /echo/headers` | `200 OK` | `Host` rewritten, `X-Forwarded-*` set |
| **TC-11** | Security | Valid JWT Admin | `GET /secure/dashboard` | `200 OK` | JWT verified, identity headers injected |
| **TC-12** | Security | Missing Auth Header | `GET /secure/dashboard` | `401 Unauthorized` | Rejection with `unauthorized` code |
| **TC-13** | Security | Forbidden Role | `GET /secure/dashboard` | `403 Forbidden` | Viewer token rejected (`forbidden`) |
| **TC-14** | Security | Expired Token | `GET /secure/dashboard` | `401 Unauthorized` | Rejection with `token_expired` |
| **TC-15** | Rate Limit | Quota Exhaustion | `GET /limited/resource` (x6) | `429 Too Many Req` | 6th call fails; returns `Retry-After` |
| **TC-16** | Resilience | Upstream Timeout | `GET /slow/compute?delay=3` | `504 Gateway Timeout` | Fails at 2.0s route timeout |
| **TC-17** | Resilience | Circuit Breaker Trip | `GET /flaky/error?fail_rate=1` | `500` then `503` | Trips to OPEN after 3 failures |
| **TC-18** | Resilience | Circuit Recovery | Sleep 5.5s $\to$ `GET /flaky` | `200 OK` | Recovers to HALF_OPEN $\to$ CLOSED |
| **TC-19** | Resilience | Upstream Crash | Request killed backend | `502 Bad Gateway` | Network refusal handled cleanly |
| **TC-20** | Streaming | Chunked Upload Hash | `POST /hasher/hash` | `200 OK` | On-the-fly MD5, SHA1, SHA256 |
| **TC-21** | Streaming | Chunked Download | `GET /hasher/generate` | `200 OK` | 1MB chunked binary stream |
| **TC-22** | Streaming | NDJSON Pipeline | `POST /pipeline/pipeline/transform` | `200 OK` | Streaming line-by-line transform |
| **TC-23** | Streaming | Idempotent Orders | `POST /pipeline/pipeline/orders` | `200 OK` | Duplicate `Idempotency-Key` replayed |
| **TC-24** | Admin API | Dynamic Route CRUD | `POST, GET, PUT, DELETE /admin/routes` | `201, 200, 204` | Full lifecycle modification |
| **TC-25** | Admin API | Route Status Toggle | `PATCH /admin/routes/{id}/status` | `200 OK` | Disable route $\to$ 404; enable $\to$ 200 |
| **TC-26** | Admin API | Hot Reload Config | `POST /admin/routes/reload` | `200 OK` | In-memory atomic table replacement |
| **TC-27** | Admin API | Restart Persistence | Restart container/process | `200 OK` | Dynamic changes persist across restart |
| **TC-28** | Containerized | Full Stack Automated Suite | `pytest tests/test_containerized.py` | `10 passed` | Validates all 8 containers & middlewares |
| **TC-29** | Database | Direct Redis Inspection | `redis-cli keys "rl:*"` | `Keys found` | ZSET cards, scores, and TTL confirmed |
| **TC-30** | Database | Direct MongoDB Inspection | `mongosh --eval "db.routes.find()"` | `Docs found` | Documents & compound indexes verified |
| **TC-31** | Database | Live DB Automated Suite | `pytest tests/test_live_databases.py` | `4 passed` | Proves physical writes & index creation |

---

## 3. Step-by-Step Scenario Execution Walkthrough

### Phase 1: Observability & Health Probing

#### Test Case 1.1: System Health & Component Status (TC-01)
Check that the gateway and its subsystems are running normally:
```bash
curl -i http://localhost:8000/admin/health
```
**Expected Response (`200 OK`):**
```json
{
  "status": "healthy",
  "version": "0.1.0",
  "routes_count": 7
}
```

#### Test Case 1.2: Gateway Statistics & Metrics (TC-02, TC-04)
Inspect gateway uptime, processed request count, and Prometheus metrics exposition:
```bash
# Gateway summary stats
curl -s http://localhost:8000/admin/stats | jq

# Prometheus metrics scrape
curl -s http://localhost:8000/metrics | grep -E "(gateway_requests_total|gateway_request_duration_seconds)"
```
**Verification Points:**
- `uptime_seconds` is positive and increments.
- `gateway_requests_total` contains labels for `method`, `route_id`, and `status_code`.

#### Test Case 1.3: Backend Inventory & Circuit State Inspection (TC-03)
Verify registered backend pools and circuit breaker states:
```bash
curl -s http://localhost:8000/admin/backends | jq
```
**Expected Output:**
List of all active backend URLs with `"status": "healthy"` and `"circuit_state": "closed"`.

---

### Phase 2: Routing, Path Normalization & Precedence

#### Test Case 2.1: Request Proxying & Prefix Stripping (TC-05)
Send a request to `/echo/users/42` with query arguments:
```bash
curl -i "http://localhost:8000/echo/users/42?page=1&status=active"
```
**Expected Output (`200 OK`):**
```json
{
  "method": "GET",
  "path": "/users/42",
  "query_params": {
    "page": "1",
    "status": "active"
  }
}
```
*Note that `/echo` was stripped, and `/users/42` reached the backend.*

#### Test Case 2.2: Path Normalization (Slashes & Trailing)
Send a request with redundant consecutive slashes:
```bash
curl -i "http://localhost:8000///echo//products///123"
```
**Verification:**
The gateway normalizes path slashes before route matching, forwarding to `/products/123`.

#### Test Case 2.3: Method Filtering — 405 Method Not Allowed (TC-07)
Send a `DELETE` request to a route configured only for `GET` and `POST` (`/slow/**`):
```bash
curl -i -X DELETE http://localhost:8000/slow/compute
```
**Expected Output (`405 Method Not Allowed`):**
```http
HTTP/1.1 405 Method Not Allowed
Allow: GET, POST
Content-Type: application/json
```
```json
{
  "error": "method_not_allowed",
  "message": "Method DELETE is not allowed for path /slow/compute"
}
```

#### Test Case 2.4: Non-Existent Route — 404 Not Found (TC-08)
Request an unregistered path:
```bash
curl -i http://localhost:8000/api/unknown/service
```
**Expected Output (`404 Not Found`):**
```json
{
  "error": "route_not_found",
  "message": "No active route found for path /api/unknown/service"
}
```

#### Test Case 2.5: Configured Disabled Route — 404 Not Found (TC-32)
Request a configured route that has `enabled: false`:
```bash
curl -i http://localhost:8000/disabled/ping
```
**Expected Output (`404 Not Found`):**
```json
{
  "error": "route_not_found",
  "message": "No active route found for path /disabled/ping"
}
```

---

### Phase 3: RFC 7230 Header Transformation & State Propagation

#### Test Case 3.1: Strict Hop-by-Hop Stripping & Custom Connection Tokens (TC-09)
Send hop-by-hop headers and dynamic tokens declared in `Connection`:
```bash
curl -i http://localhost:8000/echo/headers \
  -H "Connection: close, X-Dynamic-Secret, X-Trace-Token" \
  -H "Keep-Alive: timeout=5" \
  -H "Upgrade: websocket" \
  -H "X-Dynamic-Secret: super-confidential" \
  -H "X-Trace-Token: debug-token" \
  -H "X-Regular-Header: KeepMe"
```
**Verification in Echoed JSON (`200 OK`):**
- `Connection`, `Keep-Alive`, `Upgrade` are **absent**.
- Dynamic tokens `X-Dynamic-Secret` and `X-Trace-Token` are **absent**.
- Regular header `X-Regular-Header: KeepMe` is **preserved**.

#### Test Case 3.2: Host Authority Rewriting & X-Forwarded-* Headers (TC-10)
Inspect the forwarded host and proxy chain:
```bash
curl -s http://localhost:8000/echo/headers | jq .headers
```
**Verification:**
- `host` equals `echo-service:8001` (rewritten to backend authority).
- `x-forwarded-host` equals `localhost:8000`.
- `x-forwarded-for` contains client IP (`127.0.0.1`).
- `x-request-id` is present with a valid UUID.
- Client response includes `X-Response-Time` and `Server-Timing`.

---

### Phase 4: Authentication & Role-Based Access Control (RBAC)

Use the token generation utility to mint test JWT tokens:
```bash
# Valid Admin Token (sub: alice, roles: ["admin"])
ADMIN_TOKEN=$(uv run python scripts/generate_tokens.py --sub alice --roles admin)

# Viewer Token (sub: bob, roles: ["viewer"])
VIEWER_TOKEN=$(uv run python scripts/generate_tokens.py --sub bob --roles viewer)

# Expired Token (sub: eve, expired 10 minutes ago)
EXPIRED_TOKEN=$(uv run python scripts/generate_tokens.py --sub eve --roles admin --exp-minutes -10)
```

#### Test Case 4.1: Valid Admin Token Admitted (TC-11)
```bash
curl -i -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/secure/admin-dashboard
```
**Expected Response (`200 OK`):**
Echoed headers show injected upstream identity:
- `x-user-id: alice`
- `x-user-roles: admin`

#### Test Case 4.2: Missing Authorization Header (TC-12)
```bash
curl -i http://localhost:8000/secure/admin-dashboard
```
**Expected Response (`401 Unauthorized`):**
```json
{
  "error": "unauthorized",
  "message": "Authorization header with Bearer token is required"
}
```

#### Test Case 4.3: Insufficient Role Rejected (TC-13)
```bash
curl -i -H "Authorization: Bearer $VIEWER_TOKEN" http://localhost:8000/secure/admin-dashboard
```
**Expected Response (`403 Forbidden`):**
```json
{
  "error": "forbidden",
  "message": "User does not have required roles"
}
```

#### Test Case 4.4: Expired Token Rejected (TC-14)
```bash
curl -i -H "Authorization: Bearer $EXPIRED_TOKEN" http://localhost:8000/secure/admin-dashboard
```
**Expected Response (`401 Unauthorized`):**
```json
{
  "error": "token_expired",
  "message": "Authentication token has expired"
}
```

#### Test Case 4.5: Malformed Token Rejected
```bash
curl -i -H "Authorization: Bearer invalid.jwt.signature" http://localhost:8000/secure/admin-dashboard
```
**Expected Response (`401 Unauthorized`):**
```json
{
  "error": "invalid_token",
  "message": "Token decoding or cryptographic verification failed"
}
```

---

### Phase 5: Distributed Sliding-Window Rate Limiting

Route `/limited/**` has a configured threshold of **5 requests per minute**.

#### Test Case 5.1: Quota Consumption & 429 Throttle (TC-15)
Execute a burst of 6 rapid requests:
```bash
for i in {1..6}; do
  echo -n "Request $i: "
  curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8000/limited/resource
done
```
**Expected Output:**
```text
Request 1: HTTP 200
Request 2: HTTP 200
Request 3: HTTP 200
Request 4: HTTP 200
Request 5: HTTP 200
Request 6: HTTP 429
```

#### Test Case 5.2: Rate Limit Headers & Retry-After Inspection
Inspect response headers on the throttled request:
```bash
curl -i http://localhost:8000/limited/resource | grep -E "(HTTP|X-RateLimit|Retry-After)"
```
**Expected Headers:**
- `HTTP/1.1 429 Too Many Requests`
- `X-RateLimit-Limit: 5`
- `X-RateLimit-Remaining: 0`
- `Retry-After: <seconds_until_earliest_window_clears>`

#### Test Case 5.3: Redis Fallback to In-Memory Engine (Resilience)
*(Optional / Failure Injection Test)*
Stop or pause the Redis container:
```bash
docker compose pause redis
# Or if running locally: killall redis-server
```
Send requests to `/limited/resource`:
The gateway catches Redis connection failures and transparently falls back to in-memory tracking without throwing 500 errors to clients.
Resume Redis:
```bash
docker compose unpause redis
```

---

### Phase 6: Upstream Resilience, Timeouts & Circuit Breaker

#### Test Case 6.1: Upstream Timeout Enforcement — 504 Gateway Timeout (TC-16)
Route `/slow/**` has a configured timeout of **2.0 seconds**. Request a 3.5s delay:
```bash
curl -i "http://localhost:8000/slow/compute?delay=3.5"
```
**Expected Response (`504 Gateway Timeout`):**
Response arrives within ~2.0 to 2.2 seconds:
```json
{
  "error": "gateway_timeout",
  "message": "Upstream backend did not respond within 2.0s"
}
```

#### Test Case 6.2: Circuit Breaker State Transition — CLOSED $\to$ OPEN (TC-17)
The circuit breaker trips after **3 consecutive failures**. Use `/flaky/**` with `fail_rate=1.0`:
```bash
# Request 1: Backend 500 error
curl -i "http://localhost:8000/flaky/error?fail_rate=1.0"

# Request 2: Backend 500 error
curl -i "http://localhost:8000/flaky/error?fail_rate=1.0"

# Request 3: Backend 500 error (Threshold reached -> trips to OPEN)
curl -i "http://localhost:8000/flaky/error?fail_rate=1.0"
```
Check backend circuit state:
```bash
curl -s http://localhost:8000/admin/backends | jq '.[] | select(.url | contains("8003"))'
```
**Expected Output:**
`"circuit_state": "open"`

Now execute another request immediately:
```bash
curl -i "http://localhost:8000/flaky/error?fail_rate=1.0"
```
**Expected Response (`503 Service Unavailable`):**
Fails fast in <5ms without contacting the backend:
```json
{
  "error": "service_unavailable",
  "message": "No healthy upstream backends for route 'flaky-service-route'"
}
```

#### Test Case 6.3: Circuit Breaker Recovery — OPEN $\to$ HALF_OPEN $\to$ CLOSED (TC-18)
The circuit breaker recovery timeout is **5.0 seconds**.
```bash
# Wait for recovery timeout to elapse
sleep 6

# Execute successful request (fail_rate=0.0)
curl -i "http://localhost:8000/flaky/error?fail_rate=0.0"
```
**Expected Response (`200 OK`):**
The canary probe succeeds and resets the breaker back to `CLOSED`.
Verify state:
```bash
curl -s http://localhost:8000/admin/backends | jq '.[] | select(.url | contains("8003")) | .circuit_state'
```
**Expected Output:** `"closed"`

#### Test Case 6.4: Upstream Process Crash — 502 Bad Gateway (TC-19)
Send a request to a route whose backend is down:
```bash
# Temporarily register a route pointing to an unused port
curl -s -X POST http://localhost:8000/admin/routes \
  -H "Content-Type: application/json" \
  -d '{
    "id": "dead-service-route",
    "name": "Dead Service",
    "path_pattern": "/dead/**",
    "upstream_url": "http://127.0.0.1:9999",
    "strip_prefix": "/dead"
  }' > /dev/null

curl -i http://localhost:8000/dead/test
```
**Expected Response (`502 Bad Gateway`):**
```json
{
  "error": "bad_gateway",
  "message": "Failed to connect to upstream service"
}
```
Clean up test route:
```bash
curl -s -X DELETE http://localhost:8000/admin/routes/dead-service-route > /dev/null
```

#### Test Case 6.5: Unhealthy Upstream Backend Pool — 503 Service Unavailable (TC-33)
Request a configured route whose upstream pool instances are all marked `unhealthy`:
```bash
curl -i http://localhost:8000/unhealthy/ping
```
**Expected Response (`503 Service Unavailable`):**
```json
{
  "error": "service_unavailable",
  "message": "No healthy upstream backends for route 'unhealthy-backend-route'"
}
```

---

### Phase 7: ASGI Streaming & End-to-End Transfers

#### Test Case 7.1: Streaming Upload & Multi-Hash Calculation (TC-20)
Upload binary data to the Hasher service. The gateway streams the chunks directly without loading the body into memory:
```bash
curl -i -X POST http://localhost:8000/hasher/hash \
  --data-binary "The quick brown fox jumps over the lazy dog"
```
**Expected Response (`200 OK`):**
```json
{
  "status": "success",
  "total_bytes": 43,
  "chunk_count": 1,
  "md5": "9e107d9d372bb6826bd81d3542a419d6",
  "sha1": "2fd4e1c67a2d28fced849ee1bb76e7391b93eb12",
  "sha256": "d7a8fbb307d7809469ca9ab400f20d5f99222e18b41830fb47ef609e6778e3a3"
}
```

#### Test Case 7.2: Streaming Chunked Download (TC-21)
Download a 2MB generated binary stream:
```bash
curl -i "http://localhost:8000/hasher/generate?size_bytes=2097152&chunk_size=65536" \
  -o /tmp/stream_test.bin
```
**Verification:**
- Response header contains `Transfer-Encoding: chunked`.
- File on disk is exactly 2,097,152 bytes:
  ```bash
  ls -lh /tmp/stream_test.bin
  ```

#### Test Case 7.3: NDJSON Streaming Transformation Pipeline (TC-22)
Stream line-delimited JSON through the gateway to the pipeline service:
```bash
printf '{"record_id": 1, "val": 10}\n{"record_id": 2, "val": 20}\n' | \
  curl -i -X POST http://localhost:8000/pipeline/pipeline/transform \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @-
```
**Expected Response (`200 OK`):**
Returns transformed NDJSON lines with `"processed": true`.

#### Test Case 7.4: Idempotency Key Deduplication (TC-23)
Send duplicate requests with the same `Idempotency-Key` header:
```bash
ORDER_KEY="order-manual-$(date +%s)"

# First request: Creates order (replayed: false)
curl -s -X POST http://localhost:8000/pipeline/pipeline/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $ORDER_KEY" \
  -d '{"order_id": "ORD-500", "amount": 250.0}' | jq

# Second request with SAME key: Cached response (replayed: true)
curl -s -X POST http://localhost:8000/pipeline/pipeline/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $ORDER_KEY" \
  -d '{"order_id": "ORD-500", "amount": 250.0}' | jq
```
**Verification:**
First response returns `"replayed": false`; second response returns `"replayed": true` with identical order details.

---

### Phase 8: Admin Management API & Restart Persistence

#### Test Case 8.1: Create Dynamic Route (TC-24)
Register a new runtime route:
```bash
curl -i -X POST http://localhost:8000/admin/routes \
  -H "Content-Type: application/json" \
  -d '{
    "id": "catalog-service-route",
    "name": "Catalog Service",
    "path_pattern": "/catalog/**",
    "methods": ["GET", "POST"],
    "upstream_url": "http://echo-service:8001",
    "strip_prefix": "/catalog",
    "priority": 15
  }'
```
**Expected Response:** `HTTP/1.1 201 Created`

#### Test Case 8.2: Inspect Created Route
```bash
curl -s http://localhost:8000/admin/routes/catalog-service-route | jq
```
**Verification:**
Route object returned matching created parameters.

#### Test Case 8.3: Verify Dynamic Routing Works
```bash
curl -i http://localhost:8000/catalog/products/101
```
**Expected Response (`200 OK`):**
Echo service reflects `/products/101`.

#### Test Case 8.4: Toggle Route Status (TC-25)
Disable the route:
```bash
curl -i -X PATCH http://localhost:8000/admin/routes/catalog-service-route/status \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```
Test route is disabled:
```bash
curl -i http://localhost:8000/catalog/products/101
```
**Expected Response (`404 Not Found`).**

Re-enable route:
```bash
curl -i -X PATCH http://localhost:8000/admin/routes/catalog-service-route/status \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```
Test route is active again:
```bash
curl -i http://localhost:8000/catalog/products/101
```
**Expected Response (`200 OK`).**

#### Test Case 8.5: Zero-Downtime Hot Reload (TC-26)
Trigger hot reload from configuration file:
```bash
curl -i -X POST http://localhost:8000/admin/routes/reload
```
**Expected Response (`200 OK`):**
```json
{
  "success": true,
  "message": "Routes reloaded from disk",
  "active_routes": 8
}
```

#### Test Case 8.6: Delete Dynamic Route
Clean up the test route:
```bash
curl -i -X DELETE http://localhost:8000/admin/routes/catalog-service-route
```
**Expected Response:** `HTTP/1.1 204 No Content`

---

### Phase 9: Containerized Full-Stack Verification (Docker Compose)

This phase verifies that the entire gateway architecture—including all 5 mock backends, Redis, and MongoDB—operates seamlessly when fully isolated inside Docker containers.

#### Test Case 9.1: Container Stack Health Probing (TC-28)
Verify all 8 containers are healthy and running:
```bash
docker compose ps
```
**Expected Output:**
All containers (`mountain_gateway`, `mountain_redis`, `mountain_mongodb`, `mountain_echo`, `mountain_slow`, `mountain_flaky`, `mountain_hasher`, `mountain_pipeline`) report `STATUS: Up ... (healthy)`.

#### Test Case 9.2: Automated Containerized Test Suite (TC-28)
Run the dedicated end-to-end containerized test suite:
```bash
pytest tests/test_containerized.py -v
```
**Expected Output:**
`10 passed in ~2.6s` validating:
1. `test_middleware_timing_and_trace_headers`
2. `test_middleware_prometheus_metrics`
3. `test_middleware_jwt_auth_and_rbac`
4. `test_middleware_redis_rate_limiting_in_container`
5. `test_service_echo_and_rfc7230_sanitization`
6. `test_service_slow_timeout_handling`
7. `test_service_flaky_and_circuit_breaker`
8. `test_service_hasher_streaming_chunks`
9. `test_service_pipeline_ndjson_and_idempotency`
10. `test_live_mongodb_route_storage_in_container`

#### Test Case 9.3: Direct Live Redis Container Inspection (TC-29)
Verify rate-limiting keys and ZSET scores directly inside the Redis container:
```bash
docker exec mountain_redis redis-cli keys "rl:*"
```
Inspect ZSET timestamps and TTL of an active rate-limited route:
```bash
docker exec mountain_redis redis-cli zrange "rl:rate-limited-route:ip:127.0.0.1" 0 -1 WITHSCORES
docker exec mountain_redis redis-cli ttl "rl:rate-limited-route:ip:127.0.0.1"
```
**Expected Response:**
Key exists, score timestamps match epoch times, and TTL is $> 0$ and $\le 120$s.

#### Test Case 9.4: Direct Live MongoDB Container Inspection (TC-30)
Verify route documents and indexes directly inside the MongoDB container:
```bash
docker exec mountain_mongodb mongosh gateway --eval "db.routes.getIndexes()"
docker exec mountain_mongodb mongosh gateway --eval "db.routes.countDocuments()"
docker exec mountain_mongodb mongosh gateway --eval "db.routes.find({}, {id: 1, path_pattern: 1, enabled: 1})"
```
**Expected Response:**
- Indexes on `path_pattern_1` and `enabled_1` exist.
- Documents are seeded and stored as expected.

#### Test Case 9.5: Live Database Verification Suite (TC-31)
Run the automated live database test suite verifying real reads, writes, and deletions:
```bash
pytest tests/test_live_databases.py -v
```
**Expected Output:**
`4 passed` verifying direct database interaction without in-memory mocks.

---

## 4. Automated Live Verification Helper

An automated live test runner is included in the repository. It executes the critical paths of this playbook automatically:

```bash
# Execute against localhost:8000
./scripts/run_manual_tests.sh

# Or against custom deployment
GATEWAY_URL="http://10.0.0.15:8000" ./scripts/run_manual_tests.sh
```

---

## 5. QA Sign-Off & Verification Checklist

Use this checklist during manual release testing:

- [ ] **Observability**
  - [ ] `/admin/health` returns 200 with component status
  - [ ] `/admin/stats` tracks request counts & uptime
  - [ ] `/admin/backends` accurately reports health and circuit states
  - [ ] `/metrics` scrapes cleanly without formatting errors
- [ ] **Routing & RFC 7230 Headers**
  - [ ] Path prefix stripping and path normalization functional
  - [ ] RFC 7230 hop-by-hop headers stripped upstream
  - [ ] Dynamic tokens in `Connection` header completely removed
  - [ ] `X-Request-ID` and client proxy IP forwarded faithfully
  - [ ] 405 Method Not Allowed returns accurate `Allow` header
  - [ ] 404 returned for unmapped routes with RFC 7807 JSON error body
- [ ] **Security (JWT & RBAC)**
  - [ ] Valid tokens admitted with user headers injected upstream
  - [ ] Missing, expired, and invalid signatures rejected with 401
  - [ ] Role mismatches rejected with 403 Forbidden
- [ ] **Rate Limiting**
  - [ ] Sliding-window enforces burst thresholds accurately
  - [ ] Throttled requests return HTTP 429 with `Retry-After`
  - [ ] Transparent fallback operates if Redis is down
- [ ] **Resilience**
  - [ ] 504 Gateway Timeout triggers when backend exceeds route timeout
  - [ ] Circuit breaker trips to OPEN after consecutive threshold failures
  - [ ] Fast-fails with 503 while OPEN without pinging dead backends
  - [ ] Transitions to HALF_OPEN after timeout and recovers to CLOSED on success
  - [ ] Unreachable backends return 502 Bad Gateway
- [ ] **ASGI Streaming**
  - [ ] Large payload uploads stream without full buffering
  - [ ] Chunked response downloads stream cleanly to clients
  - [ ] NDJSON streaming transform preserves line-by-line streaming
  - [ ] Idempotent order deduplication replays identical cached responses
- [ ] **Admin Operations & Persistence**
  - [ ] CRUD operations modify routes dynamically
  - [ ] Route enable/disable toggle works instantly
  - [ ] Route mutations persist across process restarts
  - [ ] Hot reload loads changes from disk without dropped connections
- [ ] **Containerized & Database Verification (Docker Compose)**
  - [ ] All 8 services report healthy status via `docker compose ps`
  - [ ] `pytest tests/test_containerized.py` passes 10/10 tests
  - [ ] Direct Redis inspection confirms key existence, ZSET cardinality, and TTL
  - [ ] Direct MongoDB inspection confirms collection index creation and document persistence
  - [ ] `./scripts/run_manual_tests.sh` passes all 15 verification steps

---

**Tester Name:** _______________________  
**Date Executed:** _______________________  
**Release / Git Commit:** _______________  
**Result:** [ ] PASS  [ ] FAIL  [ ] BLOCKED  
