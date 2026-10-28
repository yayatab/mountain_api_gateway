# Mountain API Gateway — QA Curl Verification Commands

This document aggregates all live `curl` verification commands used during manual and automated QA sign-off of the Mountain API Gateway.
All commands target `http://localhost:8000` (the default gateway port).

---

## 1. Observability & Health Probing

### 1.1 Gateway System Health
```bash
curl -i http://localhost:8000/admin/health
```
**Expected Response:** `HTTP 200 OK`
```json
{
  "status": "healthy",
  "service": "mountain-api-gateway",
  "version": "0.1.0",
  "uptime_seconds": 123.45
}
```

### 1.2 Summary Statistics
```bash
curl -s http://localhost:8000/admin/stats | jq
```
**Expected Response:** `HTTP 200 OK`
```json
{
  "total_routes": 9,
  "active_routes": 8,
  "disabled_routes": 1,
  "total_backends": 6,
  "healthy_backends": 5,
  "unhealthy_backends": 1
}
```

### 1.3 Backend Inventory & Circuit State Inspection
```bash
curl -s http://localhost:8000/admin/backends | jq
```
**Expected Response:** `HTTP 200 OK` — list of all registered backend instances, health states (`healthy` / `unhealthy`), and circuit states (`closed` / `open` / `half_open`).

### 1.4 Prometheus Metrics Scraping
```bash
curl -s http://localhost:8000/metrics | grep gateway_
```
**Expected Output:** Prometheus formatted text with `gateway_requests_total` and `gateway_request_duration_seconds`.

---

## 2. Routing, Path Normalization & Precedence

### 2.1 Prefix Stripping & Query Parameter Reflection
```bash
curl -i "http://localhost:8000/echo/users/42?page=1&status=active"
```
**Expected Response:** `HTTP 200 OK` — Echo service receives path `/users/42` with query `page=1&status=active`.

### 2.2 Path Normalization (Consecutive Slashes)
```bash
curl -i "http://localhost:8000///echo//users///42"
```
**Expected Response:** `HTTP 200 OK` — Path collapsed to `/users/42`.

### 2.3 Method Filtering — 405 Method Not Allowed
```bash
curl -i -X DELETE http://localhost:8000/slow/compute
```
**Expected Response:** `HTTP 405 Method Not Allowed` with `allow: POST, GET` header.

### 2.4 Unmapped Route — 404 Route Not Found
```bash
curl -i http://localhost:8000/unmapped/resource
```
**Expected Response:** `HTTP 404 Not Found` with RFC 7807 JSON body (`error: "route_not_found"`).

### 2.5 Configured Disabled Route — 404 Route Not Found
```bash
curl -i http://localhost:8000/disabled/ping
```
**Expected Response:** `HTTP 404 Not Found` (`{"error": "route_not_found", "message": "No active route found for path /disabled/ping"}`).

---

## 3. RFC 7230 Header Stripping & Forwarding

### 3.1 Hop-by-Hop & Dynamic Connection Header Removal
```bash
curl -i http://localhost:8000/echo/test-hop \
  -H "Connection: close, X-Dynamic-Token" \
  -H "Keep-Alive: timeout=5" \
  -H "Upgrade: websocket" \
  -H "X-Dynamic-Token: secret-12345"
```
**Expected Response:** Upstream echo service receives none of the stripped headers.

### 3.2 Trace ID Propagation & Host Authority Rewriting
```bash
curl -i -H "X-Request-ID: test-trace-uuid-1234" http://localhost:8000/echo/headers
```
**Expected Response:** Response headers contain `x-request-id: test-trace-uuid-1234`, `x-response-time`, and `server-timing: gateway;dur=...`. Upstream receives `x-forwarded-for` and `host: echo-service:8001`.

---

## 4. Authentication & Role-Based Access Control (RBAC)

