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

    def __hash__(self) -> int:
        return hash(self.url.rstrip("/"))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, BackendInstance):
            return self.url.rstrip("/") == other.url.rstrip("/")
        return False


class TargetPool(BaseModel):
    backends: set[BackendInstance] = Field(default_factory=set)
    strategy: str = "round_robin"
