# Design Decisions

## 1. Route Matching Strategy

**Approach chosen:**
Pre-compiled Regex matching with multi-attribute ranking: Explicit Priority -> Specificity Score -> Pattern Length.

**Why:**
Provides maximum route flexibility (supporting exact paths, path parameters like `{id}`, single-segment wildcards `*`, and recursive globs `**`) while guaranteeing deterministic, sub-millisecond evaluation. Normalizing paths ensures consistency against irregular trailing slashes and redundant path separators.

**Overlapping routes resolved by:**
1. **Explicit Priority**: Route with higher `priority` integer always wins, allowing operators to force overrides for emergency maintenance or canary paths.
2. **Specificity Score**:
   - Exact paths: Score `1000 + len(pattern)`
   - Single-segment wildcard (`*`) & parameter (`{param}`): Score `500 + static_prefix_length`
   - Recursive globs (`**`): Score `100 + static_prefix_length`
3. **Pattern Length Tie-breaker**: More specific static prefixes win when scores match.

---

## 2. Header Handling

**Forwarded headers:**
- Standard application headers: `Authorization`, `Accept`, `Content-Type`, `User-Agent`, cookies, and custom headers.
- `X-Forwarded-For`: Existing chain preserved and appended with immediate client IP (`<existing>, <client_ip>`).
- `X-Forwarded-Host`: Preserves original client Host.
- `X-Forwarded-Proto`: Client scheme (`http` / `https`).
- `X-Forwarded-Port`: Backend port if present.

**Stripped headers:**
- **RFC 7230 §6.1 Hop-by-Hop headers**: `Connection`, `Keep-Alive`, `Proxy-Authenticate`, `Proxy-Authorization`, `TE`, `Trailers`, `Transfer-Encoding`, and `Upgrade`.
- **Dynamic Hop-by-Hop headers**: Any header token declared within the client's `Connection` header value (e.g. `Connection: close, X-Custom-Hop`).
- **Original Host**: Incoming client `Host` is removed to prevent virtual host / SNI routing failures at upstream backends.
- **Content-Length on Streaming**: Stripped on streamed responses to prevent HTTP framing desynchronization when upstream uses chunked encoding.

**Added headers:**
- `Host`: Rewritten to target backend network authority.
- `X-Request-ID`: Trace ID preserved if supplied by client, or newly generated UUIDv4, injected into upstream request and echoed in client response.
- `X-Real-IP`: Direct connecting client IP.

---

## 3. Middleware Chain Design

**Approach chosen:**
Chain of Responsibility / Asynchronous interceptor pipeline (`execute(context, call_next)`).

**Ordering:**
1. Context Initialization (Request ID, IP, Start Timer)
2. Authentication (JWT verification, role/scope validation)
3. Rate Limiting (Token bucket / Sliding window with Redis + memory fallback)
4. Request Transformation (Header stripping, path rewriting)
5. Streaming Proxy Dispatch
6. Response Transformation (CORS, Latency measurement, response headers)

**Data passing between middleware:**
A shared `GatewayContext` instance containing `request_id`, `client_ip`, `start_time`, `matched_route`, `user_id`, `user_roles`, `auth_claims`, and a mutable `state` dict.

---

## 4. Error Response Strategy

**Backend timeout:**
HTTP `504 Gateway Timeout` with structured JSON:
```json
{
  "error": "gateway_timeout",
  "message": "Upstream backend did not respond within 30.0s",
  "request_id": "<uuid>",
  "timestamp": "<iso8601>"
}
```

**Backend error:**
HTTP `502 Bad Gateway` on connection refused, network errors, or upstream drops with structured JSON:
```json
{
  "error": "bad_gateway",
  "message": "Failed to connect to upstream backend",
  "request_id": "<uuid>",
  "timestamp": "<iso8601>"
}
```

**Information exposure:**
Internal Python tracebacks, socket descriptors, and backend network addresses are strictly concealed from clients. All errors produce clean, consistent JSON envelopes, while detailed diagnostic traces are logged server-side with correlation `request_id`.

---

## 5. One Thing I Would Do Differently With More Time

Implement zero-copy kernel socket splicing (e.g. via Linux `splice(2)` or HTTP/2 & HTTP/3 multiplexing) for ultra-high-throughput streaming, and implement dynamic route compilation into a single compiled Radix Tree / DFA state machine for zero-allocation path traversal.
