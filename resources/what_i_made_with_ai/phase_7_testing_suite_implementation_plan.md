# Phase 7: Comprehensive Test Suite & Quality Hardening — Implementation Plan

## Goal Description
Elevate test coverage, resilience verification, and edge-case validation to production/interview-grade excellence (pushing coverage from **90% to >96%**):
1. **Health Checker Background Worker Lifecycle**: Test `HealthChecker.start()`, `stop()`, periodic probe loop execution, and graceful cancellation during FastAPI application lifespan.
2. **HTTP Connection Pool Management**: Verify `ProxyHttpClientPool.startup()` and `shutdown()` connection pool lifecycle, ensuring zero socket leaks.
3. **Redis Rate Limiting & Fallback Engine**: Test `RedisRateLimitEngine` sliding-window Lua execution, atomic token consumption, and seamless fallback to `InMemoryRateLimitEngine` when Redis is unreachable.
4. **Proxy & Routing Edge Cases**: Test malformed JSON files in `load_from_file()`, concurrent request proxying, path normalization edge cases (`//`, trailing slashes), and method validation fallback.
5. **Gateway Lifespan Integration**: Comprehensive integration test exercising application lifespan startup (pool + health checker + file load) and shutdown.

---

## User Review Required

> [!NOTE]
> All new tests will run fast (<1.5s total runtime) using mock transports and in-memory simulated timers, requiring no external network dependencies or running Redis servers.

> [!TIP]
> Coverage will be measured with `pytest-cov`, targeting >95% overall codebase coverage with 0 type errors in `pyright`.

---

## Proposed Changes

### 1. Health Checker & Lifespan Suite ([tests/test_service_discovery.py](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/tests/test_service_discovery.py))
#### [MODIFY] `tests/test_service_discovery.py`
Add tests for:
- `test_health_checker_background_task_lifecycle`: Tests `checker.start()`, periodic interval ticking, and `checker.stop()` cleanly cancelling background loop.
- `test_gateway_lifespan_startup_and_shutdown`: Tests the FastAPI lifespan context manager starting the HTTP pool and health checker, then stopping them cleanly on shutdown.

---

### 2. HTTP Client Connection Pool Suite ([tests/test_request_proxying.py](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/tests/test_request_proxying.py))
#### [MODIFY] `tests/test_request_proxying.py`
Add tests for:
- `test_http_client_pool_lifecycle`: Tests `client_pool.startup()`, `get_client()`, and `client_pool.shutdown()`.
- `test_client_pool_auto_startup`: Verifies client pool initializes automatically if requested before explicit startup.

---

### 3. Redis Rate Limiter & Fallback Suite ([tests/test_rate_limiting.py](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/tests/test_rate_limiting.py))
#### [MODIFY] `tests/test_rate_limiting.py`
Add tests for:
- `test_redis_rate_limit_engine_atomic_sliding_window`: Mock Redis client testing Lua script execution, window trimming, and rate limit calculation.
- `test_redis_engine_fallback_on_connection_error`: Verifies that if Redis encounters connection or timeout errors, `RateLimitMiddleware` falls back gracefully to in-memory rate limiting without dropping traffic.

---

### 4. File Persistence & Matcher Edge Cases ([tests/test_route_matching.py](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/tests/test_route_matching.py))
#### [MODIFY] `tests/test_route_matching.py`
Add tests for:
- `test_registry_load_missing_file_noop`: Loading from non-existent file path gracefully no-ops without exception.
- `test_registry_load_empty_file_noop`: Loading from empty file no-ops without exception.
- `test_route_matcher_empty_path_normalization`: Verifies normalizing `""` returns `"/"` and multiple slashes `///` are collapsed.

---

## Verification Plan

### Automated Tests
Run the comprehensive test suite with coverage reporting:
```bash
uv run pytest --cov=gateway --cov=mock_backend_services --cov-report=term-missing
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Target: **>95% Code Coverage**, 0 ruff errors, 0 pyright errors.
