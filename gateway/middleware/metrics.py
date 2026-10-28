import logging
import time

from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

from gateway.middleware.base import BaseMiddleware, CallNext
from gateway.models.context import GatewayContext

logger = logging.getLogger("gateway.access")

REQUEST_COUNT = Counter(
    "gateway_requests_total",
    "Total count of requests processed by the gateway",
    ["method", "route_id", "status_code"],
)

REQUEST_DURATION = Histogram(
    "gateway_request_duration_seconds",
    "Request latency in seconds",
    ["method", "route_id"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


def render_metrics_endpoint() -> Response:
    """Renders Prometheus metrics payload for scraping."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


class MetricsMiddleware(BaseMiddleware):
    async def __call__(
        self,
        request: Request,
        context: GatewayContext,
        call_next: CallNext,
    ) -> Response:
        start_time = time.perf_counter()
        response = await call_next(request, context)
        duration = time.perf_counter() - start_time

        route_id = context.matched_route.id if context.matched_route else "unknown"
        method = request.method
        status_code = str(response.status_code)

        REQUEST_COUNT.labels(
            method=method,
            route_id=route_id,
            status_code=status_code,
        ).inc()

        REQUEST_DURATION.labels(
            method=method,
            route_id=route_id,
        ).observe(duration)

        extra_fields = {
            "http_method": method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": duration * 1000,
            "client_ip": context.client_ip,
            "request_id": context.request_id,
            "route_id": route_id,
            "user_id": context.user_id or "anonymous",
        }

        logger.info(
            "%s %s -> %s (%.2fms)",
            method,
            request.url.path,
            status_code,
            duration * 1000,
            extra=extra_fields,
        )

        return response
