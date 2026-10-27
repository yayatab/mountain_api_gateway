# AI Tool Usage

## Tools I Used

- Antigravity (agy-cli / Gemini) agentic coding assistant for architecture scaffolding, RFC 7230 edge case analysis, and test scenario generation

## What Helped Most

1. Rapid generation of comprehensive test suites covering edge cases in path parsing (wildcard globs `**`, path parameters `{id}`, specificity ordering) and mock ASGI server transports.
2. Formulating clean Pydantic v2 schemas and FastAPI lifespan integration patterns.

## What I Had to Fix
![img.png](img.png)

Using 
1. **Coding**
* AI preferred to write it's won code instead of using built in libraries
* some places were used the wrong container (list instead of set)

## What AI Struggled With

- Subtle HTTP/1.1 framing mechanics when combining streaming chunks with hop-by-hop headers and connection release semantics (`stream=True` in HTTPX requiring an explicit `response.aclose()` in a finally block to avoid leaking pool connections).
