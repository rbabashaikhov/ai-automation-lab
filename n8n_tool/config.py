"""Configuration loading for n8n_tool.

Reads N8N_BASE_URL and N8N_API_KEY from the environment (optionally
populated from a .env file). Never logs or prints the raw API key.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str

    @property
    def masked_api_key(self) -> str:
        """A safe-to-print representation of the API key, never the full value."""
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


def load_config(env_file: Path | str | None = None) -> Config:
    """Load configuration from the environment / a .env file.

    Existing environment variables always take precedence over values in
    the .env file, matching standard dotenv semantics.
    """
    dotenv_path = Path(env_file) if env_file is not None else Path(".env")
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=False)

    base_url = os.environ.get("N8N_BASE_URL", "").strip()
    api_key = os.environ.get("N8N_API_KEY", "").strip()

    missing = []
    if not base_url:
        missing.append("N8N_BASE_URL")
    if not api_key:
        missing.append("N8N_API_KEY")
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )

    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ConfigError(
            f"N8N_BASE_URL must start with http:// or https:// (got: {base_url!r})"
        )

    return Config(base_url=base_url.rstrip("/"), api_key=api_key)
