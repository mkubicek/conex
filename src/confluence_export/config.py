from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

CONFIG_DIR_NAME = "confluence-export"
CONFIG_FILE = "config.json"
LOCAL_CONFIG_DIR = ".conex"

GATEWAY_HOST = "api.atlassian.com"
_SITE_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.atlassian\.net$", re.IGNORECASE)


class AuthMode(str, Enum):
    BASIC_API_TOKEN = "basic_api_token"
    SCOPED_API_TOKEN = "scoped_api_token"
    BEARER_PAT = "bearer_pat"
    COOKIE = "cookie"


class ApiDialect(str, Enum):
    CLOUD_V2 = "cloud_v2"
    GATEWAY_V2 = "gateway_v2"
    COOKIE_V1 = "cookie_v1"


@dataclass
class ConnectionProfile:
    site_url: str
    api_base_url: str
    cloud_id: str | None
    auth_mode: AuthMode
    api_dialect: ApiDialect
    config_source: str
    interactive: bool


def is_scoped_token(token: str) -> bool:
    return bool(token) and token.startswith("ATATT") and "=ADA" in token


def is_atlassian_site_url(url: str) -> bool:
    if not url:
        return False
    host = urlparse(url).hostname or ""
    return bool(_SITE_HOST_RE.match(host))


def gateway_url(cloud_id: str) -> str:
    return f"https://{GATEWAY_HOST}/ex/confluence/{cloud_id}"


def _config_dir() -> Path:
    return Path.home() / ".config" / CONFIG_DIR_NAME


def config_path() -> Path:
    return _config_dir() / CONFIG_FILE


def cache_dir() -> Path:
    return _config_dir() / "cache"


def find_local_config(start_dir: Path | None) -> Path | None:
    if start_dir is None:
        return None
    d = start_dir.resolve()
    for parent in (d, *d.parents):
        p = parent / LOCAL_CONFIG_DIR / CONFIG_FILE
        if p.exists():
            return p
    return None


@dataclass
class Config:
    base_url: str
    email: str
    api_token: str
    site_url: str = ""
    cloud_id: str | None = None
    auth_type: str = ""
    api_base_url: str = ""
    config_source: str = ""

    @property
    def use_bearer(self) -> bool:
        return not self.email

    @property
    def needs_token(self) -> bool:
        return not self.api_token

    def validate(self) -> None:
        if not self.base_url:
            raise ValueError("base_url is required")


def _parse_config_data(data: dict) -> dict:
    if data.get("version") == 2:
        auth = data.get("auth", {})
        site_url = (data.get("site_url") or "").rstrip("/")
        api_base = (data.get("api_base_url") or site_url).rstrip("/")
        return {
            "site_url": site_url,
            "base_url": api_base,
            "api_base_url": api_base,
            "cloud_id": data.get("cloud_id"),
            "email": auth.get("email", ""),
            "api_token": auth.get("token", ""),
            "auth_type": auth.get("type", ""),
        }

    # v1 migration
    base = (data.get("base_url") or "").rstrip("/")
    return {
        "site_url": base,
        "base_url": base,
        "api_base_url": base,
        "cloud_id": None,
        "email": data.get("email", ""),
        "api_token": data.get("api_token", ""),
        "auth_type": "",
    }


def load_config(base_url: str | None = None, email: str | None = None, api_token: str | None = None, output_dir: str | None = None) -> Config:
    file_cfg: dict = {}
    src = ""
    local = find_local_config(Path(output_dir).resolve() if output_dir else None)
    cp = local or config_path()
    if cp.exists():
        file_cfg = _parse_config_data(json.loads(cp.read_text()))
        src = str(cp)

    resolved_base = (base_url or os.environ.get("CONFLUENCE_BASE_URL") or file_cfg.get("base_url", "")).rstrip("/")
    cfg = Config(
        base_url=resolved_base,
        email=email or os.environ.get("CONFLUENCE_EMAIL") or file_cfg.get("email", ""),
        api_token=api_token or os.environ.get("CONFLUENCE_API_TOKEN") or os.environ.get("CONFLUENCE_PAT") or file_cfg.get("api_token", ""),
        site_url=(os.environ.get("CONFLUENCE_SITE_URL") or file_cfg.get("site_url") or resolved_base).rstrip("/"),
        cloud_id=os.environ.get("CONFLUENCE_CLOUD_ID") or file_cfg.get("cloud_id"),
        auth_type=os.environ.get("CONFLUENCE_AUTH_TYPE") or file_cfg.get("auth_type", ""),
        api_base_url=(os.environ.get("CONFLUENCE_API_BASE_URL") or file_cfg.get("api_base_url") or resolved_base).rstrip("/"),
        config_source=src,
    )
    cfg.validate()
    return cfg


def save_config(cfg: Config, path: Path | None = None) -> Path:
    cp = path or config_path()
    cp.parent.mkdir(parents=True, exist_ok=True)
    auth_type = cfg.auth_type or ("bearer_pat" if cfg.use_bearer else ("scoped_api_token" if is_scoped_token(cfg.api_token) else "basic_api_token"))
    data = {
        "version": 2,
        "site_url": (cfg.site_url or cfg.base_url).rstrip("/"),
        "cloud_id": cfg.cloud_id,
        "api_base_url": (cfg.api_base_url or cfg.base_url).rstrip("/"),
        "auth": {
            "type": auth_type,
            "email": cfg.email,
            "token": cfg.api_token,
            "cookie_header": "",
        },
    }
    cp.write_text(json.dumps(data, indent=2) + "\n")
    cp.chmod(0o600)
    return cp
