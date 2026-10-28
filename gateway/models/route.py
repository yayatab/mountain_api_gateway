import uuid
from datetime import UTC, datetime
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

    @model_validator(mode="before")
    @classmethod
    def normalize_auth_config(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "scopes" in data and "required_scopes" not in data:
                data["required_scopes"] = data.pop("scopes")
            has_auth_fields = (
                "required_scopes" in data or "allowed_roles" in data or "required" in data
            )
            if "enabled" not in data and has_auth_fields:
                data["enabled"] = True
        return data


class RateLimitConfig(BaseModel):
    enabled: bool = False
    requests_per_minute: int = 60
    burst_capacity: int = 10
    key_by: str = "ip"  # "ip", "user", or "api_key"

    @model_validator(mode="before")
    @classmethod
    def normalize_rate_limit_config(cls, data: Any) -> Any:
        if isinstance(data, dict):
            has_rl_fields = (
                "requests_per_minute" in data or "burst_capacity" in data or "key_by" in data
            )
            if "enabled" not in data and has_rl_fields:
                data["enabled"] = True
        return data


class TransformConfig(BaseModel):
    strip_prefix: str | None = None
    add_prefix: str | None = None
    add_request_headers: dict[str, str] = Field(default_factory=dict)
    remove_request_headers: set[str] = Field(default_factory=set)
    add_response_headers: dict[str, str] = Field(default_factory=dict)
    remove_response_headers: set[str] = Field(default_factory=set)
    cors_enabled: bool = False
    cors_allow_origin: str = "*"
    cors_allow_methods: str = "*"
    cors_allow_headers: str = "*"
    cors_allow_credentials: bool = False
    cors_max_age: int = 86400

    @model_validator(mode="before")
    @classmethod
    def normalize_transform_config(cls, data: Any) -> Any:
        if isinstance(data, dict) and "cors" in data and "cors_enabled" not in data:
            cors_val = data.pop("cors")
            if isinstance(cors_val, bool):
                data["cors_enabled"] = cors_val
            elif isinstance(cors_val, dict):
                data["cors_enabled"] = True
                for k, v in cors_val.items():
                    data[f"cors_{k}"] = v
        return data


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def handle_middleware_chain_alias(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Map path -> path_pattern
            if "path" in data and "path_pattern" not in data:
                data["path_pattern"] = data.pop("path")
            # Map target -> upstream_url
            if "target" in data and "upstream_url" not in data:
                data["upstream_url"] = data.pop("target")
            # Map middleware_config -> middleware
            if "middleware_config" in data and "middleware" not in data:
                data["middleware"] = data.pop("middleware_config")
            # Map middlewares -> middleware_chain
            if "middlewares" in data and "middleware_chain" not in data:
                data["middleware_chain"] = data["middlewares"]

            # Auto-enable middlewares listed in chain if configured
            chain = data.get("middleware_chain") or data.get("middlewares") or []
            if isinstance(chain, list):
                mw_set = {str(m).strip().lower() for m in chain}
                if "auth" in mw_set or "jwt" in mw_set:
                    data.setdefault("middleware", {})
                    if isinstance(data["middleware"], dict):
                        data["middleware"].setdefault("auth", {})
                        if isinstance(data["middleware"]["auth"], dict):
                            data["middleware"]["auth"].setdefault("enabled", True)
                if "rate_limit" in mw_set or "ratelimit" in mw_set:
                    data.setdefault("middleware", {})
                    if isinstance(data["middleware"], dict):
                        data["middleware"].setdefault("rate_limit", {})
                        if isinstance(data["middleware"]["rate_limit"], dict):
                            data["middleware"]["rate_limit"].setdefault("enabled", True)
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
