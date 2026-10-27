import re
from http import HTTPMethod

from pydantic import BaseModel, ConfigDict, Field

from gateway.models.route import MatchResult, RouteDefinition


class CompiledRoute(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    route: RouteDefinition
    regex: re.Pattern[str]
    specificity_score: int
    param_names: list[str] = Field(default_factory=list)


class RouteMatcher:
    def __init__(self, routes: list[RouteDefinition] | None = None) -> None:
        self._compiled_routes: list[CompiledRoute] = []
        if routes:
            self.set_routes(routes)

    def set_routes(self, routes: list[RouteDefinition]) -> None:
        compiled: list[CompiledRoute] = []
        for route in routes:
            if not route.enabled:
                continue
            compiled_route = self._compile(route)
            compiled.append(compiled_route)

        # Sort: Explicit priority, then specificity score, then pattern length
        compiled.sort(
            key=lambda item: (
                item.route.priority,
                item.specificity_score,
                len(item.route.path_pattern),
            ),
            reverse=True,
        )
        self._compiled_routes = compiled

    def match(self, method: str, path: str) -> MatchResult | None:
        normalized_path = self._normalize_path(path)
        method_upper = method.upper()

        for compiled_route in self._compiled_routes:
            if compiled_route.route.methods:
                allowed_methods = {m.value for m in compiled_route.route.methods}
                if method_upper not in allowed_methods:
                    continue

            match = compiled_route.regex.match(normalized_path)
            if match:
                path_params = {
                    k: v
                    for k, v in match.groupdict().items()
                    if not k.startswith("__") and v is not None
                }
                target_path = self._rewrite_path(normalized_path, compiled_route.route)
                return MatchResult(
                    route=compiled_route.route,
                    path_params=path_params,  # ty: ignore
                    target_path=target_path,
                )

        return None

    def get_allowed_methods_for_path(self, path: str) -> set[HTTPMethod]:
        normalized_path = self._normalize_path(path)
        allowed: set[HTTPMethod] = set()

        for compiled_route in self._compiled_routes:
            if compiled_route.regex.match(normalized_path):
                if not compiled_route.route.methods:
                    return set(HTTPMethod)
                allowed.update(compiled_route.route.methods)

        return allowed

    @staticmethod
    def _normalize_path(path: str) -> str:
        if not path:
            return "/"
        path = path.strip()
        if not path.startswith("/"):
            path = "/" + path
        # Normalize consecutive slashes
        path = re.sub(r"/+", "/", path)
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        return path

    def _compile(self, route: RouteDefinition) -> CompiledRoute:
        pattern = self._normalize_path(route.path_pattern)

        # Calculate specificity score and static prefix
        has_recursive_wildcard = "**" in pattern
        has_wildcard = "*" in pattern
        has_param = "{" in pattern and "}" in pattern

        # Find static prefix length
        first_special = len(pattern)
        for special in ["{", "*"]:
            idx = pattern.find(special)
            if idx != -1 and idx < first_special:
                first_special = idx
        static_prefix_len = first_special

        if not has_wildcard and not has_param:
            specificity = 1000 + len(pattern)
        elif has_recursive_wildcard:
            specificity = 100 + static_prefix_len
        else:
            specificity = 500 + static_prefix_len

        # Build regex
        regex_parts: list[str] = ["^"]
        param_names: list[str] = []

        pos = 0
        raw_pattern = pattern
        token_scanner = re.compile(r"(\*\*|\*|\{[a-zA-Z_][a-zA-Z0-9_]*\})")

        for m in token_scanner.finditer(raw_pattern):
            literal = raw_pattern[pos : m.start()]
            if literal:
                regex_parts.append(re.escape(literal))
            token = m.group(1)
            if token == "**":
                regex_parts.append(r"(?P<__rest__>.*)")
            elif token == "*":
                regex_parts.append(r"[^/]+")
            elif token.startswith("{") and token.endswith("}"):
                pname = token[1:-1]
                param_names.append(pname)
                regex_parts.append(rf"(?P<{pname}>[^/]+)")
            pos = m.end()

        tail = raw_pattern[pos:]
        if tail:
            regex_parts.append(re.escape(tail))

        # Allow optional trailing slash
        regex_parts.append(r"/?$")
        compiled_regex = re.compile("".join(regex_parts))

        return CompiledRoute(
            route=route,
            regex=compiled_regex,
            specificity_score=specificity,
            param_names=param_names,
        )

    def _rewrite_path(self, path: str, route: RouteDefinition) -> str:
        target = path
        strip = route.strip_prefix or (
            route.middleware.transform.strip_prefix if route.middleware else None
        )
        add = route.add_prefix or (
            route.middleware.transform.add_prefix if route.middleware else None
        )

        if strip:
            strip_norm = self._normalize_path(strip)
            if target == strip_norm:
                target = "/"
            elif target.startswith(strip_norm + "/"):
                target = target[len(strip_norm) :]

        if add:
            add_norm = self._normalize_path(add)
            target = add_norm if target == "/" else f"{add_norm}{target}"

        return self._normalize_path(target)
