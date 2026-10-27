from fastapi import Request, Response

from gateway.middleware.base import BaseMiddleware, CallNext
from gateway.models.context import GatewayContext


class RequestTransformMiddleware(BaseMiddleware):
    async def __call__(
        self,
        request: Request,
        context: GatewayContext,
        call_next: CallNext,
    ) -> Response:
        route = context.matched_route
        if route is None:
            return await call_next(request, context)

        transform_config = route.middleware.transform
        if not transform_config.add_request_headers and not transform_config.remove_request_headers:
            return await call_next(request, context)

        scope_headers = list(request.scope.get("headers", []))
        remove_lower = {
            h.lower().encode("latin-1") for h in transform_config.remove_request_headers
        }

        # Filter out headers to remove
        filtered = [(k, v) for k, v in scope_headers if k.lower() not in remove_lower]

        # Add new request headers
        for key, val in transform_config.add_request_headers.items():
            filtered.append((key.lower().encode("latin-1"), val.encode("latin-1")))

        request.scope["headers"] = filtered

        return await call_next(request, context)
