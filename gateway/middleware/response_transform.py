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

        # Strip configured response headers
        route = context.matched_route
        if route and route.middleware.transform.remove_response_headers:
            remove_set = {h.lower() for h in route.middleware.transform.remove_response_headers}
            for h in list(response.headers.keys()):
                if h.lower() in remove_set:
                    del response.headers[h]

        # Inject route-level custom response headers
        if route and route.middleware.transform.add_response_headers:
            for k, v in route.middleware.transform.add_response_headers.items():
                response.headers[k] = v

        # Inject CORS headers if enabled
        if route and route.middleware.transform.cors_enabled:
            transform = route.middleware.transform
            origin = transform.cors_allow_origin
            if origin == "*" and request.headers.get("origin"):
                origin = "*"
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = transform.cors_allow_methods
            response.headers["Access-Control-Allow-Headers"] = transform.cors_allow_headers
            response.headers["Access-Control-Max-Age"] = str(transform.cors_max_age)
            if transform.cors_allow_credentials:
                response.headers["Access-Control-Allow-Credentials"] = "true"

        return response
