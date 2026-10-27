from typing import Any

from pydantic import BaseModel, Field


class ServiceHealth(BaseModel):
    status: str = "healthy"
    service: str


class EchoResponse(BaseModel):
    method: str
    path: str
    query_params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    client_ip: str | None = None


class SlowResponse(BaseModel):
    status: str = "completed"
    delayed_seconds: float
    path: str


class FlakyResponse(BaseModel):
    status: str = "success"
    path: str
    fail_rate: float


class FileHashResponse(BaseModel):
    filename: str | None = None
    total_bytes: int
    chunk_count: int
    md5: str
    sha1: str
    sha256: str
    elapsed_seconds: float


class PipelineRecord(BaseModel):
    id: str
    event: str
    data: dict[str, Any] = Field(default_factory=dict)
    gateway_request_id: str | None = None
    processed_by_user: str | None = None


class OrderProcessResult(BaseModel):
    order_id: str
    idempotency_key: str
    user_id: str | None = None
    status: str = "processed"
    total_items: int = 0
    tags: set[str] = Field(default_factory=set)
    is_duplicate: bool = False
