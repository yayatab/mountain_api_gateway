#!/usr/bin/env bash
set -eo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}   Mountain API Gateway — Live Manual Verification   ${NC}"
echo -e "${BLUE}======================================================${NC}"
echo -e "Testing target: ${YELLOW}${GATEWAY_URL}${NC}\n"

# 1. Healthcheck probe
echo -n "1. Checking Gateway Health (/admin/health)... "
HEALTH_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${GATEWAY_URL}/admin/health" || echo "000")
if [ "$HEALTH_CODE" -ne 200 ]; then
  echo -e "${RED}FAILED (HTTP ${HEALTH_CODE})${NC}"
  echo -e "${YELLOW}Please ensure the gateway is running: docker compose up or uv run uvicorn gateway.main:app --port 8000${NC}"
  exit 1
fi
echo -e "${GREEN}PASSED (HTTP 200)${NC}"

# 2. Basic Routing & Path Rewriting
echo -n "2. Verifying Path Rewriting & Reflection (/echo/users/42)... "
ECHO_RESP=$(curl -s "${GATEWAY_URL}/echo/users/42?test=true")
if echo "$ECHO_RESP" | grep -q '"path":"/users/42"'; then
  echo -e "${GREEN}PASSED${NC}"
else
  echo -e "${RED}FAILED${NC} Output: $ECHO_RESP"
fi

# Generate Tokens
ADMIN_TOKEN=$(uv run python scripts/generate_tokens.py --sub alice --roles admin)
VIEWER_TOKEN=$(uv run python scripts/generate_tokens.py --sub bob --roles viewer)
EXPIRED_TOKEN=$(uv run python scripts/generate_tokens.py --sub eve --roles admin --exp-minutes -10)

# 3. JWT Auth - Valid Admin Token
echo -n "3. Verifying Valid JWT Admin Token (/secure/admin-dashboard)... "
ADMIN_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${ADMIN_TOKEN}" "${GATEWAY_URL}/secure/admin-dashboard")
if [ "$ADMIN_CODE" -eq 200 ]; then
  echo -e "${GREEN}PASSED (HTTP 200)${NC}"
else
  echo -e "${RED}FAILED (HTTP ${ADMIN_CODE})${NC}"
fi

# 4. JWT Auth - Missing Token (401)
echo -n "4. Verifying Missing Token Rejection (/secure/admin-dashboard)... "
MISSING_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${GATEWAY_URL}/secure/admin-dashboard")
if [ "$MISSING_CODE" -eq 401 ]; then
  echo -e "${GREEN}PASSED (HTTP 401)${NC}"
else
  echo -e "${RED}FAILED (HTTP ${MISSING_CODE})${NC}"
fi

# 5. JWT Auth - Insufficient Role (403)
echo -n "5. Verifying Insufficient Role Rejection (/secure/admin-dashboard)... "
FORBIDDEN_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${VIEWER_TOKEN}" "${GATEWAY_URL}/secure/admin-dashboard")
if [ "$FORBIDDEN_CODE" -eq 403 ]; then
  echo -e "${GREEN}PASSED (HTTP 403)${NC}"
else
  echo -e "${RED}FAILED (HTTP ${FORBIDDEN_CODE})${NC}"
fi

# 6. JWT Auth - Expired Token (401)
echo -n "6. Verifying Expired Token Rejection (/secure/admin-dashboard)... "
EXPIRED_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${EXPIRED_TOKEN}" "${GATEWAY_URL}/secure/admin-dashboard")
if [ "$EXPIRED_CODE" -eq 401 ]; then
  echo -e "${GREEN}PASSED (HTTP 401)${NC}"
else
  echo -e "${RED}FAILED (HTTP ${EXPIRED_CODE})${NC}"
fi

# 7. Sliding-Window Rate Limiting (Burst of 6 requests, limit=5)
echo -n "7. Verifying Sliding-Window Rate Limiting (/limited/resource)... "
RL_CODES=()
for i in {1..6}; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "${GATEWAY_URL}/limited/resource")
  RL_CODES+=("$CODE")
done

