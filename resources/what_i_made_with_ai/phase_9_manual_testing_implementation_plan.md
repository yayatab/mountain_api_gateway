# Phase 9: Manual Testing Playbook & Automated Demonstration Harness — Implementation Plan

## Goal Description
Build an end-to-end interactive manual testing harness and comprehensive demonstration playbook for human operators, interviewers, and QA engineers to verify every feature of the Mountain API Gateway in a live, running environment:
1. **Interactive Manual Testing Playbook (`MANUAL_TESTING.md`)**: Complete step-by-step testing instructions, curl commands, JSON payloads, and expected response envelopes for every gateway subsystem.
2. **Automated Live Verification Runner (`scripts/run_manual_tests.sh`)**: An executable shell script that connects to the live gateway, executes all test scenarios, validates status codes and response headers (`Retry-After`, `Server-Timing`, `X-Request-ID`), and prints color-coded results.
3. **JWT Token Generator Utility (`scripts/generate_tokens.py`)**: A developer CLI tool for generating test tokens with configurable subjects, roles, scopes, and expiration timestamps.
4. **Master Plan & Documentation Updates**: Update [`PLAN.md`](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/PLAN.md) and [`README.md`](file:///Users/yairtaboch/PycharmProjects/mountain_api_gateway/README.md) to integrate Phase 9.

---

## User Review Required

> [!NOTE]
> The automated test script (`scripts/run_manual_tests.sh`) tests against a live gateway running either via `docker compose up` or locally via `uvicorn gateway.main:app`. It provides instant pass/fail feedback and prints exact `curl` commands for manual inspection.

> [!TIP]
> The token generator utility (`scripts/generate_tokens.py`) reads `GATEWAY_JWT_SECRET` from the environment or uses the gateway default, making it simple to mint tokens for `admin`, `viewer`, or custom roles on demand.

---

## Proposed Changes

### 1. Developer Tooling & Scripts

#### [NEW] `scripts/generate_tokens.py`
A CLI utility to generate test tokens:
- `--sub <user_id>` (default: `alice`)
- `--roles <comma_separated>` (e.g. `admin,superadmin` or `viewer`)
- `--scopes <comma_separated>` (e.g. `read:reports,export:data`)
- `--exp-minutes <int>` (default: `60`, supports negative for expired tokens)
- `--secret <str>` (defaults to gateway secret)

#### [NEW] `scripts/run_manual_tests.sh`
An end-to-end executable test runner verifying live scenarios:
1. **Gateway Health Probe**: `GET http://localhost:8000/admin/health`
2. **Basic Proxying & Prefix Rewriting**: `GET http://localhost:8000/echo/users/42`
3. **JWT Auth - Valid Admin Token**: `GET http://localhost:8000/secure/admin-dashboard` (expects `200`)
4. **JWT Auth - Missing Token**: `GET http://localhost:8000/secure/admin-dashboard` (expects `401`)
5. **JWT Auth - Insufficient Role**: `GET http://localhost:8000/secure/admin-dashboard` with viewer token (expects `403`)
6. **JWT Auth - Expired Token**: Token with negative expiration (expects `401`)
7. **Sliding-Window Rate Limiting**: Burst 6 requests to `/limited/test` (expects 5x `200`, 1x `429` with `Retry-After`)
8. **Upstream Timeout (504)**: `GET /slow/compute?delay=3.0` (expects `504` in ~2.0s)
9. **Circuit Breaker Fault Injection**: Request to `/flaky/error?fail_rate=1.0` (expects `500`/`502`)
10. **Streaming File Hasher**: `POST /hasher/hash` with streaming payload, verify MD5/SHA1/SHA256 calculations
11. **Streaming NDJSON Pipeline**: `POST /pipeline/pipeline/transform` streaming multi-record transform
12. **Idempotent Order Processing**: `POST /pipeline/pipeline/orders` with `Idempotency-Key` (first creates, second replayed)
13. **Admin API Route Mutation & Hot Reload**: Dynamically register a new route, toggle status, and reload

---

### 2. Manual Testing Documentation

#### [NEW] `MANUAL_TESTING.md`
A structured playbook organized by subsystem:
- Environment Prerequisites (Docker vs Local)
- Test Matrix with curl commands, sample inputs, and anticipated outputs
- Troubleshooting Guide (port binding issues, Redis connection, Docker socket)

#### [MODIFY] `PLAN.md`
Update Phase roadmap to formally include Phase 9 (Manual Testing Playbook & Automated Demonstration Harness).

#### [MODIFY] `README.md`
Add a dedicated "Manual Testing & Demonstration Harness" section referencing `scripts/run_manual_tests.sh` and `MANUAL_TESTING.md`.

---

## Verification Plan

### Automated Execution
```bash
# Verify helper script syntax and imports
uv run python scripts/generate_tokens.py --roles admin
uv run python scripts/generate_tokens.py --roles viewer
uv run python scripts/generate_tokens.py --exp-minutes -10

# Verify script formatting & typing
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest --cov=gateway --cov=mock_backend_services --cov-report=term-missing
```

### End-to-End Live Execution
Run `scripts/run_manual_tests.sh` against the running gateway environment to verify all 13 test scenarios pass.
