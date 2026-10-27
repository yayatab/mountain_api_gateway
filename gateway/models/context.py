import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from gateway.models.route import RouteDefinition


class GatewayContext(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_ip: str = "127.0.0.1"
    start_time: float = Field(default_factory=time.perf_counter)
    matched_route: RouteDefinition | None = None
    user_id: str | None = None
    user_roles: list[str] = Field(default_factory=list)
    auth_claims: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