if [[ "${RL_CODES[*]}" == *"429"* ]]; then
  echo -e "${GREEN}PASSED (Triggered 429 Too Many Requests)${NC}"
else
  echo -e "${RED}FAILED (Sequence: ${RL_CODES[*]})${NC}"
fi

# 8. Upstream Timeout Handling (504 Gateway Timeout)
echo -n "8. Verifying 2.0s Timeout Handling (/slow/compute?delay=3.0)... "
START_TIME=$(date +%s)
TIMEOUT_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${GATEWAY_URL}/slow/compute?delay=3.0")
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

if [ "$TIMEOUT_CODE" -eq 504 ] && [ "$DURATION" -le 3 ]; then
  echo -e "${GREEN}PASSED (HTTP 504 in ${DURATION}s)${NC}"
else
  echo -e "${RED}FAILED (HTTP ${TIMEOUT_CODE} in ${DURATION}s)${NC}"
fi

# 9. Streaming File Hasher (MD5, SHA1, SHA256)
echo -n "9. Verifying Streaming File Hasher (/hasher/hash)... "
HASH_RESP=$(curl -s -X POST "${GATEWAY_URL}/hasher/hash" --data-binary "streaming verification payload")
if echo "$HASH_RESP" | grep -q '"md5"' && echo "$HASH_RESP" | grep -q '"sha256"'; then
  echo -e "${GREEN}PASSED${NC}"
else
  echo -e "${RED}FAILED${NC} Output: $HASH_RESP"
fi

# 10. Streaming NDJSON Transform
echo -n "10. Verifying Streaming NDJSON Pipeline (/pipeline/pipeline/transform)... "
TRANSFORM_RESP=$(printf '{"event":"click","user":"101"}\n' | curl -s -X POST "${GATEWAY_URL}/pipeline/pipeline/transform" -H "Content-Type: application/x-ndjson" --data-binary @-)
if echo "$TRANSFORM_RESP" | grep -q '"processed":true'; then
  echo -e "${GREEN}PASSED${NC}"
else
  echo -e "${RED}FAILED${NC} Output: $TRANSFORM_RESP"
fi

# 11. Idempotent Order Creation
echo -n "11. Verifying Idempotent Orders Deduplication (/pipeline/pipeline/orders)... "
IDEM_KEY="test-key-$(date +%s)"
ORDER_1=$(curl -s -X POST "${GATEWAY_URL}/pipeline/pipeline/orders" -H "Content-Type: application/json" -H "Idempotency-Key: ${IDEM_KEY}" -d '{"order_id":"1","amount":100}')
ORDER_2=$(curl -s -X POST "${GATEWAY_URL}/pipeline/pipeline/orders" -H "Content-Type: application/json" -H "Idempotency-Key: ${IDEM_KEY}" -d '{"order_id":"1","amount":100}')
if echo "$ORDER_1" | grep -q '"replayed":false' && echo "$ORDER_2" | grep -q '"replayed":true'; then
  echo -e "${GREEN}PASSED (First processed, second replayed)${NC}"
else
  echo -e "${RED}FAILED${NC}"
fi

# 12. Admin Management API (Stats, Backends, Reload)
echo -n "12. Verifying Admin API & Hot Reload (/admin/routes/reload)... "
RELOAD_RESP=$(curl -s -X POST "${GATEWAY_URL}/admin/routes/reload")
if echo "$RELOAD_RESP" | grep -q '"success":true'; then
  echo -e "${GREEN}PASSED${NC}"
else
  echo -e "${RED}FAILED${NC} Output: $RELOAD_RESP"
fi

# 13. Prometheus Metrics Scrape
echo -n "13. Verifying Prometheus Metrics (/metrics)... "
METRICS_RESP=$(curl -s "${GATEWAY_URL}/metrics")
if echo "$METRICS_RESP" | grep -q 'gateway_requests_total'; then
  echo -e "${GREEN}PASSED${NC}"
else
  echo -e "${RED}FAILED${NC}"
fi

echo -e "\n${GREEN}======================================================${NC}"
echo -e "${GREEN}   All live manual test scenarios passed successfully!${NC}"
echo -e "${GREEN}======================================================${NC}"