### Token Generation
```bash
# Valid Admin Token
ADMIN_TOKEN=$(uv run python scripts/generate_tokens.py --sub alice --roles admin)

# Viewer Token (insufficient role)
VIEWER_TOKEN=$(uv run python scripts/generate_tokens.py --sub bob --roles viewer)

# Expired Token
EXPIRED_TOKEN=$(uv run python scripts/generate_tokens.py --sub eve --roles admin --exp-minutes -10)
```

### 4.1 Valid Admin Token Admitted
```bash
curl -i -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/secure/admin-dashboard
```
**Expected Response:** `HTTP 200 OK` — Echoes injected headers `x-user-id: alice` and `x-user-roles: admin`.

### 4.2 Missing Authorization Header
```bash
curl -i http://localhost:8000/secure/admin-dashboard
```
**Expected Response:** `HTTP 401 Unauthorized` (`{"error": "unauthorized"}`).

### 4.3 Expired Token Rejected
```bash
curl -i -H "Authorization: Bearer $EXPIRED_TOKEN" http://localhost:8000/secure/admin-dashboard
```
**Expected Response:** `HTTP 401 Unauthorized` (`{"error": "token_expired"}`).

### 4.4 Invalid Signature Rejected
```bash
curl -i -H "Authorization: Bearer invalid.signature.token" http://localhost:8000/secure/admin-dashboard
```
**Expected Response:** `HTTP 401 Unauthorized` (`{"error": "invalid_token"}`).

### 4.5 Insufficient Role Rejected
```bash
curl -i -H "Authorization: Bearer $VIEWER_TOKEN" http://localhost:8000/secure/admin-dashboard
```
**Expected Response:** `HTTP 403 Forbidden` (`{"error": "forbidden"}`).

---

## 5. Sliding-Window Rate Limiting

### 5.1 Trigger Rate Limit Burst Exhaustion
```bash
for i in {1..8}; do
  curl -s -o /dev/null -w "Req $i: HTTP %{http_code}\n" http://localhost:8000/limited/resource
done
```
**Expected Output:**
```text
Req 1: HTTP 200
Req 2: HTTP 200
Req 3: HTTP 200
Req 4: HTTP 200
Req 5: HTTP 200
Req 6: HTTP 429
Req 7: HTTP 429
Req 8: HTTP 429
```

### 5.2 Inspect Rate Limit Headers
```bash
curl -i http://localhost:8000/limited/resource
```
**Expected Headers:**
- `HTTP/1.1 429 Too Many Requests`
- `retry-after: 60`
- `x-ratelimit-limit: 5`
- `x-ratelimit-remaining: 0`

---

## 6. Resilience, Timeouts & Circuit Breaker

### 6.1 Gateway Timeout (504)
Route configured with `timeout_seconds: 2.0`; requesting delay of 3.0s:
```bash
curl -i "http://localhost:8000/slow/compute?delay=3.0"
```
**Expected Response:** `HTTP 504 Gateway Timeout` in ~2000ms.

### 6.2 Circuit Breaker Trip to OPEN
Send 3 consecutive failures:
```bash
for i in {1..3}; do
  curl -s -o /dev/null "http://localhost:8000/flaky/error?fail_rate=1.0"
done
```
**Verify State:**
```bash
curl -s http://localhost:8000/admin/backends | jq '.[] | select(.url | contains("8003")) | .circuit_state'
```
**Output:** `"open"`

### 6.3 Immediate Fast-Fail While OPEN (503)
```bash
curl -i "http://localhost:8000/flaky/error?fail_rate=1.0"
```
**Expected Response:** `HTTP 503 Service Unavailable` returned immediately without forwarding upstream.

### 6.4 Circuit Breaker Recovery (HALF_OPEN -> CLOSED)
```bash
# Wait for recovery timeout (5s)
sleep 6

# Canary success probe
curl -i "http://localhost:8000/flaky/error?fail_rate=0.0"
```
**Expected Response:** `HTTP 200 OK` — Circuit resets to `"closed"`.

