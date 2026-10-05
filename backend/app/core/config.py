from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Orbit | URL Engineering"
    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./agentic.db"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:3000"]
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    llm_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:0.5b"
    max_workflow_retries: int = 3
    max_total_task_attempts: int = 6
    workspace_path: str = "/workspace"
    max_codebase_files: int = 500
    redirect_rate_limit: int = 120
    redirect_cache_seconds: int = 300
    requester_token: str = "local-requester-token"
    reviewer_token: str = "local-reviewer-token"
    sandbox_timeout_seconds: int = 120
    sandbox_image: str = "agentic-url-api:local"
    worker_lease_seconds: int = 30
    worker_poll_seconds: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
