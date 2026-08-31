from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    prometheus_url: str = "http://prometheus:9090"
    loki_url: str = "http://loki:3100"
    docker_host: str = "unix:///var/run/docker.sock"
    executor_gateway_url: str = "http://executor-gateway:8090"
    executor_identity_key: str = "opspilot-local-workload-signing-key"
    executor_identity_issuer: str = "opspilot-control-api"
    executor_identity_audience: str = "opspilot-executor-gateway"
    executor_identity_subject: str = "control-api"
    executor_identity_ttl_seconds: int = 10
    executor_gateway_timeout: float = 15
    database_path: str = "/data/opspilot.db"
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str = ""
    embedding_timeout: float = 10
    semantic_minimum_similarity: float = 0.75
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
