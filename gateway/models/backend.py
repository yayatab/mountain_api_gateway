from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class BackendInstance(BaseModel):
    url: str
    weight: int = 1
    status: HealthStatus = HealthStatus.HEALTHY
    consecutive_failures: int = 0
    last_checked_at: datetime | None = None


class TargetPool(BaseModel):
    backends: list[BackendInstance] = Field(default_factory=list)
    strategy: str = "round_robin"
