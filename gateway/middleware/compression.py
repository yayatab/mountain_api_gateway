import gzip
import logging
import zlib
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Request, Response
from fastapi.responses import Response as RawResponse
from fastapi.responses import StreamingResponse

from gateway.middleware.base import BaseMiddleware, CallNext
from gateway.models.context import GatewayContext
from gateway.models.route import CompressionConfig

logger = logging.getLogger("gateway.middleware.compression")

# Check if brotli is available
try:
    import brotli  # pyright: ignore[reportMissingImports]

    HAS_BROTLI = True
except ImportError:
    brotli = None
    HAS_BROTLI = False


class CompressionMiddleware(BaseMiddleware):
    @staticmethod
    def select_algorithm(accept_encoding_header: str, config: CompressionConfig) -> str | None:
        if not accept_encoding_header:
            return None

        # Parse accept-encoding tokens and quality weights
        parsed_encodings: list[tuple[str, float]] = []
        for part in accept_encoding_header.split(","):
            part = part.strip()
            if not part:
                continue
            if ";q=" in part:
                token, q_str = part.split(";q=", 1)
                try:
                    q = float(q_str.strip())
                except ValueError:
                    q = 1.0
            else:
                token = part
                q = 1.0
            parsed_encodings.append((token.strip().lower(), q))

        # Sort by quality weight descending
        parsed_encodings.sort(key=lambda x: x[1], reverse=True)

        supported_route_algos = {a.lower() for a in config.algorithms}

        for token, q in parsed_encodings:
            if q <= 0:
                continue
            if token == "br" and HAS_BROTLI and "br" in supported_route_algos:
                return "br"
            if token == "gzip" and "gzip" in supported_route_algos:
                return "gzip"
            if token == "deflate" and "deflate" in supported_route_algos:
                return "deflate"
            if token == "*":
                for fallback in ["gzip", "deflate"]:
                    if fallback in supported_route_algos:
                        return fallback
        return None

    @staticmethod
    def is_mime_compressible(content_type: str | None, mime_types: set[str]) -> bool:
        if not content_type:
            return False
        base_mime = content_type.split(";")[0].strip().lower()
        return base_mime in {m.lower() for m in mime_types}

    @staticmethod
    def append_vary(headers: dict[str, str], vary_value: str) -> None:
        existing = headers.get("vary")
        if not existing:
            headers["Vary"] = vary_value
        else:
            values = {v.strip().lower() for v in existing.split(",")}
            if vary_value.lower() not in values:
                headers["Vary"] = f"{existing}, {vary_value}"

    async def __call__(
        self,
        request: Request,
        context: GatewayContext,
        call_next: CallNext,
    ) -> Response:
        response = await call_next(request, context)

        route = context.matched_route
        if route is None or not route.middleware.compression.enabled:
            return response

        config = route.middleware.compression

        # Check existing Content-Encoding
        existing_encoding = response.headers.get("content-encoding", "").lower()
        if existing_encoding and existing_encoding != "identity":
            return response

        # Check MIME type eligibility
        content_type = response.headers.get("content-type")
        if not self.is_mime_compressible(content_type, config.mime_types):
            return response

        # Select algorithm based on client Accept-Encoding
        accept_encoding = request.headers.get("accept-encoding", "")
        selected_algo = self.select_algorithm(accept_encoding, config)
        if not selected_algo:
            return response

        # Check min size if content-length is present
        content_length_header = response.headers.get("content-length")
        if content_length_header:
            try:
                if int(content_length_header) < config.min_size_bytes:
                    return response
            except ValueError:
                pass

        # Compress StreamingResponse
        if isinstance(response, StreamingResponse):
            original_iterator = response.body_iterator

            def get_compressor(algo: str, level: int) -> Any:
                if algo == "gzip":
                    return zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
                if algo == "deflate":
                    return zlib.compressobj(level, zlib.DEFLATED, zlib.MAX_WBITS)
                if algo == "br" and HAS_BROTLI and brotli:
                    return brotli.Compressor(quality=level)
                return zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)

            first_chunks: list[bytes] = []
            total_size = 0

            # Buffer up to min_size_bytes to verify eligibility
            if config.min_size_bytes > 0:
                try:
                    async for chunk in original_iterator:
                        if chunk:
                            chunk_bytes = (
                                chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                            )
                            first_chunks.append(chunk_bytes)
                            total_size += len(chunk_bytes)
                            if total_size >= config.min_size_bytes:
                                break
                except Exception as exc:
                    logger.warning(
                        "Streaming probe failed [%s]: %s",
                        context.request_id,
                        exc,
                    )
                    raise

                if total_size < config.min_size_bytes:
                    # Payload is smaller than min_size_bytes: return uncompressed
                    async def passthrough_chunk_generator() -> AsyncGenerator[bytes, None]:
                        for chunk in first_chunks:
                            yield chunk

                    return StreamingResponse(
                        content=passthrough_chunk_generator(),
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        media_type=response.media_type,
                    )

            async def compressed_chunk_generator() -> AsyncGenerator[bytes, None]:
                compressor = get_compressor(selected_algo, config.compress_level)
                try:
                    for chunk in first_chunks:
                        compressed = compressor.compress(chunk)
                        if compressed:
                            yield compressed
                    async for chunk in original_iterator:
                        if chunk:
                            chunk_bytes = (
                                chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                            )
                            compressed = compressor.compress(chunk_bytes)
                            if compressed:
                                yield compressed
                    tail = compressor.flush()
                    if tail:
                        yield tail
                except Exception as exc:
                    logger.warning(
                        "Streaming compression interrupted [%s]: %s",
                        context.request_id,
                        exc,
                    )
                    raise

            new_headers = dict(response.headers)
            new_headers["Content-Encoding"] = selected_algo
            self.append_vary(new_headers, "Accept-Encoding")
            new_headers.pop("content-length", None)

            return StreamingResponse(
                content=compressed_chunk_generator(),
                status_code=response.status_code,
                headers=new_headers,
                media_type=response.media_type,
            )

        # Compress buffered Response
        body = getattr(response, "body", b"")
        if len(body) < config.min_size_bytes:
            return response

        try:
            if selected_algo == "gzip":
                compressed_body = gzip.compress(body, compresslevel=config.compress_level)
            elif selected_algo == "deflate":
                compressed_body = zlib.compress(body, level=config.compress_level)
            elif selected_algo == "br" and HAS_BROTLI and brotli:
                compressed_body = brotli.compress(body, quality=config.compress_level)
            else:
                compressed_body = gzip.compress(body, compresslevel=config.compress_level)
        except Exception as exc:
            logger.error(
                "Unexpected error compressing response body [%s]: %s",
                context.request_id,
                exc,
                exc_info=True,
            )
            return response

        new_headers = dict(response.headers)
        new_headers["Content-Encoding"] = selected_algo
        new_headers["Content-Length"] = str(len(compressed_body))
        self.append_vary(new_headers, "Accept-Encoding")

        return RawResponse(
            content=compressed_body,
            status_code=response.status_code,
            headers=new_headers,
            media_type=response.media_type,
        )
