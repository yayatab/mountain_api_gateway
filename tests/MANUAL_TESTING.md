# Mountain API Gateway — Manual Testing Playbook

This playbook provides a comprehensive guide for developers, QA engineers, and interviewers to manually verify every capability of the **Mountain API Gateway** against a live running instance.

---

## 1. Environment Setup

Before running manual tests, start the gateway and upstream services using either Docker Compose or local Python processes:

### Option A: Docker Compose (Recommended)
```bash
docker compose up --build
```
This starts:
- **API Gateway**: `http://localhost:8000`
- **Echo Service**: `http://localhost:8001`
- **Slow Service**: `http://localhost:8002`
- **Flaky Service**: `http://localhost:8003`
- **Streaming Hasher Service**: `http://localhost:8004`
- **Data Pipeline Service**: `http://localhost:8005`
- **Redis Server**: `localhost:6379`

### Option B: Local Python Development
```bash
# Terminal 1: Start Gateway
GATEWAY_ROUTES_FILE_PATH=config/routes.json uv run uvicorn gateway.main:app --port 8000 --reload

# Terminal 2: Start Mock Services
uv run uvicorn mock_backend_services.echo_service:app --port 8001 &
uv run uvicorn mock_backend_services.slow_service:app --port 8002 &
uv run uvicorn mock_backend_services.flaky_service:app --port 8003 &
uv run uvicorn mock_backend_services.hasher_service:app --port 8004 &
uv run uvicorn mock_backend_services.data_pipeline_service:app --port 8005 &
```

---

## 2. Automated Live Verification Runner

You can execute the entire live manual test suite automatically using the bundled runner:

```bash
./scripts/run_manual_tests.sh
```

To test against a custom host or port:
```bash
GATEWAY_URL="http://127.0.0.1:8000" ./scripts/run_manual_tests.sh
```

---

## 3. Test Scenarios Matrix

| Scenario | Route Pattern | Target Backend | Key Feature Tested | Expected Status |
| :--- | :--- | :--- | :--- | :--- |
| **1. System Health** | `/admin/health` | Gateway Internal | Component health & uptime | `200 OK` |
| **2. Path Rewriting** | `/echo/users/42` | `echo-service:8001` | Prefix stripping & reflection | `200 OK` |
| **3. Hop-by-Hop Headers** | `/echo/headers` | `echo-service:8001` | RFC 7230 header stripping | `200 OK` |
| **4. Valid JWT Auth** | `/secure/dashboard` | `echo-service:8001` | RBAC `admin` role validation | `200 OK` |
| **5. Missing JWT Auth** | `/secure/dashboard` | `echo-service:8001` | Authentication enforcement | `401 Unauthorized` |
| **6. Forbidden Role** | `/secure/dashboard` | `echo-service:8001` | RBAC `viewer` role rejection | `403 Forbidden` |
| **7. Expired JWT** | `/secure/dashboard` | `echo-service:8001` | Expiration check | `401 Unauthorized` |
| **8. Rate Limiting** | `/limited/resource` | `echo-service:8001` | Burst quota & `Retry-After` | `429 Too Many Requests` |
| **9. Upstream Timeout** | `/slow/compute` | `slow-service:8002` | 2.0s gateway timeout | `504 Gateway Timeout` |
| **10. Circuit Breaker** | `/flaky/error` | `flaky-service:8003` | Fault handling & trip | `500` / `502 Bad Gateway` |
| **11. Streaming Hasher** | `/hasher/hash` | `hasher-service:8004` | Chunked MD5/SHA1/SHA256 | `200 OK` |
| **12. Streaming Download** | `/hasher/generate`| `hasher-service:8004` | Non-buffering chunk transfer | `200 OK` |
| **13. NDJSON Transform** | `/pipeline/transform`| `pipeline-service:8005` | Streaming line processing | `200 OK` |
| **14. Idempotent Orders**| `/pipeline/orders` | `pipeline-service:8005` | `Idempotency-Key` deduplication | `200 OK` |
| **15. Admin API CRUD** | `/admin/routes` | Gateway Internal | Dynamic route registration | `201 Created` |
| **16. Hot Reload** | `/admin/routes/reload`| Gateway Internal | Zero-downtime config reload | `200 OK` |
| **17. Prometheus Metrics**| `/metrics` | Gateway Internal | Request duration & counters | `200 OK` |

---

## 4. Step-by-Step Scenario Walkthrough

### Scenario 1: System Health & Active Stats
Check gateway status, route counts, and backend inventory:
```bash
curl -i http://localhost:8000/admin/health
curl -s http://localhost:8000/admin/stats | jq
curl -s http://localhost:8000/admin/backends | jq
```

---

### Scenario 2: Request Proxying & Path Rewriting
Route `/echo/**` has `strip_prefix: "/echo"`. The gateway removes `/echo` and forwards the remaining path:
```bash
curl -i "http://localhost:8000/echo/items/999?filter=active"
```
**Expected Response (`200 OK`):**
```json
{
  "method": "GET",
  "path": "/items/999",
  "headers": {
    "host": "echo-service:8001",
    "x-request-id": "<uuid>",
    "x-forwarded-for": "127.0.0.1",
    "x-real-ip": "127.0.0.1"
  },
  "query_params": {
    "filter": "active"
  }
}
```

---

