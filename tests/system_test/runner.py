from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from http import HTTPMethod
from pathlib import Path
from typing import Any

import httpx
import yaml

from gateway.discovery.load_balancer import LoadBalancer
from gateway.main import create_app
from gateway.middleware.auth import AuthMiddleware
from gateway.middleware.base import MiddlewarePipeline
from gateway.middleware.metrics import MetricsMiddleware
from gateway.middleware.rate_limit import InMemoryRateLimitEngine, RateLimitMiddleware
from gateway.middleware.request_transform import RequestTransformMiddleware
from gateway.middleware.response_transform import ResponseTransformMiddleware
from gateway.models.backend import BackendInstance
from gateway.models.route import (
    AuthMiddlewareConfig,
    MiddlewareConfig,
    RateLimitConfig,
    RouteDefinition,
    TransformConfig,
)
from gateway.proxy.engine import ProxyEngine
from gateway.routing.registry import RouteRegistry
from tests.system_test.backend_transport import SystemBackendTransport
from tests.system_test.curl_parser import parse_curl_command
from tests.system_test.models import ExpectedResponse, SystemTestScenario


def _parse_middleware_config(raw_mw: dict[str, Any] | None) -> MiddlewareConfig:
    if not raw_mw:
        return MiddlewareConfig()

    auth_cfg = AuthMiddlewareConfig()
    if "auth" in raw_mw:
        raw_auth = raw_mw["auth"]
        allowed_roles = set(raw_auth.get("allowed_roles", []))
        required_scopes = set(raw_auth.get("required_scopes", []))
        auth_cfg = AuthMiddlewareConfig(
            enabled=raw_auth.get("enabled", True),
            required=raw_auth.get("required", True),
            allowed_roles=allowed_roles,
            required_scopes=required_scopes,
        )

    rate_cfg = RateLimitConfig()
    if "rate_limit" in raw_mw:
        raw_rl = raw_mw["rate_limit"]
        rate_cfg = RateLimitConfig(
            enabled=raw_rl.get("enabled", True),
            requests_per_minute=raw_rl.get("requests_per_minute", 60),
            burst_capacity=raw_rl.get("burst_capacity", raw_rl.get("burst", 10)),
            key_by=raw_rl.get("key_by", "ip"),
        )

    trans_cfg = TransformConfig()
    if "transform" in raw_mw:
        raw_tr = raw_mw["transform"]
        trans_cfg = TransformConfig(
            strip_prefix=raw_tr.get("strip_prefix"),
            add_prefix=raw_tr.get("add_prefix"),
            add_request_headers=raw_tr.get("add_request_headers", {}),
            remove_request_headers=set(raw_tr.get("remove_request_headers", [])),
            add_response_headers=raw_tr.get("add_response_headers", {}),
        )

    return MiddlewareConfig(auth=auth_cfg, rate_limit=rate_cfg, transform=trans_cfg)


def _build_route_definition(
    raw_route: dict[str, Any], raw_config: dict[str, Any] | None = None
) -> RouteDefinition:
    merged = dict(raw_route)
    if raw_config:
        for k, v in raw_config.items():
            if k == "middleware" and "middleware" in merged:
                mw = dict(merged["middleware"])
                mw.update(v)
                merged["middleware"] = mw
            elif k not in merged or merged[k] is None:
                merged[k] = v

    methods_raw = merged.get("methods", ["GET", "POST", "PUT", "DELETE", "PATCH"])
    methods = {HTTPMethod(m.upper()) for m in methods_raw}

    upstream_url = merged.get("upstream_url") or ""

    raw_pool = merged.get("upstream_pool") or []
    upstream_pool: set[BackendInstance] = set()
    for b in raw_pool:
        if isinstance(b, dict):
            upstream_pool.add(BackendInstance(**b))
        elif isinstance(b, BackendInstance):
            upstream_pool.add(b)

    mw_config = _parse_middleware_config(merged.get("middleware"))

    return RouteDefinition(
        id=merged.get("id", f"route-{merged.get('path_pattern', 'test').replace('/', '-')}"),
        name=merged.get("name", "System Test Route"),
        path_pattern=merged.get("path_pattern", "/**"),
        methods=methods,
        upstream_url=upstream_url,
        upstream_pool=upstream_pool,
        strip_prefix=merged.get("strip_prefix"),
        add_prefix=merged.get("add_prefix"),
        timeout_seconds=float(merged.get("timeout_seconds", 30.0)),
        priority=int(merged.get("priority", 0)),
        enabled=merged.get("enabled", True),
        middleware=mw_config,
        middleware_chain=merged.get("middleware_chain") or merged.get("middlewares") or [],
    )


