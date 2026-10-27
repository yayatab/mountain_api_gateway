import uuid
from http import HTTPMethod

from pydantic import BaseModel, Field


class AuthMiddlewareConfig(BaseModel):
    enabled: bool = False
    required: bool = True
    allowed_roles: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)


class RateLimitConfig(BaseModel):
    enabled: bool = False
    requests_per_minute: int = 60
    burst_capacity: int = 10
    key_by: str = "ip"  # "ip", "user", or "api_key"


class TransformConfig(BaseModel):
    strip_prefix: str | None = None
    add_prefix: str | None = None
    add_request_headers: dict[str, str] = Field(default_factory=dict)
    remove_request_headers: list[str] = Field(default_factory=list)
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
    upstream_url: str
    strip_prefix: str | None = None
    add_prefix: str | None = None
    priority: int = 0
    timeout_seconds: float = 30.0
    enabled: bool = True
    middleware: MiddlewareConfig = Field(default_factory=MiddlewareConfig)


class MatchResult(BaseModel):
    route: RouteDefinition
    path_params: dict[str, str] = Field(default_factory=dict)
    target_path: str
