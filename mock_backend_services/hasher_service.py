import asyncio
import hashlib
import os
import time
from collections.abc import AsyncGenerator

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mock_backend_services.models import FileHashResponse, ServiceHealth

app = FastAPI(
    title="Mock File Hasher Service",
    description="Streams file uploads chunk-by-chunk and calculates MD5, SHA1, and SHA256 hashes.",
    version="0.1.0",
)


@app.get("/health", response_model=ServiceHealth, summary="Service health probe")
async def health_check() -> ServiceHealth:
    return ServiceHealth(status="healthy", service="hasher-service")


@app.post(
    "/hash",
    response_model=FileHashResponse,
    summary="Stream upload file and compute MD5, SHA1, and SHA256 hashes",
)
@app.post(
    "/upload",
    response_model=FileHashResponse,
    summary="Alias for /hash",
)
async def hash_file_stream(request: Request) -> JSONResponse:
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()

    total_bytes = 0
    chunk_count = 0
    start_time = time.perf_counter()

    filename = request.headers.get("x-file-name") or request.query_params.get("filename")

    async for chunk in request.stream():
        if not chunk:
            continue
        total_bytes += len(chunk)
        chunk_count += 1
        md5.update(chunk)
        sha1.update(chunk)
        sha256.update(chunk)

    elapsed = time.perf_counter() - start_time

    response_data = FileHashResponse(
        filename=filename,
        total_bytes=total_bytes,
        chunk_count=chunk_count,
        md5=md5.hexdigest(),
        sha1=sha1.hexdigest(),
        sha256=sha256.hexdigest(),
        elapsed_seconds=round(elapsed, 4),
    )
    return JSONResponse(content=response_data.model_dump())


@app.get(
    "/generate",
    summary="Generate and stream large test binary files",
)
async def generate_file_stream(
    size_bytes: int = Query(default=1048576, ge=1, le=104857600),
    chunk_size: int = Query(default=65536, ge=512, le=1048576),
) -> StreamingResponse:
    async def file_chunk_generator() -> AsyncGenerator[bytes, None]:
        remaining = size_bytes
        # 64KB deterministic repeating pattern
        base_pattern = b"0123456789abcdef" * 4096

        while remaining > 0:
            current_chunk_size = min(remaining, chunk_size)
            chunk = (
                base_pattern[:current_chunk_size]
                if current_chunk_size <= len(base_pattern)
                else (base_pattern * ((current_chunk_size // len(base_pattern)) + 1))[
                    :current_chunk_size
                ]
            )
            yield chunk
            remaining -= current_chunk_size
            await asyncio.sleep(0)  # Yield control to event loop

    headers = {
        "Content-Disposition": 'attachment; filename="synthetic_test_file.bin"',
        "Content-Length": str(size_bytes),
    }
    return StreamingResponse(
        file_chunk_generator(),
        media_type="application/octet-stream",
        headers=headers,
    )


if __name__ == "__main__":
    port = int(os.getenv("HASHER_PORT", "8004"))
    uvicorn.run("mock_backend_services.hasher_service:app", host="0.0.0.0", port=port, reload=True)