### 6.5 Route with All Unhealthy Backends (503)
```bash
curl -i http://localhost:8000/unhealthy/ping
```
**Expected Response:** `HTTP 503 Service Unavailable` (`{"error": "service_unavailable", "message": "No healthy upstream backends for route 'unhealthy-backend-route'"}`).

### 6.6 Upstream Connection Refused (502)
```bash
# Register dead route pointing to unused port
curl -s -X POST http://localhost:8000/admin/routes \
  -H "Content-Type: application/json" \
  -d '{"id":"dead-route","name":"Dead","path_pattern":"/dead/**","upstream_url":"http://127.0.0.1:9999","strip_prefix":"/dead"}' > /dev/null

curl -i http://localhost:8000/dead/ping

# Clean up
curl -s -X DELETE http://localhost:8000/admin/routes/dead-route > /dev/null
```
**Expected Response:** `HTTP 502 Bad Gateway` (`{"error": "bad_gateway"}`).

---

## 7. ASGI Streaming & Transfer Mechanics

### 7.1 Multi-Hash Chunked File Upload
```bash
curl -i -X POST http://localhost:8000/hasher/hash \
  --data-binary "The quick brown fox jumps over the lazy dog"
```
**Expected Response:** `HTTP 200 OK` with calculated MD5, SHA1, and SHA256 hashes.

### 7.2 Chunked Download Stream
```bash
curl -i "http://localhost:8000/echo/stream?count=5&delay_ms=10"
```
**Expected Response:** `HTTP 200 OK` with `transfer-encoding: chunked` and streaming NDJSON lines.

### 7.3 Streaming NDJSON Pipeline Transformation
```bash
printf '{"id": 1, "value": 10}\n{"id": 2, "value": 20}\n' | \
  curl -i -X POST http://localhost:8000/pipeline/pipeline/transform \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @-
```
**Expected Response:** `HTTP 200 OK` with transformed stream.

### 7.4 Idempotent Order Deduplication
```bash
# First request: Creates order
curl -i -X POST http://localhost:8000/pipeline/pipeline/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-order-uuid-101" \
  -d '{"order_id": "ORD-1", "amount": 99.99}'

# Second request: Replays cached order
curl -i -X POST http://localhost:8000/pipeline/pipeline/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-order-uuid-101" \
  -d '{"order_id": "ORD-1", "amount": 99.99}'
```
**Expected Response:** First request returns `HTTP 201 Created` (`"is_duplicate": false`); second request returns `HTTP 200 OK` (`"is_duplicate": true`).

---

## 8. Dynamic Route CRUD & Zero-Downtime Hot Reload

### 8.1 Dynamically Register Route
```bash
curl -i -X POST http://localhost:8000/admin/routes \
  -H "Content-Type: application/json" \
  -d '{
    "id": "qa-live-route",
    "name": "QA Live Route",
    "path_pattern": "/qa-live/**",
    "upstream_url": "http://echo-service:8001",
    "strip_prefix": "/qa-live"
  }'
```
**Expected Response:** `HTTP 201 Created`. Immediate traffic to `/qa-live/ping` returns `HTTP 200 OK`.

### 8.2 Toggle Route Status (Disable / Enable)
```bash
# Disable
curl -i -X PATCH http://localhost:8000/admin/routes/qa-live-route/status \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
# Traffic to /qa-live/ping returns 404

# Re-enable
curl -i -X PATCH http://localhost:8000/admin/routes/qa-live-route/status \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
# Traffic to /qa-live/ping returns 200
```

### 8.3 Zero-Downtime Storage Hot Reload
```bash
curl -i -X POST http://localhost:8000/admin/routes/reload
```
**Expected Response:** `HTTP 200 OK` (`{"success": true, "reloaded_count": ...}`).

### 8.4 Clean Up Route
```bash
curl -i -X DELETE http://localhost:8000/admin/routes/qa-live-route
```
**Expected Response:** `HTTP 200 OK`.
