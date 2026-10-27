import asyncio
import json
import os
from collections.abc import AsyncGenerator

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mock_backend_services.models import EchoResponse, ServiceHealth

app = FastAPI(
    title="Mock Echo Service",
    description="Reflects request metadata, supports chunked streaming and custom status codes.",
    version="0.1.0",
)


@app.get("/health", response_model=ServiceHealth, summary="Service health probe")
async def health_check() -> ServiceHealth:
    return ServiceHealth(status="healthy", service="echo-service")


@app.get("/stream", summary="Stream simulated chunked responses")
async def stream_endpoint(
    chunks: int = Query(default=5, ge=1, le=100),
    delay: float = Query(default=0.05, ge=0.0, le=5.0),
) -> StreamingResponse:
    async def chunk_generator() -> AsyncGenerator[bytes, None]:
        for i in range(1, chunks + 1):
            chunk_data = json.dumps({"chunk": i, "total": chunks}) + "\n"
            yield chunk_data.encode("utf-8")
            if delay > 0 and i < chunks:
                await asyncio.sleep(delay)

    return StreamingResponse(chunk_generator(), media_type="application/x-ndjson")


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    summary="Echo request metadata back to caller",
)
async def echo_endpoint(
    request: Request,
    full_path: str,
    status_code: int = Query(default=200, ge=100, le=599),
) -> JSONResponse:
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8", errors="replace")

    body_parsed = None
    if body_str:
        try:
            body_parsed = json.loads(body_str)
        except Exception:
            body_parsed = body_str

    headers = dict(request.headers)
    query_params = dict(request.query_params)

    echo_payload = EchoResponse(
        method=request.method,
        path=f"/{full_path.lstrip('/')}",
        query_params=query_params,
        headers=headers,
        body=body_parsed,
        client_ip=request.client.host if request.client else None,
    )

    return JSONResponse(
        status_code=status_code,
        content=echo_payload.model_dump(),
    )


if __name__ == "__main__":
    port = int(os.getenv("ECHO_PORT", "8001"))
    uvicorn.run("mock_backend_services.echo_service:app", host="0.0.0.0", port=port, reload=True)
