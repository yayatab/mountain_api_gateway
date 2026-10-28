import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class BackendInstance(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    url: str = ""
    base_url: str = ""
    health_check_endpoint: str = "/health"
    health_check_interval: float = 10.0
    weight: int = 1
    status: HealthStatus = HealthStatus.HEALTHY
    consecutive_failures: int = 0
    last_checked_at: datetime | None = None
    last_health_check_time: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def sync_urls_and_times(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Sync base_url and url
            if "url" in data and "base_url" not in data:
                data["base_url"] = data["url"]
            elif "base_url" in data and "url" not in data:
                data["url"] = data["base_url"]

            # Sync last_checked_at and last_health_check_time
            if "last_checked_at" in data and "last_health_check_time" not in data:
                data["last_health_check_time"] = data["last_checked_at"]
            elif "last_health_check_time" in data and "last_checked_at" not in data:
                data["last_checked_at"] = data["last_health_check_time"]
        return data

    def __hash__(self) -> int:
        target = self.url or self.base_url
        return hash(target.rstrip("/"))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, BackendInstance):
            target_self = (self.url or self.base_url).rstrip("/")
            target_other = (other.url or other.base_url).rstrip("/")
            return target_self == target_other
        return False


class TargetPool(BaseModel):
    backends: set[BackendInstance] = Field(default_factory=set)
    strategy: str = "round_robin"


class BackendStatus(BaseModel):
    url: str
    status: HealthStatus
    consecutive_failures: int = 0
    circuit_state: str
    weight: int = 1
    last_checked_at: str | None = None
