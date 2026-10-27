import httpx


class ProxyHttpClientPool:
    def __init__(
        self,
        max_connections: int = 1000,
        max_keepalive_connections: int = 100,
        keepalive_expiry: float = 30.0,
    ) -> None:
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
        )
        self._client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=self._limits,
                follow_redirects=False,
            )

    async def shutdown(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=self._limits,
                follow_redirects=False,
            )
        return self._client


# Global default client pool instance
client_pool = ProxyHttpClientPool()
