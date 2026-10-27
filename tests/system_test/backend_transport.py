from __future__ import annotations

import asyncio

import httpx

from mock_backend_services.data_pipeline_service import app as data_pipeline_app
from mock_backend_services.echo_service import app as echo_app
from mock_backend_services.flaky_service import app as flaky_app
from mock_backend_services.hasher_service import app as hasher_app
from mock_backend_services.slow_service import app as slow_app


class SystemBackendTransport(httpx.AsyncBaseTransport):
    """Mock backend transport simulating upstreams with dynamic UP/DOWN status."""

    def __init__(self, initial_services: dict[str, str] | None = None) -> None:
        self._services: dict[str, str] = {
            "echo-service": "up",
            "slow-service": "up",
            "flaky-service": "up",
            "hasher-service": "up",
            "pipeline-service": "up",
        }
        if initial_services:
            for name, status in initial_services.items():
                self.set_service_state(name, status)

        self._transports = {
            "echo-service": httpx.ASGITransport(app=echo_app),
            "slow-service": httpx.ASGITransport(app=slow_app),
            "flaky-service": httpx.ASGITransport(app=flaky_app),
            "hasher-service": httpx.ASGITransport(app=hasher_app),
            "pipeline-service": httpx.ASGITransport(app=data_pipeline_app),
        }

    def _normalize_service_name(self, name_or_host: str) -> str:
        cleaned = name_or_host.lower().strip()
        if "8001" in cleaned or "echo" in cleaned:
            return "echo-service"
        if "8002" in cleaned or "slow" in cleaned:
            return "slow-service"
        if "8003" in cleaned or "flaky" in cleaned:
            return "flaky-service"
        if "8004" in cleaned or "hasher" in cleaned:
            return "hasher-service"
        if "8005" in cleaned or "pipeline" in cleaned:
            return "pipeline-service"
        return cleaned

    def set_service_state(self, service_name: str, state: str) -> None:
        key = self._normalize_service_name(service_name)
        self._services[key] = state.lower().strip()

    def get_service_state(self, service_name: str) -> str:
        key = self._normalize_service_name(service_name)
        return self._services.get(key, "up")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        port = str(request.url.port or "")
        authority = f"{host}:{port}" if port else host

        service_key = self._normalize_service_name(authority)

        # Check if service is intentionally marked down
        current_state = self.get_service_state(service_key)
        if current_state == "down":
            raise httpx.ConnectError(
                f"Connection refused: simulated upstream service '{service_key}' is DOWN"
            )

        # Dispatch to known mock backend app
        transport = self._transports.get(service_key)
        if transport is not None:
            # Respect request timeout if provided in request extensions
            timeout = request.extensions.get("timeout", {}).get("read")
            if timeout is not None:
                try:
                    async with asyncio.timeout(timeout):
                        return await transport.handle_async_request(request)
                except TimeoutError as exc:
                    raise httpx.ReadTimeout(
                        f"Request to {authority} timed out after {timeout}s",
                        request=request,
                    ) from exc
            return await transport.handle_async_request(request)

        # Unregistered/unknown mock host
        raise httpx.ConnectError(
            f"Simulated connection refused: unknown upstream host '{authority}'"
        )
