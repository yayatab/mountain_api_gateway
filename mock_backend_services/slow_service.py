import asyncio
import os

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from mock_backend_services.models import ServiceHealth, SlowResponse

app = FastAPI(
    title="Mock Slow Service",
    description="Simulates delayed responses for testing timeouts and latency.",
    version="0.1.0",
)


@app.get("/health", response_model=ServiceHealth, summary="Service health probe")
async def health_check() -> ServiceHealth:
    return ServiceHealth(status="healthy", service="slow-service")


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    response_model=SlowResponse,
    summary="Delayed endpoint response",
)
async def slow_endpoint(
    full_path: str,
    delay: float = Query(default=1.0, ge=0.0, le=60.0),
) -> JSONResponse:
    if delay > 0:
        await asyncio.sleep(delay)
    payload = SlowResponse(
        status="completed",
        delayed_seconds=delay,
        path=f"/{full_path.lstrip('/')}",
    )
    return JSONResponse(content=payload.model_dump())


if __name__ == "__main__":
    port = int(os.getenv("SLOW_PORT", "8002"))
    uvicorn.run("mock_backend_services.slow_service:app", host="0.0.0.0", port=port, reload=True)
