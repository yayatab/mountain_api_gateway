import re
from collections.abc import Mapping
from urllib.parse import urlparse

HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def get_header_ci(headers: Mapping[str, str], target_key: str) -> str | None:
    """Retrieve header value case-insensitively."""
    target_lower = target_key.lower()
    for k, v in headers.items():
        if k.lower() == target_lower:
            return v
    return None


def parse_connection_tokens(connection_header: str | None) -> set[str]:
    """Extract hop-by-hop header names declared inside the Connection header."""
    if not connection_header:
        return set()
    tokens = re.split(r",\s*", connection_header)
    return {token.strip().lower() for token in tokens if token.strip()}


def prepare_upstream_headers(
    incoming_headers: Mapping[str, str],
    client_host: str | None,
    client_ip: str,
    target_url: str,
    request_id: str,
    scheme: str = "http",
    user_id: str | None = None,
    user_roles: list[str] | None = None,
) -> dict[str, str]:
    """Clean hop-by-hop headers and inject proxy tracing headers for upstream request."""
    parsed_target = urlparse(target_url)
    target_netloc = parsed_target.netloc or parsed_target.path

    connection_val = get_header_ci(incoming_headers, "connection")
    dynamic_hop_headers = parse_connection_tokens(connection_val)
    all_hop_headers = HOP_BY_HOP_HEADERS | dynamic_hop_headers

    upstream_headers: dict[str, str] = {}
    for key, value in incoming_headers.items():
        key_lower = key.lower()
        if key_lower in all_hop_headers:
            continue
        # Avoid passing incoming host header directly
        if key_lower == "host":
            continue
        upstream_headers[key] = value

    # Rewrite Host header to target backend authority
    upstream_headers["Host"] = target_netloc

    # Track original client host
    if "x-forwarded-host" not in {k.lower() for k in upstream_headers}:
        if client_host:
            upstream_headers["X-Forwarded-Host"] = client_host
        else:
            incoming_host = get_header_ci(incoming_headers, "host")
            upstream_headers["X-Forwarded-Host"] = incoming_host or target_netloc

    # Manage X-Forwarded-For chain
    existing_xff = get_header_ci(upstream_headers, "x-forwarded-for")
    if existing_xff:
        upstream_headers["X-Forwarded-For"] = f"{existing_xff}, {client_ip}"
    else:
        upstream_headers["X-Forwarded-For"] = client_ip

    upstream_headers["X-Real-IP"] = client_ip
    upstream_headers["X-Forwarded-Proto"] = scheme
    if parsed_target.port:
        upstream_headers["X-Forwarded-Port"] = str(parsed_target.port)

    # Invert / Inject X-Request-ID
    upstream_headers["X-Request-ID"] = request_id

    # Inject authenticated user identity if present
    if user_id:
        upstream_headers["X-User-Id"] = user_id
    if user_roles:
        upstream_headers["X-User-Roles"] = ",".join(user_roles)

    return upstream_headers


def prepare_client_response_headers(
    upstream_headers: Mapping[str, str],
    is_streaming: bool = True,
) -> dict[str, str]:
    """Clean hop-by-hop headers from upstream response before sending to client."""
    connection_val = get_header_ci(upstream_headers, "connection")
    dynamic_hop_headers = parse_connection_tokens(connection_val)
    all_hop_headers = HOP_BY_HOP_HEADERS | dynamic_hop_headers

    client_headers: dict[str, str] = {}
    for key, value in upstream_headers.items():
        key_lower = key.lower()
        if key_lower in all_hop_headers:
            continue
        if is_streaming and key_lower == "content-length":
            # Strip content-length for streaming responses to prevent framing mismatch
            continue
        client_headers[key] = value

    return client_headers
