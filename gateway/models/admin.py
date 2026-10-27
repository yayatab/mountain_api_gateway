from datetime import datetime

from pydantic import BaseModel, Field


class RouteStatusUpdate(BaseModel):
    enabled: bool


class GatewayHealth(BaseModel):
    status: str = "healthy"
    service: str = "mountain-api-gateway"
    version: str = "0.1.0"
    uptime_seconds: float
    timestamp: datetime = Field(default_factory=datetime.now)


class GatewayStats(BaseModel):
    total_routes: int
    active_routes: int
    disabled_routes: int
    total_backends: int
    healthy_backends: int
    unhealthy_backends: int


class ReloadResult(BaseModel):
    success: bool
    reloaded_count: int
    source: str
    reloaded_at: datetime = Field(default_factory=datetime.now)


class DeleteRouteResponse(BaseModel):
    message: str
    deleted: bool = True
