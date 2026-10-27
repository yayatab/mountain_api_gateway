import random

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Flaky Service")


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def flaky_endpoint(full_path: str, fail_rate: float = Query(default=0.5)) -> JSONResponse:
    if random.random() < fail_rate:
        raise HTTPException(status_code=500, detail="Synthetic upstream failure")
    return JSONResponse(
        content={
            "status": "success",
            "path": f"/{full_path}",
        }
    )
