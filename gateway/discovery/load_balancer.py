from gateway.discovery.circuit_breaker import CircuitBreaker
from gateway.models.backend import BackendInstance, BackendStatus, HealthStatus
from gateway.models.route import RouteDefinition
from gateway.routing.registry import RouteRegistry


class NoHealthyBackendException(Exception):
    """Raised when no healthy or non-tripped backend instances are available."""


class LoadBalancer:
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 5.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_seconds
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._indices: dict[str, int] = {}

    def get_circuit_breaker(self, backend_url: str) -> CircuitBreaker:
        if backend_url not in self._circuit_breakers:
            self._circuit_breakers[backend_url] = CircuitBreaker(
                failure_threshold=self._failure_threshold,
                recovery_timeout_seconds=self._recovery_timeout,
            )
        return self._circuit_breakers[backend_url]

    def select_backend(self, route: RouteDefinition) -> str:
        instances = route.upstream_pool
        if not instances:
            if route.upstream_url:
                instances = {BackendInstance(url=route.upstream_url)}
            else:
                raise NoHealthyBackendException(
                    f"Route '{route.id}' has no upstream URL or pool configured"
                )

        # Filter instances that are healthy and whose circuit breaker allows execution
        eligible: list[BackendInstance] = []
        for inst in instances:
            cb = self.get_circuit_breaker(inst.url)
            if inst.status == HealthStatus.HEALTHY and cb.can_execute():
                eligible.append(inst)

        if not eligible:
            raise NoHealthyBackendException(
                f"No healthy upstream backend instances available for route '{route.id}'"
            )

        # Sort for deterministic round-robin ordering
        eligible.sort(key=lambda b: b.url)

        # Round-robin distribution across eligible instances
        idx = self._indices.get(route.id, 0)
        selected = eligible[idx % len(eligible)]
        self._indices[route.id] = idx + 1

        return selected.url

    def record_result(self, backend_url: str, success: bool) -> None:
        cb = self.get_circuit_breaker(backend_url)
        if success:
            cb.record_success()
        else:
            cb.record_failure()

    async def get_backends_status(self, registry: RouteRegistry) -> list[BackendStatus]:
        routes = await registry.list_all(include_disabled=True)
        backends_by_url: dict[str, BackendInstance] = {}

        for route in routes:
            if route.upstream_pool:
                for inst in route.upstream_pool:
                    backends_by_url[inst.url] = inst
            elif route.upstream_url:
                backends_by_url[route.upstream_url] = BackendInstance(
                    url=route.upstream_url,
                    status=HealthStatus.HEALTHY,
                )

        status_list: list[BackendStatus] = []
        for url, inst in backends_by_url.items():
            cb = self.get_circuit_breaker(url)
            status_list.append(
                BackendStatus(
                    url=url,
                    status=inst.status,
                    consecutive_failures=inst.consecutive_failures,
                    circuit_state=cb.state.value,
                    weight=inst.weight,
                    last_checked_at=(
                        inst.last_checked_at.isoformat() if inst.last_checked_at else None
                    ),
                )
            )

        return status_list
