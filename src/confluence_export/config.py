"""Configuration management: CLI args > env vars > config file."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

CONFIG_DIR_NAME = "confluence-export"
CONFIG_FILE = "config.json"

GATEWAY_HOST = "api.atlassian.com"
_SITE_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.atlassian\.net$", re.IGNORECASE)


def is_scoped_token(token: str) -> bool:
    """True if token looks like an Atlassian scoped API token (ATATT…=ADA…).

    Scoped tokens require requests to be routed through the OAuth gateway URL
    `https://api.atlassian.com/ex/confluence/{cloudId}/...` rather than the
    Confluence site URL. The `=ADA<hex>` suffix encodes the scope set; tokens
    without it are full-access (legacy) tokens that work with the site URL.
    """
    return bool(token) and token.startswith("ATATT") and "=ADA" in token


def is_atlassian_site_url(url: str) -> bool:
    """True if url points at a `*.atlassian.net` site (not the OAuth gateway)."""
    if not url:
        return False
    host = urlparse(url).hostname or ""
    return bool(_SITE_HOST_RE.match(host))


def gateway_url(cloud_id: str) -> str:
    """Build the OAuth gateway base URL for a Confluence cloud ID."""
    return f"https://{GATEWAY_HOST}/ex/confluence/{cloud_id}"


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

    @property
    def needs_token(self) -> bool:
        """True when no api_token is configured."""
        return not self.api_token

    def validate(self) -> None:
        if not self.base_url:
            raise ValueError("base_url is required")


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
