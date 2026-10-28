import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mock_backend_services.models import OrderProcessResult, PipelineRecord, ServiceHealth

logger = logging.getLogger("mock_backend.pipeline")

app = FastAPI(
    title="Mock Data Pipeline Service",
    description=(
        "Simulates streaming NDJSON event transformation and stateful idempotent order processing."
    ),
    version="0.1.0",
)

# In-memory idempotency cache for order processing
_idempotency_cache: dict[str, OrderProcessResult] = {}


@app.get("/health", response_model=ServiceHealth, summary="Service health probe")
async def health_check() -> ServiceHealth:
    return ServiceHealth(status="healthy", service="data-pipeline-service")


@app.post(
    "/pipeline/transform",
    summary="Stream and transform NDJSON event records on the fly",
)
async def transform_event_stream(request: Request) -> StreamingResponse:
    request_id = request.headers.get("x-request-id")
    user_id = request.headers.get("x-user-id")
    body_bytes = await request.body()

    async def stream_transformer() -> AsyncGenerator[bytes, None]:
        for line in body_bytes.splitlines():
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue
            try:
                record_dict = json.loads(line_str)
                record = PipelineRecord(
                    id=record_dict.get("id", str(uuid.uuid4())),
                    event=record_dict.get("event", "unknown"),
                    data=record_dict.get("data", {}),
                    gateway_request_id=request_id,
                    processed_by_user=user_id,
                )
                out_line = json.dumps(record.model_dump()) + "\n"
                yield out_line.encode("utf-8")
                await asyncio.sleep(0)
            except Exception as exc:
                logger.warning("Skipping malformed NDJSON line: %s", exc)
                continue

    return StreamingResponse(stream_transformer(), media_type="application/x-ndjson")


@app.post(
    "/pipeline/orders",
    response_model=OrderProcessResult,
    summary="Idempotent transactional order creation endpoint",
)
async def process_order(request: Request) -> JSONResponse:
    idempotency_key = request.headers.get("idempotency-key")
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="Header 'Idempotency-Key' is required for order processing",
        )

    # Check if duplicate request
    if idempotency_key in _idempotency_cache:
        cached = _idempotency_cache[idempotency_key].model_copy()
        cached.is_duplicate = True
        return JSONResponse(status_code=200, content=cached.model_dump(mode="json"))

    body = await request.json() if await request.body() else {}
    user_id = request.headers.get("x-user-id")
    items = body.get("items", [])
    raw_tags = body.get("tags", [])

    order_result = OrderProcessResult(
        order_id=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        user_id=user_id,
        status="processed",
        total_items=len(items) if isinstance(items, list) else 0,
        tags=set(raw_tags) if isinstance(raw_tags, list) else set(),
        is_duplicate=False,
    )

    _idempotency_cache[idempotency_key] = order_result
    return JSONResponse(status_code=201, content=order_result.model_dump(mode="json"))


@app.post("/pipeline/reset", summary="Reset pipeline state for testing")
async def reset_pipeline_state() -> dict[str, str]:
    _idempotency_cache.clear()
    return {"status": "cleared"}


if __name__ == "__main__":
    port = int(os.getenv("PIPELINE_PORT", "8005"))
    uvicorn.run(
        "mock_backend_services.data_pipeline_service:app",
        host="0.0.0.0",
        port=port,
        reload=True,
    )
