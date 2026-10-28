import gzip

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse

from gateway.main import create_app
from gateway.models.route import (
    CompressionConfig,
    HTTPMethod,
    MiddlewareConfig,
    RouteDefinition,
)
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry


@pytest.fixture
def compression_backend_app() -> FastAPI:
    app = FastAPI()

    # Large text payload (> 2 KB)
    large_json = {"message": "hello " * 400, "status": "success"}

    @app.get("/large-json")
    async def get_large_json():
        import json

        content = json.dumps(large_json).encode("utf-8")
        return Response(content=content, media_type="application/json")

    @app.get("/small-json")
    async def get_small_json():
        return {"msg": "tiny"}

    @app.get("/image")
    async def get_image():
        return Response(content=b"\x89PNG\r\n\x1a\n" * 100, media_type="image/png")

    @app.get("/stream-json")
    async def stream_json():
        async def chunk_gen():
            for i in range(10):
                yield f'{{"chunk": {i}, "data": "{"x" * 200}"}}\n'.encode()

        return StreamingResponse(chunk_gen(), media_type="application/json")

    return app


@pytest.fixture
def compression_client(compression_backend_app: FastAPI) -> httpx.AsyncClient:
    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=compression_backend_app),
        base_url="http://compress-backend",
    )

    route = RouteDefinition(
        id="compress-route",
        path_pattern="/compress-api/**",
        methods={HTTPMethod.GET},
        upstream_url="http://compress-backend",
        strip_prefix="/compress-api",
        middleware=MiddlewareConfig(
            compression=CompressionConfig(
                enabled=True,
                min_size_bytes=500,  # 500 bytes threshold
                algorithms=["gzip", "deflate"],
            )
        ),
    )

    registry = RouteRegistry(routes=[route])
    engine = ProxyEngine(client=backend_client)
    gateway_app = create_app(registry=registry, proxy_engine=engine)

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway.test",
    )


@pytest.mark.asyncio
async def test_gzip_compression_on_large_payload(compression_client: httpx.AsyncClient) -> None:
    res = await compression_client.get(
        "/compress-api/large-json",
        headers={"Accept-Encoding": "gzip"},
    )
    assert res.status_code == 200
    assert res.headers.get("Content-Encoding") == "gzip"
    assert "Accept-Encoding" in res.headers.get("Vary", "")

    # HTTPX automatically decodes the gzip response
    data = res.json()
    assert data["status"] == "success"
    assert "hello hello" in data["message"]


@pytest.mark.asyncio
async def test_deflate_compression(compression_client: httpx.AsyncClient) -> None:
    res = await compression_client.get(
        "/compress-api/large-json",
        headers={"Accept-Encoding": "deflate"},
    )
    assert res.status_code == 200
    assert res.headers.get("Content-Encoding") == "deflate"

    # HTTPX automatically decodes the deflate response
    data = res.json()
    assert data["status"] == "success"
    assert "hello hello" in data["message"]


@pytest.mark.asyncio
async def test_small_payload_remains_uncompressed(compression_client: httpx.AsyncClient) -> None:
    res = await compression_client.get(
        "/compress-api/small-json",
        headers={"Accept-Encoding": "gzip"},
    )
    assert res.status_code == 200
    # Payload is under 500 bytes min_size_bytes -> uncompressed
    assert "Content-Encoding" not in res.headers
    assert res.json() == {"msg": "tiny"}


@pytest.mark.asyncio
async def test_ineligible_mime_type_remains_uncompressed(
    compression_client: httpx.AsyncClient,
) -> None:
    res = await compression_client.get(
        "/compress-api/image",
        headers={"Accept-Encoding": "gzip"},
    )
    assert res.status_code == 200
    assert "Content-Encoding" not in res.headers


@pytest.mark.asyncio
async def test_client_without_accept_encoding_remains_uncompressed(
    compression_client: httpx.AsyncClient,
) -> None:
    res = await compression_client.get(
        "/compress-api/large-json",
        headers={"Accept-Encoding": ""},
    )
    assert res.status_code == 200
    assert "Content-Encoding" not in res.headers
    assert b"hello hello" in res.content


@pytest.mark.asyncio
async def test_streaming_response_compression(compression_client: httpx.AsyncClient) -> None:
    res = await compression_client.get(
        "/compress-api/stream-json",
        headers={"Accept-Encoding": "gzip"},
    )
    assert res.status_code == 200
    assert res.headers.get("Content-Encoding") == "gzip"

    # Streaming chunks compressed with gzip and decompressed transparently by client
    assert b"chunk" in res.content
    assert b"xxxx" in res.content


@pytest.mark.asyncio
async def test_direct_raw_compression_bytes() -> None:
    from fastapi.responses import Response as StarletteResponse
    from starlette.requests import Request

    from gateway.middleware.compression import CompressionMiddleware
    from gateway.models.context import GatewayContext
    from gateway.models.route import CompressionConfig, RouteDefinition

    mw = CompressionMiddleware()
    route = RouteDefinition(
        id="test-route",
        path_pattern="/test",
        upstream_url="http://backend",
        middleware=MiddlewareConfig(
            compression=CompressionConfig(
                enabled=True,
                min_size_bytes=10,
                algorithms=["gzip"],
            )
        ),
    )
    ctx = GatewayContext(request_id="test-req", matched_route=route)

    raw_body = b"Mountain API Gateway Direct Compression Verification Test Body " * 20

    async def mock_call_next(req, context):
        return StarletteResponse(
            content=raw_body,
            media_type="application/json",
            headers={"Content-Length": str(len(raw_body))},
        )

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [(b"accept-encoding", b"gzip")],
    }
    request = Request(scope)

    compressed_resp = await mw(request, ctx, mock_call_next)
    assert compressed_resp.headers.get("Content-Encoding") == "gzip"
    assert compressed_resp.body != raw_body
    # Decompress raw compressed body bytes directly
    decompressed = gzip.decompress(compressed_resp.body)
    assert decompressed == raw_body
