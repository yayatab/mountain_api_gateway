import time
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 5.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._last_state_change: float = time.monotonic()
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> CircuitState:
        # Check if OPEN has expired and can transition to HALF_OPEN
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_state_change
            if elapsed >= self.recovery_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
                self._last_state_change = time.monotonic()
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def can_execute(self) -> bool:
        return self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        self._consecutive_failures = 0
        if self._state != CircuitState.CLOSED:
            self._state = CircuitState.CLOSED
            self._last_state_change = time.monotonic()

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.HALF_OPEN:
            # Probe failed; trip right back to OPEN
            self._state = CircuitState.OPEN
            self._last_state_change = time.monotonic()
        elif self._consecutive_failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._last_state_change = time.monotonic()

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_state_change = time.monotonic()