def _assert_sub_json(expected: Any, actual: Any, path: str = "root") -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise AssertionError(f"Expected dict at {path}, got {type(actual).__name__}: {actual}")
        for k, v in expected.items():
            if k not in actual:
                found_keys = list(actual.keys())
                raise AssertionError(
                    f"Missing expected key '{k}' in JSON at {path}. Keys found: {found_keys}"
                )
            _assert_sub_json(v, actual[k], f"{path}.{k}")
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            raise AssertionError(f"Expected list at {path}, got {type(actual).__name__}: {actual}")
        if len(expected) > len(actual):
            raise AssertionError(
                f"Expected at least {len(expected)} items at {path}, got {len(actual)}"
            )
        for idx, item in enumerate(expected):
            _assert_sub_json(item, actual[idx], f"{path}[{idx}]")
    else:
        if expected != actual:
            raise AssertionError(f"Value mismatch at {path}: expected {expected!r}, got {actual!r}")


def _verify_expected_response(
    expected: ExpectedResponse, resp: httpx.Response, step_info: str
) -> None:
    # 1. Status Code
    if expected.status_code is not None and resp.status_code != expected.status_code:
        raise AssertionError(
            f"[{step_info}] HTTP status code mismatch!\n"
            f"Expected: {expected.status_code}\n"
            f"Actual:   {resp.status_code}\n"
            f"Response body:\n{resp.text}"
        )

    # 2. Headers
    if expected.headers:
        actual_headers = {k.lower(): v for k, v in resp.headers.items()}
        for hk, hv in expected.headers.items():
            k_lower = hk.lower()
            if k_lower not in actual_headers:
                raise AssertionError(
                    f"[{step_info}] Missing expected response header: '{hk}'\n"
                    f"Actual headers: {resp.headers}"
                )
            if hv != "*" and hv.lower() not in actual_headers[k_lower].lower():
                raise AssertionError(
                    f"[{step_info}] Header value mismatch for '{hk}':\n"
                    f"Expected to contain: {hv!r}\n"
                    f"Actual header value:  {actual_headers[k_lower]!r}"
                )

    # 3. Body contains
    if expected.body_contains:
        items = (
            [expected.body_contains]
            if isinstance(expected.body_contains, str)
            else expected.body_contains
        )
        for item in items:
            if item not in resp.text:
                raise AssertionError(
                    f"[{step_info}] Expected response body to contain: {item!r}\n"
                    f"Actual response body:\n{resp.text}"
                )

    # 4. Body not contains
    if expected.body_not_contains:
        items = (
            [expected.body_not_contains]
            if isinstance(expected.body_not_contains, str)
            else expected.body_not_contains
        )
        for item in items:
            if item in resp.text:
                raise AssertionError(
                    f"[{step_info}] Expected response body NOT to contain: {item!r}\n"
                    f"Actual response body:\n{resp.text}"
                )

    # 5. JSON matching
    if expected.json_match is not None:
        try:
            actual_json = resp.json()
        except Exception as exc:
            raise AssertionError(
                f"[{step_info}] Failed to parse response as JSON: {exc}\nBody:\n{resp.text}"
            ) from exc
        _assert_sub_json(expected.json_match, actual_json)

    # 6. Regex
    if expected.regex is not None and not re.search(expected.regex, resp.text):
        raise AssertionError(
            f"[{step_info}] Response body did not match regex: {expected.regex!r}\n"
            f"Body:\n{resp.text}"
        )


