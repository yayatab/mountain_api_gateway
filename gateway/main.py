import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from gateway.config import settings
from gateway.models.context import GatewayContext
from gateway.proxy.client import client_pool
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry

GATEWAY_DESCRIPTION = """
# Mountain API Gateway

A lightweight, high-performance API Gateway built for resilient reverse proxying, dynamic routing,
strict RFC 7230 header sanitization, and streaming data transfer.

## 🚀 How to Use the Gateway

### 1. Dynamic Routing
Requests routed through the gateway are matched against configured routes in the `RouteRegistry`:
- **Exact Path Matching**: Highest specificity (e.g. `/api/v1/users`).
- **Path Parameters**: Extracted dynamically into context (e.g. `/api/v1/users/{id}`).
- **Single Segment Wildcard**: Matches one path segment (e.g. `/api/v1/*`).
- **Recursive Wildcard (Glob)**: Matches nested path hierarchy (e.g. `/static/**`).
- **Priority Overrides**: Routes with higher explicit `priority` score match first.

### 2. Header Handling & Tracing
- **Hop-by-Hop Headers Stripped**: RFC 7230 §6.1 headers (`Connection`, `Keep-Alive`, `Upgrade`,
  etc.) and dynamic tokens in `Connection: <tokens>` are stripped before reaching upstreams.
- **Traceability (`X-Request-ID`)**: Every request is assigned a unique trace ID. If provided by
  the client, it is preserved; otherwise, a UUIDv4 is generated and injected.
- **Client Identity (`X-Forwarded-*`)**:
  - `X-Forwarded-For`: Appends client IP to any existing proxy chain.
  - `X-Forwarded-Host`: Preserves original client Host.
  - `X-Real-IP`: Set to the direct connecting client IP.
  - `Host`: Rewritten to target backend's network authority.

### 3. Error Responses
All gateway-level errors follow a unified, structured JSON format without stack trace leaks:
- `404 Not Found`: No active route pattern matches the requested path.
- `405 Method Not Allowed`: Path exists but does not accept the requested HTTP method (`Allow`).
- `502 Bad Gateway`: Upstream backend is unreachable, connection refused, or network dropped.
- `504 Gateway Timeout`: Upstream backend exceeded the configured route timeout.
"""

RESERVED_SYSTEM_PATHS = {
    "/health",
    "/api/guide",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/openapi.json",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup: initialize connection pool
    await client_pool.startup()
    # Load routes from file if configured
    if settings.routes_file_path:
        await app.state.registry.load_from_file(settings.routes_file_path)
    yield
    # Shutdown: cleanly close HTTP connections
    await client_pool.shutdown()


def create_app(
    registry: RouteRegistry | None = None,
    proxy_engine: ProxyEngine | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Mountain API Gateway",
        description=GATEWAY_DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.state.registry = registry or RouteRegistry()
    app.state.proxy_engine = proxy_engine or ProxyEngine()

    @app.get("/health", tags=["System"], summary="Gateway health check")
    async def health_check() -> dict[str, str]:
        """Health check endpoint indicating API Gateway status."""
        return {"status": "healthy", "service": "api-gateway"}

    @app.get(
        "/api/guide",
        tags=["System"],
        summary="Programmatic Gateway Client & Architecture Guide",
    )
    async def api_guide() -> dict[str, object]:
        """Returns structured metadata and guidelines on how clients interact with the gateway."""
        return {
            "name": "Mountain API Gateway",
            "version": "0.1.0",
            "matching_rules": [
                {"type": "exact", "example": "/api/users", "precedence": "Highest"},
                {"type": "parameter", "example": "/api/users/{id}", "precedence": "High"},
                {"type": "wildcard", "example": "/api/*", "precedence": "Medium"},
                {
                    "type": "glob",
                    "example": "/static/**",
                    "precedence": "Lowest (unless priority override)",
                },
            ],
            "injected_headers": [
                "X-Request-ID",
                "X-Forwarded-For",
                "X-Forwarded-Host",
                "X-Forwarded-Proto",
                "X-Real-IP",
            ],
            "error_codes": {
                "404": "No route matches requested path",
                "405": "Method not allowed for matched route (check Allow header)",
                "502": "Bad Gateway (upstream unreachable)",
                "504": "Gateway Timeout (upstream exceeded timeout limit)",
            },
        }

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def catch_all_proxy(request: Request, full_path: str) -> Response:
        normalized_path = "/" + full_path.lstrip("/")

        # Allow reserved system endpoints to execute their own route handlers
        if normalized_path in RESERVED_SYSTEM_PATHS:
            return Response(status_code=404)

        # Context initialization
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        client_ip = request.client.host if request.client else "127.0.0.1"
        context = GatewayContext(
            request_id=request_id,
            client_ip=client_ip,
        )

        # Route matching
        current_registry: RouteRegistry = app.state.registry
        match_result = current_registry.find_match(request.method, normalized_path)

        if match_result is None:
            # Check if path is valid for other HTTP methods
            allowed_methods = current_registry.get_allowed_methods(normalized_path)
            if allowed_methods:
                allow_header_val = ", ".join(m.value for m in allowed_methods)
                return JSONResponse(
                    status_code=405,
                    headers={"Allow": allow_header_val, "X-Request-ID": context.request_id},
                    content={
                        "error": "method_not_allowed",
                        "message": (
                            f"Method {request.method} is not allowed for path {normalized_path}"
                        ),
                        "request_id": context.request_id,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
            return JSONResponse(
                status_code=404,
                headers={"X-Request-ID": context.request_id},
                content={
                    "error": "route_not_found",
                    "message": f"No active route found for path {normalized_path}",
                    "request_id": context.request_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

        context.matched_route = match_result.route
        engine: ProxyEngine = app.state.proxy_engine

        return await engine.forward(
            request=request,
            context=context,
            target_base_url=match_result.route.upstream_url,
            target_path=match_result.target_path,
            timeout_seconds=match_result.route.timeout_seconds,
        )

    return app


app = create_app()
