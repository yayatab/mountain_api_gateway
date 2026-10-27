import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Echo Service")


@app.api_route(
    "/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
)
async def echo_endpoint(request: Request, full_path: str) -> JSONResponse:
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

    return JSONResponse(
        content={
            "method": request.method,
            "path": f"/{full_path}",
            "query_params": query_params,
            "headers": headers,
            "body": body_parsed,
            "client_ip": request.client.host if request.client else None,
        }
    )
