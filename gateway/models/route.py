import uuid
from http import HTTPMethod
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from gateway.models.backend import BackendInstance

HttpMethod = HTTPMethod

VALID_MIDDLEWARE_NAMES: set[str] = {
    "auth",
    "rate_limit",
    "request_transform",
    "response_transform",
}

MIDDLEWARE_ALIASES: dict[str, str] = {
    "jwt": "auth",
    "authentication": "auth",
    "ratelimit": "rate_limit",
    "rate_limiter": "rate_limit",
    "transform_request": "request_transform",
    "transform_response": "response_transform",
}


class AuthMiddlewareConfig(BaseModel):
    enabled: bool = False
    required: bool = True
    allowed_roles: set[str] = Field(default_factory=set)
    required_scopes: set[str] = Field(default_factory=set)


class RateLimitConfig(BaseModel):
    enabled: bool = False
    requests_per_minute: int = 60
    burst_capacity: int = 10
    key_by: str = "ip"  # "ip", "user", or "api_key"


class TransformConfig(BaseModel):
    strip_prefix: str | None = None
    add_prefix: str | None = None
    add_request_headers: dict[str, str] = Field(default_factory=dict)
    remove_request_headers: set[str] = Field(default_factory=set)
    add_response_headers: dict[str, str] = Field(default_factory=dict)


class MiddlewareConfig(BaseModel):
    auth: AuthMiddlewareConfig = Field(default_factory=AuthMiddlewareConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    transform: TransformConfig = Field(default_factory=TransformConfig)


class RouteDefinition(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    path_pattern: str
    methods: set[HTTPMethod] = Field(default_factory=set)
    upstream_url: str = ""
    upstream_pool: set[BackendInstance] = Field(default_factory=set)
    strip_prefix: str | None = None
    add_prefix: str | None = None
    priority: int = 0
    timeout_seconds: float = 30.0
    enabled: bool = True
    middleware: MiddlewareConfig = Field(default_factory=MiddlewareConfig)
    middleware_chain: list[str] = Field(
        default_factory=list,
        description="Explicit ordered list of middlewares. Duplicates are forbidden.",
    )

    @model_validator(mode="before")
    @classmethod
    def handle_middleware_chain_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "middlewares" in data and "middleware_chain" not in data:
            data["middleware_chain"] = data["middlewares"]
        return data

    @field_validator("middleware_chain", mode="before")
    @classmethod
    def validate_and_normalize_middleware_chain(cls, v: Any) -> list[str] | None:
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("middleware_chain must be a list of middleware name strings")

        seen: set[str] = set()
        normalized_chain: list[str] = []
        for item in v:
            if not isinstance(item, str):
                raise ValueError("Each middleware in middleware_chain must be a string")
            raw_name = item.strip().lower()
            canonical_name = MIDDLEWARE_ALIASES.get(raw_name, raw_name)
            if canonical_name not in VALID_MIDDLEWARE_NAMES:
                valid_list = ", ".join(sorted(VALID_MIDDLEWARE_NAMES))
                raise ValueError(
                    f"Unknown middleware '{item}'. Valid middlewares are: {valid_list}"
                )
            if canonical_name in seen:
                raise ValueError(
                    f"Cannot configure duplicate middleware '{item}' on the same route."
                )
            seen.add(canonical_name)
            normalized_chain.append(canonical_name)
        return normalized_chain


class MatchResult(BaseModel):
    route: RouteDefinition
    path_params: dict[str, str] = Field(default_factory=dict)
    target_path: str
