from __future__ import annotations

import base64
import shlex
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import jwt

from gateway.config import settings


def generate_test_tokens() -> dict[str, str]:
    now = datetime.now(UTC)
    secret = settings.jwt_secret
    algo = settings.jwt_algorithm

    def _mint(sub: str, roles: list[str], scopes: list[str], delta_minutes: int) -> str:
        payload = {
            "sub": sub,
            "roles": roles,
            "scopes": scopes,
            "iat": now,
            "exp": now + timedelta(minutes=delta_minutes),
        }
        return jwt.encode(payload, secret, algorithm=algo)

    return {
        "{{ADMIN_TOKEN}}": _mint("admin_user", ["admin"], ["read", "write"], 60),
        "{{VIEWER_TOKEN}}": _mint("viewer_user", ["viewer"], ["read"], 60),
        "{{EXPIRED_TOKEN}}": _mint("expired_user", ["admin"], ["read"], -30),
        "{{SUPERADMIN_TOKEN}}": _mint("super_user", ["superadmin", "admin"], ["*"], 60),
        "{{INVALID_TOKEN}}": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.invalid_signature",
    }


class ParsedCurlRequest:
    def __init__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
        query_params: dict[str, Any] | None = None,
    ) -> None:
        self.method = method.upper()
        self.url = url
        self.headers = headers
        self.body = body
        self.query_params = query_params or {}

    @property
    def path(self) -> str:
        parsed = urlparse(self.url)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        return path


def substitute_placeholders(raw_command: str) -> str:
    tokens = generate_test_tokens()
    for placeholder, val in tokens.items():
        raw_command = raw_command.replace(placeholder, val)

    raw_command = raw_command.replace("{{UUID}}", str(uuid.uuid4()))
    raw_command = raw_command.replace("{{TIMESTAMP}}", str(int(datetime.now(UTC).timestamp())))
    return raw_command


def parse_curl_command(raw_curl: str) -> ParsedCurlRequest:
    """Parses a curl command line string into a structured HTTP request."""
    substituted = substitute_placeholders(raw_curl.strip())
    tokens = shlex.split(substituted)

    if tokens and tokens[0] == "curl":
        tokens = tokens[1:]

    method: str | None = None
    headers: dict[str, str] = {}
    body: bytes | None = None
    url: str | None = None

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token in ("-X", "--request"):
            i += 1
            if i < len(tokens):
                method = tokens[i]
        elif token in ("-H", "--header"):
            i += 1
            if i < len(tokens):
                header_str = tokens[i]
                if ":" in header_str:
                    k, v = header_str.split(":", 1)
                    headers[k.strip()] = v.strip()
        elif token in ("-d", "--data", "--data-binary", "--data-raw", "--data-ascii"):
            i += 1
            if i < len(tokens):
                data_val = tokens[i]
                body = data_val.encode("utf-8")
                if method is None:
                    method = "POST"
                if "Content-Type" not in headers and "content-type" not in headers:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif token == "--json":
            i += 1
            if i < len(tokens):
                body = tokens[i].encode("utf-8")
                if method is None:
                    method = "POST"
                headers["Content-Type"] = "application/json"
        elif token in ("-u", "--user"):
            i += 1
            if i < len(tokens):
                userpass = tokens[i]
                b64 = base64.b64encode(userpass.encode("utf-8")).decode("utf-8")
                headers["Authorization"] = f"Basic {b64}"
        elif token in ("-i", "-s", "-S", "-v", "-k", "--silent", "--insecure"):
            pass  # Ignored flags
        elif token in ("-o", "--output"):
            i += 1  # Skip output path
        elif not token.startswith("-"):
            # Assume it's the target URL
            url = token
        i += 1

    if not url:
        raise ValueError(f"Could not extract target URL from curl command: '{raw_curl}'")

    if not method:
        method = "POST" if body is not None else "GET"

    return ParsedCurlRequest(
        method=method,
        url=url,
        headers=headers,
        body=body,
    )
