"""Runtime configuration. Secrets are files/environment, never API payloads or logs."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OA_", env_file=ROOT / ".env", extra="ignore")
    data_dir: Path = ROOT / "data"
    database_url: str = ""
    database_url_file: Path | None = None
    portfolio_root: Path = ROOT.parent
    public_url: str = "http://127.0.0.1:8000"
    production: bool = False
    live_submissions_enabled: bool = False
    paid_services_enabled: bool = False
    secret_key_file: Path | None = None
    owner_file: Path | None = None
    redis_url: str = "redis://redis:6379/0"
    google_client_id: str = ""
    google_client_secret_file: Path | None = None
    gmail_query: str = "newer_than:30d -category:promotions"
    openai_api_key_file: Path | None = None
    openai_model: str = ""
    search_api_key_file: Path | None = None
    browser_adapter_registry: Path | None = None
    tex_sandbox: str = "bubblewrap"
    minimum_free_disk_bytes: int = 100_000_000
    google_oauth_testing: bool = True
    search_request_cost_cents: int = 0
    openai_input_cents_per_million: int = 0
    openai_output_cents_per_million: int = 0
    email_notifications_enabled: bool = False
    backup_key_file: Path | None = None
    backup_directory: Path | None = None

    @property
    def db_url(self) -> str:
        if self.database_url_file:
            return self.database_url_file.read_text().strip()
        return self.database_url or f"sqlite:///{self.data_dir.resolve() / 'autopilot.sqlite'}"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def key_path(self) -> Path:
        return self.secret_key_file or self.data_dir / "secrets" / "key"

    @property
    def owner_path(self) -> Path:
        return self.owner_file or self.data_dir / "secrets" / "owner.json"

    def validate_runtime(self) -> None:
        if self.production:
            if not self.public_url.startswith("https://"):
                raise ValueError("Production requires an HTTPS public URL")
            if not self.db_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
            if not self.secret_key_file or not self.key_path.is_file():
                raise ValueError("Production requires an external encryption/signing key file")
            if not self.owner_path.is_file():
                raise ValueError("Set up the owner password before production startup")
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
