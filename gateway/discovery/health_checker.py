import asyncio
import contextlib
import logging
from datetime import UTC, datetime

import httpx

from gateway.models.backend import BackendInstance, HealthStatus
from gateway.routing.registry import RouteRegistry

logger = logging.getLogger("gateway.health_checker")


class HealthChecker:
    def __init__(
        self,
        registry: RouteRegistry,
        check_interval_seconds: float = 5.0,
        health_path: str = "/health",
        timeout_seconds: float = 1.5,
        failure_threshold: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.registry = registry
        self.check_interval_seconds = check_interval_seconds
        self.health_path = health_path
        self.timeout_seconds = timeout_seconds
        self.failure_threshold = failure_threshold
        self._custom_client = client
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._custom_client:
            return self._custom_client
        return httpx.AsyncClient(timeout=self.timeout_seconds)

    async def check_backend(
        self,
        client: httpx.AsyncClient,
        instance: BackendInstance,
    ) -> None:
        probe_url = f"{instance.url.rstrip('/')}{self.health_path}"
        try:
            resp = await client.get(probe_url, timeout=self.timeout_seconds)
            instance.last_checked_at = datetime.now(UTC)
            if 200 <= resp.status_code < 300:
                instance.status = HealthStatus.HEALTHY
                instance.consecutive_failures = 0
            else:
                instance.consecutive_failures += 1
                if instance.consecutive_failures >= self.failure_threshold:
                    instance.status = HealthStatus.UNHEALTHY
        except Exception:
            instance.last_checked_at = datetime.now(UTC)
            instance.consecutive_failures += 1
            if instance.consecutive_failures >= self.failure_threshold:
                instance.status = HealthStatus.UNHEALTHY

    async def check_all(self, client: httpx.AsyncClient | None = None) -> None:
        active_client = client or self._custom_client
        should_close = False
        if active_client is None:
            active_client = httpx.AsyncClient(timeout=self.timeout_seconds)
            should_close = True

        try:
            routes = await self.registry.list_all(include_disabled=False)
            unique_instances: dict[str, BackendInstance] = {}
            for route in routes:
                for inst in route.upstream_pool:
                    unique_instances[inst.url] = inst

            if unique_instances:
                tasks = [
                    self.check_backend(active_client, inst) for inst in unique_instances.values()
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if should_close:
                await active_client.aclose()

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.check_interval_seconds)
                await self.check_all()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in health check worker loop")

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
