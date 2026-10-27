from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExpectedResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status_code: int | None = None
    headers: dict[str, str] | None = None
    body_contains: str | list[str] | None = None
    body_not_contains: str | list[str] | None = None
    json_match: dict[str, Any] | list[Any] | None = Field(default=None, alias="json")
    regex: str | None = None


class CurlStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    curl: str | None = None
    service_action: dict[str, str] | None = None
    expected_response: ExpectedResponse | None = None


class SystemTestScenario(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="Name of the test scenario")
    description: str | None = None
    route: dict[str, Any] | None = None
    routes: list[dict[str, Any]] | None = None
    services: dict[str, str] | list[dict[str, str]] | None = None
    config: dict[str, Any] | None = None
    curl_commands: list[CurlStep] = Field(default_factory=list)
