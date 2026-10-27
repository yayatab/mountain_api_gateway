from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    redis_url: str = "redis://localhost:6379/0"
    default_timeout: float = 30.0
    jwt_secret: str = "gateway-insecure-dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    routes_file_path: str | None = None
    log_level: str = "INFO"


settings = GatewaySettings()
