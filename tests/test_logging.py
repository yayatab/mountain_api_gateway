"""Unit and integration tests for structured logging (Console & JSON/ECS formats)."""

from __future__ import annotations

import json
import logging
from io import StringIO

import httpx
import pytest

from gateway.logging.formatter import ConsoleLogFormatter, JSONLogFormatter
from gateway.logging.setup import setup_logging
from gateway.main import create_app
from gateway.models.route import HTTPMethod, RouteDefinition
from gateway.routing.registry import RouteRegistry


def _create_record(
    name: str = "gateway.access",
    level: int = logging.INFO,
    msg: str = "Test message",
    args: tuple = (),
    exc_info=None,
    **extra,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=10,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


# ===========================================================================
# 1. JSON (ECS & PROMETHEUS) FORMATTER TESTS
# ===========================================================================


def test_json_formatter_standard_record():
    formatter = JSONLogFormatter(service_name="test-gateway", service_version="1.2.3")
    record = _create_record(msg="System initialized successfully")
    output = formatter.format(record)

    doc = json.loads(output)
    assert doc["log.level"] == "INFO"
    assert doc["log.logger"] == "gateway.access"
    assert doc["message"] == "System initialized successfully"
    assert doc["service.name"] == "test-gateway"
    assert doc["service.version"] == "1.2.3"
    assert "@timestamp" in doc
    assert doc["@timestamp"].endswith("Z")


def test_json_formatter_http_and_prometheus_correlation():
    formatter = JSONLogFormatter()
    record = _create_record(
        msg="GET /api/v1/orders -> 200 (1.45ms)",
        http_method="GET",
        path="/api/v1/orders",
        status_code=200,
        duration_ms=1.45,
        client_ip="192.168.1.100",
        request_id="req-uuid-42",
        route_id="orders-service-route",
        user_id="user_john",
    )
    output = formatter.format(record)
    doc = json.loads(output)

    # Elastic Common Schema (ECS) fields
    assert doc["trace.id"] == "req-uuid-42"
    assert doc["request_id"] == "req-uuid-42"
    assert doc["http"]["request"]["method"] == "GET"
    assert doc["http"]["request"]["path"] == "/api/v1/orders"
    assert doc["http"]["response"]["status_code"] == 200
    assert doc["http"]["response"]["duration_ms"] == 1.45
    assert doc["client"]["ip"] == "192.168.1.100"
    assert doc["client"]["user"]["id"] == "user_john"
    assert doc["route"]["id"] == "orders-service-route"

    # Prometheus Metric Label Alignment
    assert doc["prometheus"]["metric"] == "gateway_requests_total"
    assert doc["prometheus"]["labels"] == {
        "method": "GET",
        "route_id": "orders-service-route",
        "status_code": "200",
    }


def test_json_formatter_exception_handling():
    formatter = JSONLogFormatter()
    try:
        raise ValueError("Simulated network drop")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    record = _create_record(
        level=logging.ERROR,
        msg="Upstream call failed",
        exc_info=exc_info,
        http_method="POST",
        path="/flaky/unstable",
        status_code=502,
        route_id="flaky-route",
    )
    output = formatter.format(record)
    doc = json.loads(output)

    assert doc["log.level"] == "ERROR"
    assert doc["error"]["type"] == "ValueError"
    assert "Simulated network drop" in doc["error"]["message"]
    assert len(doc["error"]["stack_trace"]) > 0


# ===========================================================================
# 2. CONSOLE (HUMAN & LLM) FORMATTER TESTS
# ===========================================================================


def test_console_formatter_plain_terminal():
    formatter = ConsoleLogFormatter(enable_colors=False)
    record = _create_record(
        msg="GET /echo/ping -> 200 (0.85ms)",
        http_method="GET",
        path="/echo/ping",
        status_code=200,
        duration_ms=0.85,
        client_ip="127.0.0.1",
        request_id="abc12345-extra",
        route_id="echo-route",
        user_id="alice",
    )
    output = formatter.format(record)

    # Asserts easily readable line structure for humans and LLMs
    assert "[INFO]" in output
    assert "[gateway.access]" in output
    assert "[abc12345]" in output  # short request ID prefix
    assert "GET /echo/ping -> 200 OK (0.85ms)" in output
    assert "| route=echo-route ip=127.0.0.1 user=alice" in output
    assert "\033[" not in output  # No ANSI escapes when colors disabled


def test_console_formatter_system_log():
    formatter = ConsoleLogFormatter(enable_colors=False)
    record = _create_record(
        name="gateway.storage",
        level=logging.WARNING,
        msg="MongoDB ping failed; retrying connection",
    )
    output = formatter.format(record)

    assert "[WARNING]" in output
    assert "[gateway.storage]" in output
    assert "MongoDB ping failed; retrying connection" in output


def test_console_formatter_with_exception():
    formatter = ConsoleLogFormatter(enable_colors=False)
    try:
        raise RuntimeError("Disk full")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()

    record = _create_record(
        name="gateway.proxy",
        level=logging.ERROR,
        msg="Fatal proxy error",
        exc_info=exc_info,
    )
    output = formatter.format(record)

    assert "[ERROR]" in output
    assert "Fatal proxy error" in output
    assert "RuntimeError: Disk full" in output


# ===========================================================================
# 3. SETUP_LOGGING & GATEWAY INTEGRATION TESTS
# ===========================================================================


def test_setup_logging_configures_json_format():
    setup_logging(log_level="DEBUG", log_format="json")

    # Inspect root logger handler
    root = logging.getLogger()
    assert len(root.handlers) > 0
    assert isinstance(root.handlers[0].formatter, JSONLogFormatter)


def test_setup_logging_configures_console_format():
    setup_logging(log_level="INFO", log_format="console")
    root = logging.getLogger()
    assert len(root.handlers) > 0
    assert isinstance(root.handlers[0].formatter, ConsoleLogFormatter)


@pytest.mark.asyncio
async def test_gateway_emits_structured_logs_on_requests():
    """Verifies that an incoming request through the gateway emits a structured log

    captured by the JSON formatter containing all Prometheus labels and ECS fields.
    """
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONLogFormatter())

    access_logger = logging.getLogger("gateway.access")
    access_logger.handlers.clear()
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)

    route = RouteDefinition(
        id="logging-test-route",
        name="Logging Test Route",
        path_pattern="/log-test/**",
        methods={HTTPMethod.GET},
        upstream_url="http://echo-service",
    )
    registry = RouteRegistry(routes=[route])

    # Downstream echo simulation
    from starlette.responses import JSONResponse

    async def mock_echo(scope, receive, send):
        resp = JSONResponse({"echo": True})
        await resp(scope, receive, send)

    mock_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_echo),
        base_url="http://echo-service",
    )
    from gateway.proxy.engine import ProxyEngine

    app = create_app(registry=registry, proxy_engine=ProxyEngine(client=mock_client))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    )

    resp = await client.get("/log-test/hello", headers={"X-Request-ID": "test-req-999"})
    assert resp.status_code == 200

    log_output = stream.getvalue().strip()
    assert len(log_output) > 0

    doc = json.loads(log_output.splitlines()[-1])
    assert doc["trace.id"] == "test-req-999"
    assert doc["http"]["request"]["method"] == "GET"
    assert doc["http"]["request"]["path"] == "/log-test/hello"
    assert doc["http"]["response"]["status_code"] == 200
    assert doc["route"]["id"] == "logging-test-route"
    assert doc["prometheus"]["labels"]["route_id"] == "logging-test-route"
    assert doc["prometheus"]["labels"]["method"] == "GET"
    assert doc["prometheus"]["labels"]["status_code"] == "200"

    access_logger.removeHandler(handler)
