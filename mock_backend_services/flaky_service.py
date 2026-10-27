import os
import random

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from mock_backend_services.models import FlakyResponse, ServiceHealth

app = FastAPI(
    title="Mock Flaky Service",
    description="Simulates stochastic or deterministic service degradation and failures.",
    version="0.1.0",
)


@app.get("/health", response_model=ServiceHealth, summary="Service health probe")
async def health_check(unhealthy: bool = Query(default=False)) -> ServiceHealth:
    if unhealthy:
        raise HTTPException(status_code=503, detail="Simulated unhealthy state")
    return ServiceHealth(status="healthy", service="flaky-service")


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    response_model=FlakyResponse,
    summary="Potentially failing endpoint",
)
async def flaky_endpoint(
    full_path: str,
    fail_rate: float = Query(default=0.5, ge=0.0, le=1.0),
    fail_code: int = Query(default=500, ge=400, le=599),
) -> JSONResponse:
    if random.random() < fail_rate:
        raise HTTPException(status_code=fail_code, detail="Synthetic upstream failure")
    payload = FlakyResponse(
        status="success",
        path=f"/{full_path.lstrip('/')}",
        fail_rate=fail_rate,
    )
    return JSONResponse(content=payload.model_dump())


if __name__ == "__main__":
    port = int(os.getenv("FLAKY_PORT", "8003"))
    uvicorn.run("mock_backend_services.flaky_service:app", host="0.0.0.0", port=port, reload=True)
