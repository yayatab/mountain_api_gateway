import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from gateway.models.context import GatewayContext

logger = logging.getLogger("gateway.middleware")

CallNext = Callable[[Request, GatewayContext], Awaitable[Response]]


class BaseMiddleware(ABC):
    @abstractmethod
    async def __call__(
        self,
        request: Request,
        context: GatewayContext,
        call_next: CallNext,
    ) -> Response:
        """Process an incoming request or response in the middleware pipeline."""


class MiddlewarePipeline:
    def __init__(self, middlewares: list[BaseMiddleware] | None = None) -> None:
        self._middlewares: list[BaseMiddleware] = list(middlewares) if middlewares else []

    def add_middleware(self, middleware: BaseMiddleware) -> None:
        self._middlewares.append(middleware)

    async def execute(
        self,
        request: Request,
        context: GatewayContext,
        terminal_handler: CallNext,
    ) -> Response:
        """Execute middleware in order, terminating with terminal_handler."""

        async def dispatch(index: int, req: Request, ctx: GatewayContext) -> Response:
            if index < len(self._middlewares):
                current = self._middlewares[index]

                async def next_caller(r: Request, c: GatewayContext) -> Response:
                    return await dispatch(index + 1, r, c)

                return await current(req, ctx, next_caller)
            return await terminal_handler(req, ctx)

        try:
            return await dispatch(0, request, context)
        except Exception:
            logger.exception("Unhandled error in gateway pipeline [%s]", context.request_id)
            return JSONResponse(
                status_code=500,
                headers={"X-Request-ID": context.request_id},
                content={
                    "error": "internal_error",
                    "message": "An internal server error occurred while processing the request",
                    "request_id": context.request_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
