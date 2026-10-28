"""Structured logging formatters for Mountain API Gateway.

Provides:
- JSONLogFormatter: Elastic Common Schema (ECS) & Prometheus-aligned JSON formatter
  for ingestion by Elasticsearch, Logstash, Vector, and Datadog.
- ConsoleLogFormatter: Human- and LLM-friendly aligned terminal formatter with
  optional ANSI color-coding and clean metadata alignment.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

# Standard ANSI color escapes for terminal display
_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[2m"
_ANSI_BOLD = "\033[1m"
_ANSI_CYAN = "\033[0;36m"
_ANSI_GREEN = "\033[0;32m"
_ANSI_YELLOW = "\033[0;33m"
_ANSI_RED = "\033[0;31m"
_ANSI_MAGENTA = "\033[0;35m"

_LEVEL_COLORS: dict[str, str] = {
    "DEBUG": _ANSI_CYAN,
    "INFO": _ANSI_GREEN,
    "WARNING": _ANSI_YELLOW,
    "ERROR": _ANSI_RED,
    "CRITICAL": f"{_ANSI_BOLD}{_ANSI_RED}",
}

# Standard HTTP status code phrases for quick human / LLM readability
_STATUS_PHRASES: dict[int, str] = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class JSONLogFormatter(logging.Formatter):
    """Elastic Common Schema (ECS) compliant JSON formatter.

    Structures gateway events with 1:1 mapping to Prometheus metrics dimensions
    for seamless log-to-metric correlation in Grafana and Elasticsearch / Kibana.
    """

    def __init__(
        self,
        service_name: str = "mountain-api-gateway",
        service_version: str = "0.1.0",
    ) -> None:
        super().__init__()
        self.service_name = service_name
        self.service_version = service_version

    def format(self, record: logging.LogRecord) -> str:
        timestamp = (
            datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )

        doc: dict[str, Any] = {
            "@timestamp": timestamp,
            "log.level": record.levelname,
            "log.logger": record.name,
            "message": record.getMessage(),
            "service.name": self.service_name,
            "service.version": self.service_version,
        }

        # Request / Trace Correlation
        request_id = getattr(record, "request_id", None) or getattr(record, "trace_id", None)
        if request_id:
            doc["trace.id"] = request_id
            doc["request_id"] = request_id

        # HTTP Request and Response Context
        method = getattr(record, "http_method", None)
        path = getattr(record, "path", None)
        status_code = getattr(record, "status_code", None)
        duration_ms = getattr(record, "duration_ms", None)

        if method or path or status_code is not None:
            http_doc: dict[str, Any] = {}
            if method or path:
                http_doc["request"] = {}
                if method:
                    http_doc["request"]["method"] = str(method).upper()
                if path:
                    http_doc["request"]["path"] = str(path)
            if status_code is not None or duration_ms is not None:
                http_doc["response"] = {}
                if status_code is not None:
                    try:
                        http_doc["response"]["status_code"] = int(status_code)
                    except (ValueError, TypeError):
                        http_doc["response"]["status_code"] = status_code
                if duration_ms is not None:
                    http_doc["response"]["duration_ms"] = round(float(duration_ms), 2)
            doc["http"] = http_doc

        # Client and Route Identity
        client_ip = getattr(record, "client_ip", None)
        user_id = getattr(record, "user_id", None)
        route_id = getattr(record, "route_id", None)

        if client_ip or user_id:
            client_doc: dict[str, Any] = {}
            if client_ip:
                client_doc["ip"] = str(client_ip)
            if user_id and user_id != "anonymous":
                client_doc["user"] = {"id": str(user_id)}
            doc["client"] = client_doc

        if route_id:
            doc["route"] = {"id": str(route_id)}

        # Prometheus Metric Label Alignment
        if method and route_id and status_code is not None:
            doc["prometheus"] = {
                "metric": "gateway_requests_total",
                "labels": {
                    "method": str(method).upper(),
                    "route_id": str(route_id),
                    "status_code": str(status_code),
                },
            }

        # Error / Exception Details
        if record.exc_info:
            exc_type, exc_val, exc_tb = record.exc_info
            doc["error"] = {
                "type": exc_type.__name__ if exc_type else "Exception",
                "message": str(exc_val) if exc_val else "",
                "stack_trace": traceback.format_exception(exc_type, exc_val, exc_tb),
            }

        return json.dumps(doc, ensure_ascii=False)


class ConsoleLogFormatter(logging.Formatter):
    """Human- and LLM-friendly aligned terminal formatter.

    Produces concise, easily scannable, color-coded lines:
    [2026-10-28 02:15:30.123] [INFO] [gateway.access] [c1537c5f]
    GET /echo/users -> 200 OK (2.34ms) | route=echo-route ip=127.0.0.1
    """

    def __init__(self, enable_colors: bool = True) -> None:
        super().__init__()
        self.enable_colors = enable_colors and sys.stdout.isatty()

    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        level = record.levelname
        if self.enable_colors:
            color = _LEVEL_COLORS.get(level, "")
            level_str = f"[{color}{_ANSI_BOLD}{level}{_ANSI_RESET}]"
            time_str = f"{_ANSI_DIM}{dt}{_ANSI_RESET}"
            logger_str = f"{_ANSI_CYAN}[{record.name}]{_ANSI_RESET}"
        else:
            level_str = f"[{level}]"
            time_str = dt
            logger_str = f"[{record.name}]"

        # Request ID tag
        raw_req_id = getattr(record, "request_id", None) or getattr(record, "trace_id", None)
        if raw_req_id:
            short_id = raw_req_id[:8] if len(raw_req_id) >= 8 else raw_req_id
            req_tag = f"[{short_id}] "
            if self.enable_colors:
                req_tag = f"{_ANSI_MAGENTA}{req_tag}{_ANSI_RESET}"
        else:
            req_tag = ""

        # Check if structured HTTP access log
        method = getattr(record, "http_method", None)
        path = getattr(record, "path", None)
        status_code = getattr(record, "status_code", None)
        duration_ms = getattr(record, "duration_ms", None)

        if method and path and status_code is not None:
            phrase = _STATUS_PHRASES.get(int(status_code), "")
            dur_str = f"({duration_ms:.2f}ms)" if duration_ms is not None else ""

            if self.enable_colors:
                st_int = int(status_code)
                st_color = (
                    _ANSI_GREEN
                    if st_int < 300
                    else (
                        _ANSI_YELLOW
                        if st_int < 400
                        else (_ANSI_YELLOW if st_int < 500 else _ANSI_RED)
                    )
                )
                http_part = (
                    f"{_ANSI_BOLD}{method}{_ANSI_RESET} {path} -> "
                    f"{st_color}{_ANSI_BOLD}{status_code} {phrase}{_ANSI_RESET} "
                    f"{_ANSI_DIM}{dur_str}{_ANSI_RESET}"
                )
            else:
                http_part = f"{method} {path} -> {status_code} {phrase} {dur_str}"

            # Append structured metadata items
            extras: list[str] = []
            route_id = getattr(record, "route_id", None)
            if route_id:
                extras.append(f"route={route_id}")
            client_ip = getattr(record, "client_ip", None)
            if client_ip:
                extras.append(f"ip={client_ip}")
            user_id = getattr(record, "user_id", None)
            if user_id and user_id != "anonymous":
                extras.append(f"user={user_id}")

            extra_str = f" | {' '.join(extras)}" if extras else ""
            line = f"{time_str} {level_str} {logger_str} {req_tag}{http_part}{extra_str}"
        else:
            # General system message
            msg = record.getMessage()
            line = f"{time_str} {level_str} {logger_str} {req_tag}{msg}"

        # Append exception traceback if present
        if record.exc_info:
            tb_lines = traceback.format_exception(*record.exc_info)
            line += "\n" + "".join(tb_lines).rstrip()

        return line
