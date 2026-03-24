"""Configuration management: CLI args > env vars > config file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR_NAME = "confluence-export"
CONFIG_FILE = "config.json"


def _config_dir() -> Path:
    return Path.home() / ".config" / CONFIG_DIR_NAME


def config_path() -> Path:
    return _config_dir() / CONFIG_FILE


def cache_dir() -> Path:
    return _config_dir() / "cache"


@dataclass
class Config:
    base_url: str
    email: str  # optional — empty means use Bearer auth with PAT
    api_token: str

    @property
    def use_bearer(self) -> bool:
        """True when no email is set, meaning api_token is a PAT for Bearer auth."""
        return not self.email

    def validate(self) -> None:
        if not self.base_url:
            raise ValueError("base_url is required")
        if not self.api_token:
            raise ValueError("api_token is required (API token or PAT)")


def load_config(
    base_url: str | None = None,
    email: str | None = None,
    api_token: str | None = None,
) -> Config:
    """Load config with priority: explicit args > env vars > config file."""
    # Start with config file values
    file_cfg: dict = {}
    cp = config_path()
    if cp.exists():
        with open(cp) as f:
            file_cfg = json.load(f)

    cfg = Config(
        base_url=(
            base_url
            or os.environ.get("CONFLUENCE_BASE_URL")
            or file_cfg.get("base_url", "")
        ),
        email=(
            email
            or os.environ.get("CONFLUENCE_EMAIL")
            or file_cfg.get("email", "")
        ),
        api_token=(
            api_token
            or os.environ.get("CONFLUENCE_API_TOKEN")
            or os.environ.get("CONFLUENCE_PAT")
            or file_cfg.get("api_token", "")
        ),
    )

    # Strip trailing slashes from base_url
    cfg.base_url = cfg.base_url.rstrip("/")
    cfg.validate()
    return cfg


def save_config(cfg: Config) -> Path:
    """Save config to ~/.config/confluence-export/config.json."""
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)

    data = {
        "base_url": cfg.base_url,
        "email": cfg.email,
        "api_token": cfg.api_token,
    }

    cp = config_path()
    with open(cp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    # Restrict permissions to owner only
    cp.chmod(0o600)
    return cp