class SystemTestRunner:
    """Orchestrates system tests from YAML scenario definitions."""

    def __init__(self, scenario: SystemTestScenario) -> None:
        self.scenario = scenario
        self.backend_transport = SystemBackendTransport()

    def _setup_services(self) -> None:
        if not self.scenario.services:
            return

        if isinstance(self.scenario.services, dict):
            for svc_name, status in self.scenario.services.items():
                self.backend_transport.set_service_state(svc_name, status)
        elif isinstance(self.scenario.services, list):
            for item in self.scenario.services:
                for svc_name, status in item.items():
                    self.backend_transport.set_service_state(svc_name, status)

    def _setup_routes(self) -> list[RouteDefinition]:
        routes: list[RouteDefinition] = []

        if self.scenario.routes:
            for r in self.scenario.routes:
                routes.append(_build_route_definition(r, self.scenario.config))
        elif self.scenario.route:
            routes.append(_build_route_definition(self.scenario.route, self.scenario.config))
        else:
            default_path = Path("config/routes.json")
            if default_path.exists():
                data = json.loads(default_path.read_text(encoding="utf-8"))
                for r in data:
                    routes.append(_build_route_definition(r, self.scenario.config))

        return routes

    async def run(self) -> None:
        self._setup_services()
        routes = self._setup_routes()

        registry = RouteRegistry(routes=routes)
        load_balancer = LoadBalancer()
        proxy_engine = ProxyEngine(
            client=httpx.AsyncClient(transport=self.backend_transport),
            on_result=load_balancer.record_result,
        )

        auth_mw = AuthMiddleware()
        rl_mw = RateLimitMiddleware(memory_engine=InMemoryRateLimitEngine())
        req_tr_mw = RequestTransformMiddleware()
        res_tr_mw = ResponseTransformMiddleware()
        metrics_mw = MetricsMiddleware()

        named_mw = {
            "auth": auth_mw,
            "rate_limit": rl_mw,
            "request_transform": req_tr_mw,
            "response_transform": res_tr_mw,
        }

        pipeline = MiddlewarePipeline(
            middlewares=[
                metrics_mw,
                auth_mw,
                rl_mw,
                req_tr_mw,
                res_tr_mw,
            ],
            named_middlewares=named_mw,
        )

        gateway_app = create_app(
            registry=registry,
            proxy_engine=proxy_engine,
            pipeline=pipeline,
            load_balancer=load_balancer,
        )

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway_app),
            base_url="http://localhost:8000",
        )

        # Execute steps
        for idx, step in enumerate(self.scenario.curl_commands, start=1):
            step_name = step.name or f"Step {idx}"

            # 1. Apply any dynamic service action
            if step.service_action:
                for svc_name, action in step.service_action.items():
                    self.backend_transport.set_service_state(svc_name, action)

            # 2. Execute curl command if provided
            if step.curl:
                parsed = parse_curl_command(step.curl)
                resp = await client.request(
                    method=parsed.method,
                    url=parsed.path,
                    headers=parsed.headers,
                    content=parsed.body,
                )

                if step.expected_response:
                    _verify_expected_response(
                        expected=step.expected_response,
                        resp=resp,
                        step_info=f"{self.scenario.name} -> {step_name}: {step.curl}",
                    )


async def run_yaml_file(yaml_path: str | Path) -> None:
    path = Path(yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"YAML test file not found: {path}")

    raw_content = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw_content)

    if isinstance(loaded, list):
        scenarios = [SystemTestScenario.model_validate(s) for s in loaded]
    elif "scenarios" in loaded:
        scenarios = [SystemTestScenario.model_validate(s) for s in loaded["scenarios"]]
    else:
        scenarios = [SystemTestScenario.model_validate(loaded)]

    for scenario in scenarios:
        runner = SystemTestRunner(scenario)
        await runner.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Mountain API Gateway YAML System Test Runner")
    parser.add_argument("yaml_file", help="Path to YAML test scenario file")
    args = parser.parse_args()

    try:
        asyncio.run(run_yaml_file(args.yaml_file))
        print(f"\033[0;32m[SYSTEM TEST PASSED]\033[0m Successfully verified {args.yaml_file}")
    except Exception as exc:
        print(f"\033[0;31m[SYSTEM TEST FAILED]\033[0m {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
