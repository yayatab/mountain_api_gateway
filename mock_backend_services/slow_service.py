import asyncio

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Slow Service")


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def slow_endpoint(full_path: str, delay: float = Query(default=1.0)) -> JSONResponse:
    await asyncio.sleep(delay)
    return JSONResponse(
        content={
            "status": "completed",
            "delayed_seconds": delay,
            "path": f"/{full_path}",
        }
    )
