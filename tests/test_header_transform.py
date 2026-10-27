from gateway.proxy.headers import (
    HOP_BY_HOP_HEADERS,
    parse_connection_tokens,
    prepare_client_response_headers,
    prepare_upstream_headers,
)


def test_parse_connection_tokens():
    tokens = parse_connection_tokens("close, X-Custom-Hop, Keep-Alive, X-Feature-Flag")
    assert tokens == {"close", "x-custom-hop", "keep-alive", "x-feature-flag"}
    assert parse_connection_tokens(None) == set()
    assert parse_connection_tokens("") == set()


def test_prepare_upstream_headers_strips_standard_hop_by_hop():
    incoming = {
        "Host": "client.example.com",
        "Connection": "keep-alive",
        "Keep-Alive": "timeout=5, max=100",
        "Upgrade": "websocket",
        "Transfer-Encoding": "chunked",
        "TE": "trailers",
        "User-Agent": "TestClient/1.0",
        "Accept": "application/json",
    }
    result = prepare_upstream_headers(
        incoming_headers=incoming,
        client_host="client.example.com",
        client_ip="192.168.1.10",
        target_url="http://backend-service:8080/api",
        request_id="req-123",
    )

    for h in HOP_BY_HOP_HEADERS:
        assert h not in result
        assert h.title() not in result

    assert result["Host"] == "backend-service:8080"
    assert result["User-Agent"] == "TestClient/1.0"
    assert result["Accept"] == "application/json"
    assert result["X-Request-ID"] == "req-123"
    assert result["X-Forwarded-For"] == "192.168.1.10"
    assert result["X-Real-IP"] == "192.168.1.10"
    assert result["X-Forwarded-Host"] == "client.example.com"
    assert result["X-Forwarded-Port"] == "8080"


def test_prepare_upstream_headers_dynamic_connection_tokens():
    incoming = {
        "Connection": "close, X-Hop-Header, X-Sensitive-Routing",
        "X-Hop-Header": "must-be-stripped",
        "X-Sensitive-Routing": "also-stripped",
        "X-Keep-Me": "preserved",
    }
    result = prepare_upstream_headers(
        incoming_headers=incoming,
        client_host="client.example.com",
        client_ip="10.0.0.1",
        target_url="http://internal:9000",
        request_id="req-456",
    )
    assert "Connection" not in result
    assert "X-Hop-Header" not in result
    assert "x-hop-header" not in result
    assert "X-Sensitive-Routing" not in result
    assert "x-sensitive-routing" not in result
    assert result["X-Keep-Me"] == "preserved"


def test_prepare_upstream_headers_forwarded_chain_appends():
    incoming = {
        "X-Forwarded-For": "203.0.113.195, 70.41.3.18",
        "X-Forwarded-Host": "original.domain.com",
    }
    result = prepare_upstream_headers(
        incoming_headers=incoming,
        client_host="edge-proxy.com",
        client_ip="10.0.0.5",
        target_url="http://backend:80",
        request_id="req-789",
    )
    assert result["X-Forwarded-For"] == "203.0.113.195, 70.41.3.18, 10.0.0.5"
    assert result["X-Forwarded-Host"] == "original.domain.com"
    assert result["X-Real-IP"] == "10.0.0.5"


def test_prepare_client_response_headers_stripping():
    upstream_resp_headers = {
        "Content-Type": "application/json",
        "Content-Length": "1234",
        "Connection": "close, X-Backend-Private",
        "X-Backend-Private": "secret",
        "Transfer-Encoding": "chunked",
        "Set-Cookie": "session=abc",
    }
    cleaned = prepare_client_response_headers(upstream_resp_headers, is_streaming=True)
    assert "Content-Type" in cleaned
    assert "Set-Cookie" in cleaned
    assert "Connection" not in cleaned
    assert "Transfer-Encoding" not in cleaned
    assert "X-Backend-Private" not in cleaned
    # Content-Length should be removed when streaming
    assert "Content-Length" not in cleaned
    assert "content-length" not in cleaned
