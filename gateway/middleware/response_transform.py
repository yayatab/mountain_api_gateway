import time

from fastapi import Request, Response

from gateway.middleware.base import BaseMiddleware, CallNext
from gateway.models.context import GatewayContext


class ResponseTransformMiddleware(BaseMiddleware):
    async def __call__(
        self,
        request: Request,
        context: GatewayContext,
        call_next: CallNext,
    ) -> Response:
        response = await call_next(request, context)

        # Calculate execution latency
        elapsed_ms = (time.perf_counter() - context.start_time) * 1000

        # Inject timing headers
        response.headers["X-Response-Time"] = f"{elapsed_ms:.2f}ms"
        response.headers["Server-Timing"] = f"gateway;dur={elapsed_ms:.2f}"

        # Inject request trace id
        if "X-Request-ID" not in response.headers:
            response.headers["X-Request-ID"] = context.request_id

        # Inject route-level custom response headers
        route = context.matched_route
        if route and route.middleware.transform.add_response_headers:
            for k, v in route.middleware.transform.add_response_headers.items():
                response.headers[k] = v

        return response
