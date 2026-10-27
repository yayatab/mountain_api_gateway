# AI Tool Usage

## Tools I Used

- Antigravity (agy-cli / Gemini) agentic coding assistant for architecture scaffolding, RFC 7230 edge case analysis, and test scenario generation
- I used Gemini 3.8 and Gemini 4

## What Helped Most

1. Rapid generation of comprehensive test suites covering edge cases in path parsing (wildcard globs `**`, path parameters `{id}`, specificity ordering) and mock ASGI server transports.
2. Formulating clean Pydantic v2 schemas and FastAPI lifespan integration patterns.
3. Implementing the asynchronous middleware pipeline with a recursive chain-of-responsibility pattern, and dual-backend rate limiting (Redis sliding-window sorted sets with transparent in-memory fallback).
4. Refactoring container types from `list` to `set` across models (`RouteDefinition`, `BackendInstance`, `GatewayContext`, `AuthMiddlewareConfig`, `TransformConfig`) to eliminate duplicate instances, prevent redundant probes, and ensure deterministic round-robin load balancing.

## What I Had to Fix
![img.png](img.png)

Using 
1. **Coding**
* AI preferred to write it's won code instead of using built in libraries
* some places were used the wrong container (list instead of set)
* Adding (or telling the AI to add) more tests. and just happy-flow tests, also what-if tests such as 'unauthorized'
* type systems (containers as none instead of empty containers)

## What AI Struggled With

- Subtle HTTP/1.1 framing mechanics when combining streaming chunks with hop-by-hop headers and connection release semantics (`stream=True` in HTTPX requiring an explicit `response.aclose()` in a finally block to avoid leaking pool connections).
- Starlette Request headers caching: modifying ASGI scope headers in middleware (`request.scope["headers"]`) does not automatically invalidate Starlette's cached `request.headers` attribute. To ensure downstream proxy engines read headers injected or stripped by middleware, the proxy engine must re-instantiate `Headers(scope=request.scope)` or explicitly propagate state from `GatewayContext`.
- type system. the AI did not realize when somethin can/should be nullable. and whether a container should be a set 