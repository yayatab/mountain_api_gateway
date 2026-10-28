import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from gateway.config import settings
from gateway.discovery.load_balancer import LoadBalancer
from gateway.models.admin import (
    DeleteRouteResponse,
    GatewayHealth,
    GatewayStats,
    ReloadResult,
    RouteStatusUpdate,
)
from gateway.models.backend import BackendStatus, HealthStatus
from gateway.models.route import RouteDefinition
from gateway.routing.registry import RouteRegistry

logger = logging.getLogger("gateway.admin")

router = APIRouter(prefix="/admin", tags=["Admin"])


def _get_registry(request: Request) -> RouteRegistry:
    return request.app.state.registry


def _get_load_balancer(request: Request) -> LoadBalancer:
    return request.app.state.load_balancer


@router.post(
    "/routes",
    status_code=201,
    response_model=RouteDefinition,
    summary="Create a new dynamic route",
)
async def create_route(route: RouteDefinition, request: Request) -> RouteDefinition:
    registry = _get_registry(request)
    if await registry.exists(route.id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Route with ID '{route.id}' already exists. "
                f"Use PUT /admin/routes/{route.id} to update."
            ),
        )
    await registry.register(route)
    return route


@router.get(
    "/routes",
    response_model=list[RouteDefinition],
    summary="List all configured routes",
)
async def list_routes(
    request: Request,
    include_disabled: bool = Query(True, description="Whether to include disabled routes"),
) -> list[RouteDefinition]:
    registry = _get_registry(request)
    return await registry.list_all(include_disabled=include_disabled)


@router.get(
    "/routes/{route_id}",
    response_model=RouteDefinition,
    summary="Get route details by ID",
)
async def get_route(route_id: str, request: Request) -> RouteDefinition:
    registry = _get_registry(request)
    route = await registry.get(route_id)
    if not route:
        raise HTTPException(status_code=404, detail=f"Route '{route_id}' not found")
    return route


@router.put(
    "/routes/{route_id}",
    response_model=RouteDefinition,
    summary="Replace or update an existing route",
)
async def update_route(
    route_id: str,
    route: RouteDefinition,
    request: Request,
) -> RouteDefinition:
    registry = _get_registry(request)
    if not await registry.exists(route_id):
        raise HTTPException(status_code=404, detail=f"Route '{route_id}' not found")
    route.id = route_id
    await registry.update(route_id, route)
    return route


@router.delete(
    "/routes/{route_id}",
    response_model=DeleteRouteResponse,
    summary="Delete a route by ID",
)
async def delete_route(route_id: str, request: Request) -> DeleteRouteResponse:
    registry = _get_registry(request)
    deleted = await registry.delete(route_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Route '{route_id}' not found")
    return DeleteRouteResponse(
        message=f"Route '{route_id}' successfully deleted",
        deleted=True,
    )


@router.patch(
    "/routes/{route_id}/status",
    response_model=RouteDefinition,
    summary="Enable or disable a route",
)
async def toggle_route_status(
    route_id: str,
    payload: RouteStatusUpdate,
    request: Request,
) -> RouteDefinition:
    registry = _get_registry(request)
    updated = await registry.set_status(route_id, payload.enabled)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Route '{route_id}' not found")
    return updated


@router.get(
    "/health",
    response_model=GatewayHealth,
    summary="Admin Gateway Health & Uptime",
)
async def admin_health(request: Request) -> GatewayHealth:
    start_time: float = getattr(request.app.state, "start_time", time.perf_counter())
    uptime = time.perf_counter() - start_time
    return GatewayHealth(uptime_seconds=round(uptime, 2))


@router.get(
    "/stats",
    response_model=GatewayStats,
    summary="Gateway operational statistics",
)
async def admin_stats(request: Request) -> GatewayStats:
    registry = _get_registry(request)
    load_balancer = _get_load_balancer(request)

    all_routes = await registry.list_all(include_disabled=True)
    active_routes = sum(1 for r in all_routes if r.enabled)
    disabled_routes = len(all_routes) - active_routes

    backends = await load_balancer.get_backends_status(registry)
    healthy_backends = sum(1 for b in backends if b.status == HealthStatus.HEALTHY)
    unhealthy_backends = len(backends) - healthy_backends

    return GatewayStats(
        total_routes=len(all_routes),
        active_routes=active_routes,
        disabled_routes=disabled_routes,
        total_backends=len(backends),
        healthy_backends=healthy_backends,
        unhealthy_backends=unhealthy_backends,
    )


@router.get(
    "/backends",
    response_model=list[BackendStatus],
    summary="Backend inventory and health",
)
async def admin_backends(request: Request) -> list[BackendStatus]:
    registry = _get_registry(request)
    load_balancer = _get_load_balancer(request)
    return await load_balancer.get_backends_status(registry)


@router.post(
    "/routes/reload",
    response_model=ReloadResult,
    summary="Hot-reload routes from storage without restarting process",
)
async def reload_routes(request: Request) -> ReloadResult:
    registry = _get_registry(request)
    if registry.storage:
        if registry.storage.backend_type == "mongodb":
            try:
                routes = await registry.load_from_storage()
                db = getattr(registry.storage, "database_name", "")
                col = getattr(registry.storage, "collection_name", "")
                return ReloadResult(
                    success=True,
                    reloaded_count=len(routes),
                    source=f"mongodb:{db}.{col}",
                )
            except Exception as e:
                logger.error(
                    "Unexpected error reloading routes from MongoDB storage: %s",
                    e,
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to reload routes from MongoDB storage: {e}",
                ) from e
        file_path = getattr(registry.storage, "file_path", None)
        if not file_path or not file_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"No valid routes file configured or file '{file_path}' does not exist",
            )
        routes = await registry.load_from_storage()
        return ReloadResult(
            success=True,
            reloaded_count=len(routes),
            source=str(file_path),
        )

    path = registry.persistence_path or (
        Path(settings.routes_file_path) if settings.routes_file_path else None
    )

    if not path or not path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"No valid routes file configured or file '{path}' does not exist",
        )

    await registry.load_from_file(path)
    all_routes = await registry.list_all(include_disabled=True)
    return ReloadResult(
        success=True,
        reloaded_count=len(all_routes),
        source=str(path),
    )
