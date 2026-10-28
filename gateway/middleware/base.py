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
    def __init__(
        self,
        middlewares: list[BaseMiddleware] | None = None,
        named_middlewares: dict[str, BaseMiddleware] | None = None,
    ) -> None:
        self._middlewares: list[BaseMiddleware] = middlewares or []
        self._named_middlewares: dict[str, BaseMiddleware] = named_middlewares or {}

    def register_named_middleware(self, name: str, middleware: BaseMiddleware) -> None:
        self._named_middlewares[name.lower()] = middleware

    def add_middleware(self, middleware: BaseMiddleware, name: str | None = None) -> None:
        self._middlewares.append(middleware)
        if name:
            self._named_middlewares[name.lower()] = middleware

    async def execute(
        self,
        request: Request,
        context: GatewayContext,
        terminal_handler: CallNext,
    ) -> Response:
        """Execute middleware in order, terminating with terminal_handler."""
        route = context.matched_route

        # Determine effective middleware sequence for this request
        if route is not None and bool(route.middleware_chain):
            # Route specifies a custom middleware sequence
            chain: list[BaseMiddleware] = []
            named_instances = set(self._named_middlewares.values())

            # Global middleware (e.g. Metrics/Tracing) runs first
            for mw in self._middlewares:
                if mw not in named_instances:
                    chain.append(mw)

            # Route-configured middlewares in the exact requested order
            for mw_name in route.middleware_chain:
                resolved_name = mw_name.lower().strip()
                if resolved_name in self._named_middlewares:
                    chain.append(self._named_middlewares[resolved_name])
        else:
            # Default pipeline sequence
            chain = self._middlewares

        async def dispatch(index: int, req: Request, ctx: GatewayContext) -> Response:
            if index < len(chain):
                current = chain[index]

                async def next_caller(r: Request, c: GatewayContext) -> Response:
                    return await dispatch(index + 1, r, c)

                return await current(req, ctx, next_caller)
            return await terminal_handler(req, ctx)

        try:
            return await dispatch(0, request, context)
        except Exception as err:
            logger.error(
                "Unhandled error in gateway pipeline [%s]: %s",
                context.request_id,
                err,
                exc_info=True,
            )
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
