from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

BACKEND_DIR = Path(__file__).resolve().parent.parent  # <workspace>/backend, independent of the shell's cwd
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    """All configuration comes from environment variables (or backend/.env, then the workspace .env)."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Where records (projects, sessions, versions, screenshot metadata) live. "mysql" is the default and is never replaced
    # silently; "folder" is an explicit opt-in that stores JSON files under FOLDER_STORE_DIR (single backend process only).
    persistence: Literal["mysql", "folder"] = "mysql"
    folder_store_dir: Path = BACKEND_DIR / "storage" / "data"

    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "screenshot_react_builder"
    db_user: str = ""  # no default account: set DB_USER in .env
    db_password: str = ""

    # Pool settings
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_connect_timeout_seconds: int = 3

    # Screenshots
    screenshot_storage_dir: Path = BACKEND_DIR / "storage" / "screenshots"
    max_screenshot_bytes: int = 10 * 1024 * 1024
    max_screenshots_per_prompt: int = 4  # per message; unsent screenshots waiting in a project count toward it

    # --- AI generation (Phase 3). Secrets come only from the environment / .env; never sent to the frontend. ---
    # Any OpenAI-compatible chat-completions endpoint. Default: Hugging Face Inference Providers router.
    ai_base_url: str = "https://router.huggingface.co/v1"
    ai_api_key: SecretStr = Field(default=SecretStr(""), validation_alias=AliasChoices("AI_API_KEY", "HF_TOKEN"))
    ai_code_model: str = "Qwen/Qwen3-Coder-30B-A3B-Instruct"  # prompt-only requests
    ai_vision_model: str = "Qwen/Qwen3-VL-32B-Instruct"  # requests that include screenshots
    ai_timeout_seconds: int = 300
    ai_max_output_tokens: int = 16000
    max_prompt_chars: int = 8000
    max_total_image_bytes: int = 12 * 1024 * 1024  # all screenshots of one request, before base64 (+33%)
    max_generated_files: int = 30
    max_generated_file_bytes: int = 200_000
    max_repair_attempts: int = 1
    max_session_attempts: int = 3
    generated_storage_dir: Path = BACKEND_DIR / "storage" / "generated"

    # Build sandbox. "disabled" (default) refuses to build: generated code is never run unsandboxed.
    build_runner: str = "disabled"  # "disabled" | "docker" | "host" (host = explicit opt-in, UNSANDBOXED)
    # node_modules holding typescript, vite, react, react-dom, @vitejs/plugin-react for the host runner (default: the Application's own)
    build_toolchain_dir: Path | None = None
    build_image: str = "screenshot-builder-sandbox:1"
    build_timeout_seconds: int = 180
    build_memory: str = "1g"
    build_cpus: str = "1"
    build_max_dist_bytes: int = 20 * 1024 * 1024

    # CORS: comma-separated list of exact origins allowed to call the API
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Name of the database used by automated tests (must end with "_test").
    test_db_name: str = Field(default="screenshot_react_builder_test")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def resolved_storage_dir(self) -> Path:
        path = self.screenshot_storage_dir
        if not path.is_absolute():
            path = BACKEND_DIR / path
        return path.resolve()

    @property
    def resolved_build_toolchain_dir(self) -> Path:
        path = self.build_toolchain_dir or BACKEND_DIR.parent / "node_modules"
        if not path.is_absolute():
            path = BACKEND_DIR / path
        return path.resolve()

    @property
    def resolved_folder_store_dir(self) -> Path:
        path = self.folder_store_dir
        if not path.is_absolute():
            path = BACKEND_DIR / path
        return path.resolve()

    @property
    def resolved_generated_dir(self) -> Path:
        path = self.generated_storage_dir
        if not path.is_absolute():
            path = BACKEND_DIR / path
        return path.resolve()

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_api_key.get_secret_value().strip())

    def database_url(self, database: str | None = None) -> URL:
        """Build the SQLAlchemy URL. URL.create escapes special characters in the password."""
        return URL.create(
            "mysql+pymysql",
            username=self.db_user,
            password=self.db_password or None,
            host=self.db_host,
            port=self.db_port,
            database=database or self.db_name,
            query={"charset": "utf8mb4"},
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