### Scenario 3: Strict RFC 7230 Header Stripping
Send hop-by-hop headers and dynamic connection tokens. Verify the backend never receives them:
```bash
curl -i http://localhost:8000/echo/test \
  -H "Connection: close, X-Custom-Token" \
  -H "X-Custom-Token: secret-12345" \
  -H "Keep-Alive: timeout=5" \
  -H "Upgrade: websocket"
```
**Verification:**
In the echoed `headers` JSON, verify:
- `Connection`, `Keep-Alive`, `Upgrade`, and `X-Custom-Token` are **not present**.
- `Host` has been rewritten to `echo-service:8001`.
- `X-Request-ID` is present.

---

### Scenario 4: JWT Authentication & RBAC

Generate tokens using the helper CLI:
```bash
# Valid Admin Token
ADMIN_TOKEN=$(uv run python scripts/generate_tokens.py --sub alice --roles admin)

# Viewer Token (Missing admin role)
VIEWER_TOKEN=$(uv run python scripts/generate_tokens.py --sub bob --roles viewer)

# Expired Token
EXPIRED_TOKEN=$(uv run python scripts/generate_tokens.py --sub eve --roles admin --exp-minutes -10)
```

#### 4.1 Valid Admin Token (`200 OK`):
```bash
curl -i -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/secure/admin-dashboard
```

#### 4.2 Missing Authorization Header (`401 Unauthorized`):
```bash
curl -i http://localhost:8000/secure/admin-dashboard
```
```json
{
  "error": "unauthorized",
  "message": "Authorization header with Bearer token is required"
}
```

#### 4.3 Insufficient Role (`403 Forbidden`):
```bash
curl -i -H "Authorization: Bearer $VIEWER_TOKEN" http://localhost:8000/secure/admin-dashboard
```
```json
{
  "error": "forbidden",
  "message": "User does not have required roles"
}
```

#### 4.4 Expired Token (`401 Unauthorized`):
```bash
curl -i -H "Authorization: Bearer $EXPIRED_TOKEN" http://localhost:8000/secure/admin-dashboard
```
```json
{
  "error": "token_expired",
  "message": "Authentication token has expired"
}
```

---

### Scenario 5: Dual-Tier Sliding-Window Rate Limiting
Route `/limited/**` permits **5 requests per minute**:
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

Inspect the `429` headers for `Retry-After`:
```bash
curl -i http://localhost:8000/limited/resource | grep -E "(HTTP|X-RateLimit|Retry-After)"
```

---

### Scenario 6: Upstream Timeout (504 Gateway Timeout)
Route `/slow/**` has a configured timeout of **2.0 seconds**. Requesting a 3.5s delay triggers a timeout:
```bash
curl -i "http://localhost:8000/slow/compute?delay=3.5"
```
**Expected Response (`504 Gateway Timeout`):**
```json
{
  "error": "gateway_timeout",
  "message": "Upstream backend did not respond within 2.0s"
}
```

---

### Scenario 7: Streaming File Hasher
Upload a streaming payload without buffering. The Hasher calculates MD5, SHA1, and SHA256 on the fly:
```bash
curl -i -X POST http://localhost:8000/hasher/hash \
  --data-binary "High performance streaming test payload"
```
**Expected Response (`200 OK`):**
```json
{
  "status": "success",
  "total_bytes": 39,
  "chunk_count": 1,
  "md5": "b3ad3866f91cb9668ec0032b49dfda2e",
  "sha1": "ce517f41506eb3a77fc32d84950fa2e95a98bf35",
  "sha256": "8dc1e16f383e29f5f14e30491a92a54fb1ba704d2be7341ea22b392b45075c1a"
}
```

Download a generated 1MB binary stream:
```bash
curl -i "http://localhost:8000/hasher/generate?size_bytes=1048576&chunk_size=65536" -o /tmp/download.bin
```

---

### Scenario 8: Streaming NDJSON Pipeline & Idempotent Orders

#### 8.1 Stream Transformation:
```bash
printf '{"item": "book", "qty": 2}\n{"item": "pen", "qty": 10}\n' | \
  curl -i -X POST http://localhost:8000/pipeline/pipeline/transform \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @-
```

#### 8.2 Idempotent Order Creation:
```bash
IDEM_KEY="order-key-$(date +%s)"

# First request: Creates order
curl -i -X POST http://localhost:8000/pipeline/pipeline/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEM_KEY" \
  -d '{"order_id": "ORD-1", "amount": 100.0}'

# Second request with SAME key: Returns cached order with replayed: true
curl -i -X POST http://localhost:8000/pipeline/pipeline/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEM_KEY" \
  -d '{"order_id": "ORD-1", "amount": 100.0}'
```

---

### Scenario 9: Dynamic Admin API & Hot Reload

#### 9.1 Register a new route dynamically:
```bash
curl -i -X POST http://localhost:8000/admin/routes \
  -H "Content-Type: application/json" \
  -d '{
    "id": "dynamic-service",
    "name": "Dynamic Service",
    "path_pattern": "/dynamic/**",
    "methods": ["GET"],
    "upstream_url": "http://echo-service:8001",
    "strip_prefix": "/dynamic"
  }'
```

#### 9.2 Toggle Route Status:
```bash
# Disable route
curl -i -X PATCH http://localhost:8000/admin/routes/dynamic-service/status \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# Test route is disabled (404)
curl -i http://localhost:8000/dynamic/test

# Re-enable route
curl -i -X PATCH http://localhost:8000/admin/routes/dynamic-service/status \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

#### 9.3 Hot Reload from Disk:
```bash
curl -i -X POST http://localhost:8000/admin/routes/reload
```

---

### Scenario 10: Prometheus Metrics Scraping
Inspect Prometheus metric counters, status codes, and duration histograms:
```bash
curl -s http://localhost:8000/metrics | grep gateway_
```
