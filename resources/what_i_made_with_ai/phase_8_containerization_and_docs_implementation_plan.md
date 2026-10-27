# Phase 8: Containerization & Final Production Documentation — Implementation Plan

## Goal Description
Complete the final milestone of the Mountain API Gateway project:
1. **Production Multi-Stage Dockerfile**: Fast, reproducible container image built with `uv` and `python:3.12-slim`, adhering to security best practices (non-root `appuser`, no build tools in final runtime stage, explicit healthcheck).
2. **Multi-Service Docker Compose Environment**: Orchestrate Gateway (8000), Redis (6379), Echo (8001), Slow (8002), Flaky (8003), Hasher (8004), and Pipeline (8005) with dedicated health checks, network isolation, and volume persistence.
3. **Seed Gateway Configuration**: Pre-configured `config/routes.json` mapped into the container, exposing routes for all backend services, authentication, rate limiting, and circuit breaking out-of-the-box.
4. **Comprehensive Production `README.md`**: Complete documentation featuring architecture diagrams (Mermaid), quickstart, test execution, schema definitions, and interview-ready live `curl` command demonstrations with anticipated responses.
5. **Comprehensive `DECISIONS.md`**: In-depth explanations of architectural decisions (ASGI chunk streaming vs memory buffering, circuit breaker 3-state FSM, sliding-window rate limiting with atomic Lua pipelines, collection uniqueness via `set`, restart persistence design).
6. **Strict `AI_USAGE.MD` Review**: Ensure full compliance with project guidelines, maintaining the integrity of the human-edited sections.

---

## User Review Required

> [!NOTE]
> All services in `docker-compose.yml` utilize the single multi-stage Dockerfile with customized entrypoints (`gateway`, `mock_backend_services.echo_service`, `mock_backend_services.slow_service`, etc.), ensuring zero duplicated build definitions and rapid layer caching.

> [!TIP]
> The seed configuration `config/routes.json` enables instant live testing of all gateway capabilities (path routing, streaming MD5/SHA1/SHA256 hasher, idempotent orders, JWT RBAC, rate limiting, and 504 timeouts) immediately upon container startup.

---

## Proposed Changes

### 1. Containerization Layer

#### [MODIFY] `Dockerfile`
Multi-stage build utilizing `ghcr.io/astral-sh/uv:latest` and `python:3.12-slim`:
- Stage 1 (`builder`): Installs dependencies with `uv sync --frozen --no-dev`.
- Stage 2 (`runtime`): Copies virtualenv, creates non-root `appuser` (UID 10001), configures `PATH="/app/.venv/bin:$PATH"`, sets container healthcheck against `/admin/health`, and defaults to running the Gateway on port 8000.

#### [NEW] `config/routes.json`
Seed route definitions pre-registering:
- `/echo/**` -> `http://echo-service:8001`
- `/slow/**` -> `http://slow-service:8002` (2s timeout)
- `/flaky/**` -> `http://flaky-service:8003` (upstream pool with circuit breaker)
- `/hasher/**` -> `http://hasher-service:8004` (streaming file hasher)
- `/pipeline/**` -> `http://pipeline-service:8005` (NDJSON stream transform & idempotent orders)
- `/secure/**` -> `http://echo-service:8001` (JWT authentication, `admin` role required)
- `/limited/**` -> `http://echo-service:8001` (sliding window rate limit: 5 req/min)

#### [MODIFY] `docker-compose.yml`
Define services:
- `gateway`: Ports `8000:8000`, env vars for Redis and route persistence path, mounts `config/routes.json`, depends on all backends + redis.
- `redis`: Port `6379:6379`, Redis 7 Alpine with append-only persistence volume.
- `echo-service`: Port `8001:8001`.
- `slow-service`: Port `8002:8002`.
- `flaky-service`: Port `8003:8003`.
- `hasher-service`: Port `8004:8004`.
- `pipeline-service`: Port `8005:8005`.

---

### 2. Architecture & Design Decisions Documentation

#### [MODIFY] `DECISIONS.md`
Expand with detailed architectural justifications:
1. **True ASGI Streaming vs In-Memory Buffering**: Why chunk-by-chunk iteration over `request.stream()` and Starlette `StreamingResponse` prevents OOM on large uploads/downloads.
2. **Circuit Breaker Three-State FSM**: Transition mechanics (`CLOSED` -> `OPEN` -> `HALF_OPEN` -> `CLOSED`), background probing, and half-open single-canary isolation.
3. **Sliding-Window Rate Limiting with Atomic Lua**: Why sorted set timestamps with microsecond UUID keys prevent race conditions, and why transparent memory fallback prevents gateway cascading outages.
4. **Collection Uniqueness via `set`**: Why `BackendInstance`, `HTTPMethod`, `roles`, and header filters use sets to prevent duplicates and race conditions, while route evaluation preserves an ordered list for deterministic priority matching.
5. **Atomic File Persistence Across Restarts**: How atomic writes to disk on admin CRUD operations ensure zero configuration loss on process termination.

---

### 3. Production Documentation & User Guide

#### [MODIFY] `README.md`
Provide an interview-ready, comprehensive guide:
1. **Architecture & Request Flow**: High-level system diagram (Mermaid) and component breakdown.
2. **Key Capabilities & Standards**: RFC 7230 hop-by-hop handling, dynamic prefix rewriting, Prometheus metrics, admin API.
3. **Quickstart & Setup**: Running locally via `uv` or in containers via `docker compose up --build`.
4. **Test Suite & Verification**: Command guide for Pytest, coverage reports (98%), Ruff lint/format, and Pyright.
5. **Live `curl` Walkthrough**: Complete copy-pasteable demonstrations with anticipated JSON responses:
   - Dynamic routing & path prefix rewriting
   - Streaming MD5/SHA1/SHA256 file upload hashing
   - Streaming binary downloads
   - Streaming NDJSON data pipeline transformation
   - Idempotent order processing with `Idempotency-Key`
   - JWT authentication (valid token, missing token 401, missing role 403)
   - Dual-tier sliding window rate limiting (200 OK -> 429 Too Many Requests with `Retry-After`)
   - Upstream timeout trigger (504 Gateway Timeout)
   - Circuit breaker trip & failover (502 Bad Gateway)
   - Admin API route CRUD & hot reload without process restart
   - Prometheus metrics scraping (`/metrics`)

---

## Verification Plan

### Automated Checks
```bash
uv run pytest --cov=gateway --cov=mock_backend_services --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

### Configuration Validation
```bash
docker compose config
```

### Guidelines Verification
Verify that `## What I Had to Fix` in `AI_USAGE.MD` remains completely untouched.
