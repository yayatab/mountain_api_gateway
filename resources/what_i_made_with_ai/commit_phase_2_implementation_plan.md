# Git Commit Plan: Phase 2 Delivery & Project Artifacts

## Goal Description

Commit all completed, verified deliverables for **Phase 2: Request Proxying & Header Handling Engine**, including:
- Core proxy streaming engine and RFC 7230 header sanitization.
- Interactive `/docs` (Swagger UI), `/openapi.json`, and machine-readable `/api/guide`.
- Mock backend services (`echo_service`, `slow_service`, `flaky_service`).
- Comprehensive Phase 2 test suite (`test_header_transform.py`, `test_request_proxying.py`, `test_error_handling.py`).
- Design decisions ([DECISIONS.md](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/DECISIONS.md)), AI reflections ([AI_USAGE.MD](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/AI_USAGE.MD)), project guidelines ([GEMINI.md](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/GEMINI.md)), and tooling configs.

---

## User Review Required

> [!IMPORTANT]
> **AI_USAGE.MD & GEMINI.md Preservation**:
> Per the project guidelines defined in [GEMINI.md](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/GEMINI.md), the `## What I Had to Fix` section and accompanying screenshot `img.png` in [AI_USAGE.MD](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/AI_USAGE.MD) have been left completely untouched.

> [!NOTE]
> **Proposed Commit Structure**:
> We propose staging all modified and untracked Phase 2 files and creating a clean, structured commit:
> ```
> feat(proxy): implement Phase 2 streaming proxy, RFC 7230 header engine, /docs, and tests
> ```

---

## Proposed Changes to Commit

Grouped by component:

### 1. Gateway Core & Proxy Engine
- `gateway/proxy/headers.py` [NEW]: Case-insensitive RFC 7230 §6.1 hop-by-hop stripping, dynamic connection token pruning, `Host` rewriting, `X-Forwarded-*` chain, and `X-Request-ID`.
- `gateway/proxy/client.py` [NEW]: `httpx.AsyncClient` connection pool lifecycle management.
- `gateway/proxy/engine.py` [NEW]: True ASGI request/response chunk streaming with `request.stream()` and socket leak prevention.
- `gateway/main.py` [NEW]: FastAPI application with `/docs`, `/openapi.json`, `/api/guide`, `/health`, and catch-all routing.
- `gateway/models/route.py` [MODIFY]: Support for `set[HTTPMethod] | list[HTTPMethod]` and backwards-compatible `HttpMethod` alias.
- `gateway/routing/matcher.py` [MODIFY]: Clean formatting conforming to Ruff standard.

### 2. Mock Backend Services
- `mock_backend_services/echo_service.py` [NEW]: Reflection endpoint echoing method, headers, query, and payload.
- `mock_backend_services/slow_service.py` [NEW]: Configurable delay endpoint for timeout testing.
- `mock_backend_services/flaky_service.py` [NEW]: Synthetic failure endpoint.

### 3. Test Suite
- `tests/test_header_transform.py` [NEW]: 5 tests verifying hop-by-hop stripping and header rewriting.
- `tests/test_request_proxying.py` [NEW]: 4 tests verifying streaming GET/POST/PUT/DELETE and path translation.
- `tests/test_error_handling.py` [NEW]: 5 tests verifying 504 timeouts, 502 bad gateway, 404, 405 + `Allow`, and documentation routes.

### 4. Configuration & Documentation
- `pyproject.toml` [MODIFY]: Added Pyright venv resolution settings.
- `DECISIONS.md` [MODIFY]: Documented route matching, header handling, and error response decisions.
- `AI_USAGE.MD` [MODIFY]: Documented tools used, what helped most, and HTTP framing struggles.
- `GEMINI.md` [NEW]: User project guidelines for AI usage logging.
- `img.png` [NEW]: Visual evidence asset for human code corrections.

---

## Verification Plan

### Automated Checks Prior to Commit
Run all quality gates to ensure 100% clean status:
1. `uv run pytest -v` (27/27 tests passing)
2. `uv run ruff check .` (0 errors)
3. `uv run ruff format --check .` (0 files modified)
4. `uv run pyright` (0 errors, 0 warnings)

### Git Execution Command
Once approved:
```bash
git add .
git commit -m "feat(proxy): implement Phase 2 streaming proxy, RFC 7230 header engine, /docs, and tests"
```
