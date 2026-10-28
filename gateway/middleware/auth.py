import logging
from datetime import UTC, datetime
from typing import Any

import jwt
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from gateway.config import settings
from gateway.middleware.base import BaseMiddleware, CallNext
from gateway.models.context import GatewayContext

logger = logging.getLogger("gateway.auth")


class AuthMiddleware(BaseMiddleware):
    def __init__(
        self,
        secret: str | None = None,
        algorithm: str | None = None,
    ) -> None:
        self._secret = secret or settings.jwt_secret
        self._algorithm = algorithm or settings.jwt_algorithm

    async def __call__(
        self,
        request: Request,
        context: GatewayContext,
        call_next: CallNext,
    ) -> Response:
        route = context.matched_route
        if route is None or not route.middleware.auth.enabled:
            return await call_next(request, context)

        auth_config = route.middleware.auth
        auth_header = request.headers.get("authorization")

        token: str | None = None
        if auth_header and auth_header.strip().lower().startswith("bearer "):
            token = auth_header.strip()[7:].strip()

        # Handle missing token
        if not token:
            if not auth_config.required:
                return await call_next(request, context)
            return JSONResponse(
                status_code=401,
                headers={"X-Request-ID": context.request_id},
                content={
                    "error": "unauthorized",
                    "message": "Authorization header with Bearer token is required",
                    "request_id": context.request_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

        # Validate JWT token
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
            )
        except jwt.ExpiredSignatureError as err:
            logger.warning("Authentication token expired [%s]: %s", context.request_id, err)
            return JSONResponse(
                status_code=401,
                headers={"X-Request-ID": context.request_id},
                content={
                    "error": "token_expired",
                    "message": "Authentication token has expired",
                    "request_id": context.request_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
        except jwt.InvalidTokenError as err:
            logger.warning("Invalid authentication token [%s]: %s", context.request_id, err)
            return JSONResponse(
                status_code=401,
                headers={"X-Request-ID": context.request_id},
                content={
                    "error": "invalid_token",
                    "message": "Invalid authentication token",
                    "request_id": context.request_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as err:
            logger.error(
                "Unexpected error validating authentication token [%s]: %s",
                context.request_id,
                err,
                exc_info=True,
            )
            return JSONResponse(
                status_code=500,
                headers={"X-Request-ID": context.request_id},
                content={
                    "error": "internal_error",
                    "message": "Unexpected error validating authentication token",
                    "request_id": context.request_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

        # Extract identity & roles
        user_id = payload.get("sub") or payload.get("user_id")
        context.user_id = str(user_id) if user_id is not None else None

        roles_claim = payload.get("roles") or payload.get("role") or []
        if isinstance(roles_claim, str):
            user_roles = {roles_claim}
        elif isinstance(roles_claim, (list, set)):
            user_roles = {str(r) for r in roles_claim}
        else:
            user_roles = set()
        context.user_roles = user_roles

        # Extract scopes
        scopes_claim = payload.get("scopes") or payload.get("scope") or []
        if isinstance(scopes_claim, str):
            token_scopes = set(scopes_claim.replace(",", " ").split())
        elif isinstance(scopes_claim, (list, set)):
            token_scopes = {str(s) for s in scopes_claim}
        else:
            token_scopes = set()

        context.auth_claims = payload

        # Role-based access control (RBAC)
        if auth_config.allowed_roles:
            has_role = bool(auth_config.allowed_roles & context.user_roles)
            if not has_role:
                return JSONResponse(
                    status_code=403,
                    headers={"X-Request-ID": context.request_id},
                    content={
                        "error": "forbidden",
                        "message": "User does not have the required role to access this route",
                        "request_id": context.request_id,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )

        # Scope-based access control
        if auth_config.required_scopes:
            has_scopes = auth_config.required_scopes.issubset(token_scopes)
            if not has_scopes:
                return JSONResponse(
                    status_code=403,
                    headers={"X-Request-ID": context.request_id},
                    content={
                        "error": "forbidden",
                        "message": "Token does not possess required scopes for this route",
                        "request_id": context.request_id,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )

        # Inject user identity into request scope headers for upstream forwarding
        scope_headers = list(request.scope.get("headers", []))
        if context.user_id:
            scope_headers.append((b"x-user-id", context.user_id.encode("utf-8")))
        if context.user_roles:
            sorted_roles = ",".join(sorted(context.user_roles))
            scope_headers.append((b"x-user-roles", sorted_roles.encode("utf-8")))
        request.scope["headers"] = scope_headers

        return await call_next(request, context)
