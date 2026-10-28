import asyncio
from typing import Any, cast

import httpx
import pytest
from starlette.datastructures import Headers

from gateway.main import create_app
from gateway.middleware.cache import CacheMiddleware
from gateway.models.route import CacheConfig, HTTPMethod, MiddlewareConfig, RouteDefinition
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry
from mock_backend_services.echo_service import app as echo_app


@pytest.fixture
def mock_echo_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=echo_app),
        base_url="http://echo-backend:8001",
    )


def test_cache_key_deterministic_sorting() -> None:
    config = CacheConfig(
        vary_headers=["Accept", "Accept-Encoding"],
        key_prefix="test:cache",
    )

    headers1 = Headers({"accept": "application/json", "accept-encoding": "gzip"})
    headers2 = Headers({"Accept": "application/json", "Accept-Encoding": "gzip"})

    # Different query param order
    key1 = CacheMiddleware.generate_cache_key(
        route_id="route-1",
        method="GET",
        path="/api/items",
        query_items=[("sort", "desc"), ("page", "2"), ("filter", "active")],
        headers=headers1,
        config=config,
    )

    key2 = CacheMiddleware.generate_cache_key(
        route_id="route-1",
        method="get",
        path="/api/items/",
        query_items=[("filter", "active"), ("page", "2"), ("sort", "desc")],
        headers=headers2,
        config=config,
    )

    assert key1 == key2

    # Vary header value difference produces different key
    headers3 = Headers({"accept": "text/html", "accept-encoding": "gzip"})
    key3 = CacheMiddleware.generate_cache_key(
        route_id="route-1",
        method="GET",
        path="/api/items",
        query_items=[("sort", "desc"), ("page", "2"), ("filter", "active")],
        headers=headers3,
        config=config,
    )
    assert key1 != key3


@pytest.mark.asyncio
async def test_cache_miss_then_hit(mock_echo_client: httpx.AsyncClient) -> None:
    call_count = 0

    from fastapi import FastAPI

    backend_app = FastAPI()

    @backend_app.get("/data")
    async def get_data():
        nonlocal call_count
        call_count += 1
        return {"call": call_count, "message": "hello world"}

    custom_backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app),
        base_url="http://custom-backend",
    )

    route = RouteDefinition(
        id="cache-route",
        path_pattern="/cached-api/**",
        methods={HTTPMethod.GET},
        upstream_url="http://custom-backend",
        strip_prefix="/cached-api",
        middleware=MiddlewareConfig(
            cache=CacheConfig(
                enabled=True,
                ttl_seconds=10,
            )
        ),
    )

    registry = RouteRegistry(routes=[route])
    engine = ProxyEngine(client=custom_backend_client)
    app = create_app(registry=registry, proxy_engine=engine)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        # Request 1: MISS
        res1 = await client.get("/cached-api/data?q=1")
        assert res1.status_code == 200
        assert res1.headers.get("X-Cache") == "MISS"
        assert res1.json()["call"] == 1
        assert call_count == 1

        # Request 2: HIT
        res2 = await client.get("/cached-api/data?q=1")
        assert res2.status_code == 200
        assert res2.headers.get("X-Cache") == "HIT"
        assert "Age" in res2.headers
        assert res2.json()["call"] == 1
        # Upstream backend was NOT called a second time
        assert call_count == 1


@pytest.mark.asyncio
async def test_cache_bypass_on_post_and_no_cache_header(
    mock_echo_client: httpx.AsyncClient,
) -> None:
    route = RouteDefinition(
        id="cache-methods-route",
        path_pattern="/service/**",
        methods={HTTPMethod.GET, HTTPMethod.POST},
        upstream_url="http://echo-backend:8001",
        strip_prefix="/service",
        middleware=MiddlewareConfig(
            cache=CacheConfig(
                enabled=True,
                methods={"GET"},
            )
        ),
    )

    registry = RouteRegistry(routes=[route])
    engine = ProxyEngine(client=mock_echo_client)
    app = create_app(registry=registry, proxy_engine=engine)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        # POST method is bypassed
        res_post = await client.post("/service/item", json={"name": "test"})
        assert res_post.status_code == 200
        assert res_post.headers.get("X-Cache") == "BYPASS"

        # GET with Cache-Control: no-cache is bypassed
        res_bypass = await client.get(
            "/service/item",
            headers={"Cache-Control": "no-cache"},
        )
        assert res_bypass.status_code == 200
        assert res_bypass.headers.get("X-Cache") == "BYPASS"


@pytest.mark.asyncio
async def test_cache_ttl_expiration() -> None:
    call_count = 0
    from fastapi import FastAPI

    backend_app = FastAPI()

    @backend_app.get("/now")
    async def get_time():
        nonlocal call_count
        call_count += 1
        return {"count": call_count}

    custom_backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app),
        base_url="http://time-backend",
    )

    route = RouteDefinition(
        id="short-ttl-route",
        path_pattern="/time/**",
        methods={HTTPMethod.GET},
        upstream_url="http://time-backend",
        strip_prefix="/time",
        middleware=MiddlewareConfig(
            cache=CacheConfig(
                enabled=True,
                ttl_seconds=1,  # 1 second TTL
            )
        ),
    )

    registry = RouteRegistry(routes=[route])
    engine = ProxyEngine(client=custom_backend_client)
    app = create_app(registry=registry, proxy_engine=engine)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        res1 = await client.get("/time/now")
        assert res1.headers.get("X-Cache") == "MISS"
        assert res1.json()["count"] == 1

        # Immediate repeat -> HIT
        res2 = await client.get("/time/now")
        assert res2.headers.get("X-Cache") == "HIT"
        assert res2.json()["count"] == 1

        # Wait for TTL to expire
        await asyncio.sleep(1.1)

        # After TTL -> MISS again
        res3 = await client.get("/time/now")
        assert res3.headers.get("X-Cache") == "MISS"
        assert res3.json()["count"] == 2


@pytest.mark.asyncio
async def test_cache_fail_open_on_redis_error(mock_echo_client: httpx.AsyncClient) -> None:
    # Middleware with a broken mock redis client
    class BrokenRedis:
        async def get(self, key: str):
            raise ConnectionError("Redis connection refused")

        async def set(self, key: str, val: str, ex: int):
            raise ConnectionError("Redis connection refused")

        async def aclose(self):
            pass

    cache_mw = CacheMiddleware(redis_client=cast(Any, BrokenRedis()))

    route = RouteDefinition(
        id="broken-redis-route",
        path_pattern="/resilient/**",
        methods={HTTPMethod.GET},
        upstream_url="http://echo-backend:8001",
        strip_prefix="/resilient",
        middleware=MiddlewareConfig(
            cache=CacheConfig(
                enabled=True,
                ttl_seconds=60,
            )
        ),
    )

    from gateway.middleware.base import MiddlewarePipeline

    pipeline = MiddlewarePipeline(
        middlewares=[cache_mw],
        named_middlewares={"cache": cache_mw},
    )

    registry = RouteRegistry(routes=[route])
    engine = ProxyEngine(client=mock_echo_client)
    app = create_app(registry=registry, proxy_engine=engine, pipeline=pipeline)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        # Request should succeed and not crash despite Redis failure
        res = await client.get("/resilient/ping")
        assert res.status_code == 200
        assert res.json()["path"] == "/ping"
