"""Configuration and resolved connection profiles."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

import requests

CONFIG_DIR_NAME = "confluence-export"
CONFIG_FILE = "config.json"
LOCAL_CONFIG_DIR_NAME = ".conex"

GATEWAY_HOST = "api.atlassian.com"
_SITE_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.atlassian\.net$", re.IGNORECASE)
_GATEWAY_PATH_RE = re.compile(r"^/ex/confluence/([^/]+)")


class AuthMode(Enum):
    BASIC_API_TOKEN = "basic_api_token"
    SCOPED_API_TOKEN = "scoped_api_token"
    BEARER_PAT = "bearer_pat"
    COOKIE = "cookie"


class ApiDialect(Enum):
    CLOUD_V2 = "cloud_v2"
    GATEWAY_V2 = "gateway_v2"
    COOKIE_V1 = "cookie_v1"


class ConnectionProfileError(ValueError):
    """Raised when a connection profile cannot be resolved."""


@dataclass
class AuthConfig:
    type: AuthMode | None
    email: str = ""
    token: str = field(default="", repr=False)
    cookie_header: str = field(default="", repr=False)


@dataclass
class ConnectionProfile:
    site_url: str
    api_base_url: str
    cloud_id: str | None
    auth_mode: AuthMode
    api_dialect: ApiDialect
    config_source: str
    interactive: bool
    auth: AuthConfig = field(repr=False)


@dataclass
class Config:
    """Backward-compatible legacy config shape.

    New command paths should use :class:`ConnectionProfile`. This class remains
    available for direct callers and older tests.
    """

    base_url: str
    email: str
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


@dataclass
class _ResolvedConfig:
    site_url: str = ""
    api_base_url: str = ""
    cloud_id: str | None = None
    auth: AuthConfig | None = None
    source: Path | None = None
    source_label: str = "CLI/environment"


def is_scoped_token(token: str) -> bool:
    """True if token looks like an Atlassian scoped API token (ATATT...=ADA...)."""
    return bool(token) and token.startswith("ATATT") and "=ADA" in token


def is_atlassian_site_url(url: str) -> bool:
    """True if url points at an HTTPS `*.atlassian.net` site."""
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = parsed.hostname or ""
    return bool(_SITE_HOST_RE.match(host))


def is_gateway_url(url: str) -> bool:
    """True if url points at Atlassian's Confluence OAuth gateway."""
    parsed = urlparse(url)
    return (parsed.hostname or "").lower() == GATEWAY_HOST and bool(
        _GATEWAY_PATH_RE.match(parsed.path)
    )


def gateway_cloud_id(url: str) -> str | None:
    """Extract the cloud ID from an OAuth gateway URL, if present."""
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() != GATEWAY_HOST:
        return None
    match = _GATEWAY_PATH_RE.match(parsed.path)
    return match.group(1) if match else None


def gateway_url(cloud_id: str) -> str:
    """Build the OAuth gateway base URL for a Confluence cloud ID."""
    return f"https://{GATEWAY_HOST}/ex/confluence/{cloud_id}"


def normalize_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _config_dir() -> Path:
    return Path.home() / ".config" / CONFIG_DIR_NAME


def config_path() -> Path:
    return _config_dir() / CONFIG_FILE


def cache_dir() -> Path:
    return _config_dir() / "cache"


def local_config_path(start_dir: str | Path) -> Path:
    return Path(start_dir).expanduser().resolve() / LOCAL_CONFIG_DIR_NAME / CONFIG_FILE


def find_local_config(start_dir: str | Path | None) -> Path | None:
    """Find the nearest local .conex/config.json from start_dir upward."""
    if start_dir is None:
        return None
    current = Path(start_dir).expanduser().resolve()
    for candidate_dir in (current, *current.parents):
        candidate = candidate_dir / LOCAL_CONFIG_DIR_NAME / CONFIG_FILE
        if candidate.exists():
            return candidate
    return None


def display_config_source(path: Path | None, fallback: str = "CLI/environment") -> str:
    if path is None:
        return fallback
    try:
        return f"~/{path.expanduser().resolve().relative_to(Path.home())}"
    except ValueError:
        return str(path)


def resolve_cloud_id(site_url: str) -> str | None:
    """Look up the Confluence cloud ID for a site URL via /_edge/tenant_info."""
    site_url = normalize_url(site_url)
    if not is_atlassian_site_url(site_url):
        return None
    try:
        resp = requests.get(
            site_url + "/_edge/tenant_info",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        cloud_id = resp.json().get("cloudId")
        return cloud_id if isinstance(cloud_id, str) and cloud_id else None
    except (requests.exceptions.RequestException, ValueError):
        return None


def _auth_mode(value: str | AuthMode | None) -> AuthMode | None:
    if value is None or value == "":
        return None
    if isinstance(value, AuthMode):
        return value
    try:
        return AuthMode(str(value))
    except ValueError as exc:
        values = ", ".join(mode.value for mode in AuthMode)
        raise ConnectionProfileError(
            f"unknown auth type '{value}' (expected one of: {values})"
        ) from exc


def _infer_auth_mode(
    *,
    explicit: str | AuthMode | None,
    email: str,
    token: str,
    cookie_header: str,
) -> AuthMode:
    explicit_mode = _auth_mode(explicit)
    if explicit_mode is not None:
        return explicit_mode
    if cookie_header:
        return AuthMode.COOKIE
    if email and token:
        if is_scoped_token(token):
            return AuthMode.SCOPED_API_TOKEN
        return AuthMode.BASIC_API_TOKEN
    if token:
        if is_scoped_token(token):
            return AuthMode.SCOPED_API_TOKEN
        return AuthMode.BEARER_PAT
    if email:
        return AuthMode.BASIC_API_TOKEN
    raise ConnectionProfileError("authentication credentials are required")


def _read_json(path: Path) -> dict:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConnectionProfileError(f"config file must contain a JSON object: {path}")
    return data


def _auth_from_v1(data: dict) -> AuthConfig:
    email = str(data.get("email", "") or "")
    token = str(data.get("api_token", "") or "")
    mode = _infer_auth_mode(
        explicit=None,
        email=email,
        token=token,
        cookie_header="",
    )
    return AuthConfig(type=mode, email=email, token=token)


def _parse_file_config(path: Path) -> _ResolvedConfig:
    data = _read_json(path)
    if data.get("version") == 2:
        auth_data = data.get("auth") or {}
        if not isinstance(auth_data, dict):
            raise ConnectionProfileError(f"auth must be an object in {path}")
        token = str(auth_data.get("token", "") or "")
        email = str(auth_data.get("email", "") or "")
        cookie_header = str(auth_data.get("cookie_header", "") or "")
        auth_type = auth_data.get("type")
        mode = _infer_auth_mode(
            explicit=auth_type,
            email=email,
            token=token,
            cookie_header=cookie_header,
        )
        return _ResolvedConfig(
            site_url=normalize_url(str(data.get("site_url", "") or "")),
            api_base_url=normalize_url(str(data.get("api_base_url", "") or "")),
            cloud_id=str(data.get("cloud_id") or "") or None,
            auth=AuthConfig(
                type=mode,
                email=email,
                token=token,
                cookie_header=cookie_header,
            ),
            source=path,
            source_label=display_config_source(path),
        )

    base_url = normalize_url(str(data.get("base_url", "") or ""))
    if is_gateway_url(base_url):
        raise ConnectionProfileError(
            "config base_url is the OAuth gateway; run `confluence-export configure` "
            "with the Confluence site URL"
        )
    cloud_id = gateway_cloud_id(base_url)
    return _ResolvedConfig(
        site_url=base_url,
        api_base_url=base_url if is_gateway_url(base_url) else "",
        cloud_id=cloud_id,
        auth=_auth_from_v1(data),
        source=path,
        source_label=display_config_source(path),
    )


def _overlay(base: _ResolvedConfig, override: _ResolvedConfig) -> _ResolvedConfig:
    override_auth_has_credentials = False
    if base.auth and override.auth:
        override_auth_has_cookie = bool(override.auth.cookie_header)
        override_auth_has_tokenish = bool(override.auth.email or override.auth.token)
        override_auth_has_credentials = override_auth_has_cookie or override_auth_has_tokenish
        if override.auth.type is not None:
            auth_type = override.auth.type
        elif override_auth_has_credentials:
            auth_type = None
        else:
            auth_type = base.auth.type
        auth = AuthConfig(
            type=auth_type,
            email=override.auth.email or ("" if override_auth_has_cookie else base.auth.email),
            token=override.auth.token or ("" if override_auth_has_cookie else base.auth.token),
            cookie_header=override.auth.cookie_header or (
                "" if override_auth_has_tokenish else base.auth.cookie_header
            ),
        )
    else:
        auth = override.auth or base.auth
        override_auth_has_credentials = bool(
            override.auth and (
                override.auth.email or override.auth.token or override.auth.cookie_header
            )
        )

    site_url_overridden = bool(override.site_url)
    merged_auth_has_credentials = bool(
        auth and (auth.email or auth.token or auth.cookie_header)
    )
    # A higher-priority explicit non-scoped auth mode may intentionally inherit
    # credentials from a lower-priority config. It still invalidates cached
    # gateway routing because the OAuth gateway is scoped-token-only transport.
    override_auth_forces_site_route = bool(
        override.auth and override.auth.type in (
            AuthMode.BASIC_API_TOKEN,
            AuthMode.BEARER_PAT,
            AuthMode.COOKIE,
        )
        and merged_auth_has_credentials
    )
    clear_derived_route = (
        (site_url_overridden or override_auth_has_credentials or override_auth_forces_site_route)
        and not override.api_base_url
    )

    return _ResolvedConfig(
        site_url=override.site_url or base.site_url,
        api_base_url=override.api_base_url or ("" if clear_derived_route else base.api_base_url),
        cloud_id=override.cloud_id if override.cloud_id is not None else (
            None if (
                site_url_overridden
                or override_auth_has_credentials
                or override_auth_forces_site_route
            ) else base.cloud_id
        ),
        auth=auth,
        source=override.source or base.source,
        source_label=override.source_label if override.source else base.source_label,
    )


def _env_config() -> _ResolvedConfig:
    site_url = os.environ.get("CONFLUENCE_SITE_URL") or os.environ.get("CONFLUENCE_BASE_URL") or ""
    api_base_url = os.environ.get("CONFLUENCE_API_BASE_URL") or ""
    cloud_id = os.environ.get("CONFLUENCE_CLOUD_ID") or None
    email = os.environ.get("CONFLUENCE_EMAIL") or ""
    token = os.environ.get("CONFLUENCE_API_TOKEN") or os.environ.get("CONFLUENCE_PAT") or ""
    cookie_header = os.environ.get("CONFLUENCE_COOKIE") or ""
    auth_type = os.environ.get("CONFLUENCE_AUTH_TYPE")

    auth = None
    if any((email, token, cookie_header, auth_type)):
        auth = AuthConfig(
            type=_auth_mode(auth_type),
            email=email,
            token=token,
            cookie_header=cookie_header,
        )
    return _ResolvedConfig(
        site_url=normalize_url(site_url),
        api_base_url=normalize_url(api_base_url),
        cloud_id=cloud_id,
        auth=auth,
    )


def _cli_config(
    *,
    site_url: str | None = None,
    base_url: str | None = None,
    api_base_url: str | None = None,
    cloud_id: str | None = None,
    auth_type: str | AuthMode | None = None,
    email: str | None = None,
    api_token: str | None = None,
    cookie: str | None = None,
) -> _ResolvedConfig:
    chosen_site_url = site_url or base_url or ""
    auth = None
    if any(v is not None for v in (auth_type, email, api_token, cookie)):
        auth = AuthConfig(
            type=_auth_mode(auth_type) if auth_type is not None else (
                AuthMode.COOKIE if cookie is not None else None
            ),
            email=email or "",
            token=api_token or "",
            cookie_header=cookie or "",
        )
    return _ResolvedConfig(
        site_url=normalize_url(chosen_site_url),
        api_base_url=normalize_url(api_base_url or ""),
        cloud_id=cloud_id or None,
        auth=auth,
    )


def _resolve_api_base(
    *,
    site_url: str,
    api_base_url: str,
    cloud_id: str | None,
    auth_mode: AuthMode,
    resolve_cloud: Callable[[str], str | None],
) -> tuple[str, str | None, ApiDialect]:
    if auth_mode is AuthMode.COOKIE:
        if is_gateway_url(site_url):
            raise ConnectionProfileError(
                "cookie authentication requires a Confluence site URL, not the OAuth gateway URL"
            )
        return site_url, None, ApiDialect.COOKIE_V1

    if auth_mode is AuthMode.SCOPED_API_TOKEN:
        if api_base_url:
            if not is_gateway_url(api_base_url):
                raise ConnectionProfileError(
                    "scoped token api_base_url must be the OAuth gateway URL"
                )
            return api_base_url, cloud_id or gateway_cloud_id(api_base_url), ApiDialect.GATEWAY_V2
        resolved_cloud_id = cloud_id or resolve_cloud(site_url)
        if not resolved_cloud_id:
            raise ConnectionProfileError(
                "scoped token requires OAuth gateway routing, but cloud ID could not be resolved"
            )
        return gateway_url(resolved_cloud_id), resolved_cloud_id, ApiDialect.GATEWAY_V2

    if is_gateway_url(api_base_url):
        raise ConnectionProfileError("OAuth gateway api_base_url requires scoped API token auth")

    return api_base_url or site_url, None, ApiDialect.CLOUD_V2


def load_connection_profile(
    *,
    site_url: str | None = None,
    base_url: str | None = None,
    api_base_url: str | None = None,
    cloud_id: str | None = None,
    auth_type: str | AuthMode | None = None,
    email: str | None = None,
    api_token: str | None = None,
    cookie: str | None = None,
    start_dir: str | Path | None = None,
    interactive: bool | None = None,
    resolve_cloud: Callable[[str], str | None] = resolve_cloud_id,
) -> ConnectionProfile:
    """Resolve CLI/env/local/global config into an explicit connection profile."""
    merged = _ResolvedConfig()

    global_path = config_path()
    if global_path.exists():
        merged = _overlay(merged, _parse_file_config(global_path))

    local_path = find_local_config(start_dir)
    if local_path is not None:
        merged = _overlay(merged, _parse_file_config(local_path))

    merged = _overlay(merged, _env_config())
    merged = _overlay(
        merged,
        _cli_config(
            site_url=site_url,
            base_url=base_url,
            api_base_url=api_base_url,
            cloud_id=cloud_id,
            auth_type=auth_type,
            email=email,
            api_token=api_token,
            cookie=cookie,
        ),
    )

    if merged.auth is None:
        raise ConnectionProfileError("authentication credentials are required")
    if not merged.site_url:
        raise ConnectionProfileError("site_url is required")
    if is_gateway_url(merged.site_url):
        raise ConnectionProfileError(
            "site_url must be the Confluence site URL, not the OAuth gateway URL"
        )

    auth = merged.auth
    auth.type = _infer_auth_mode(
        explicit=auth.type,
        email=auth.email,
        token=auth.token,
        cookie_header=auth.cookie_header,
    )
    if auth.type is AuthMode.COOKIE and not auth.cookie_header:
        raise ConnectionProfileError("cookie authentication requires a cookie header")
    if auth.type in (AuthMode.BASIC_API_TOKEN, AuthMode.SCOPED_API_TOKEN) and (
        not auth.email or not auth.token
    ):
        raise ConnectionProfileError("email and API token are required for API token authentication")
    if auth.type is AuthMode.BEARER_PAT and not auth.token:
        raise ConnectionProfileError("bearer/PAT authentication requires a token")
    api_base, resolved_cloud_id, dialect = _resolve_api_base(
        site_url=merged.site_url,
        api_base_url=merged.api_base_url,
        cloud_id=merged.cloud_id,
        auth_mode=auth.type,
        resolve_cloud=resolve_cloud,
    )

    return ConnectionProfile(
        site_url=merged.site_url,
        api_base_url=api_base,
        cloud_id=resolved_cloud_id,
        auth_mode=auth.type,
        api_dialect=dialect,
        config_source=merged.source_label,
        interactive=sys.stdin.isatty() if interactive is None else interactive,
        auth=auth,
    )


def load_config(
    base_url: str | None = None,
    email: str | None = None,
    api_token: str | None = None,
) -> Config:
    """Load legacy config with priority: explicit args > env vars > global file.

    This compatibility helper intentionally ignores local config discovery. New
    command paths use :func:`load_connection_profile`.
    """
    file_cfg: dict = {}
    cp = config_path()
    if cp.exists():
        file_cfg = _read_json(cp)

    if file_cfg.get("version") == 2:
        auth_data = file_cfg.get("auth") or {}
        file_base_url = file_cfg.get("api_base_url") or file_cfg.get("site_url", "")
        file_email = auth_data.get("email", "")
        file_token = auth_data.get("token", "")
    else:
        file_base_url = file_cfg.get("base_url", "")
        file_email = file_cfg.get("email", "")
        file_token = file_cfg.get("api_token", "")

    cfg = Config(
        base_url=(
            base_url
            or os.environ.get("CONFLUENCE_BASE_URL")
            or os.environ.get("CONFLUENCE_SITE_URL")
            or str(file_base_url or "")
        ),
        email=(email or os.environ.get("CONFLUENCE_EMAIL") or str(file_email or "")),
        api_token=(
            api_token
            or os.environ.get("CONFLUENCE_API_TOKEN")
            or os.environ.get("CONFLUENCE_PAT")
            or str(file_token or "")
        ),
    )
    cfg.base_url = normalize_url(cfg.base_url)
    cfg.validate()
    return cfg


def _config_to_v2_dict(
    *,
    site_url: str,
    auth: AuthConfig,
    cloud_id: str | None = None,
    api_base_url: str = "",
) -> dict:
    if auth.type is None:
        raise ConnectionProfileError("auth type is required")
    if is_gateway_url(site_url):
        raise ConnectionProfileError(
            "site_url must be the Confluence site URL, not the OAuth gateway URL"
        )
    data = {
        "version": 2,
        "site_url": normalize_url(site_url),
        "auth": {
            "type": auth.type.value,
        },
    }
    if cloud_id:
        data["cloud_id"] = cloud_id
    if api_base_url:
        data["api_base_url"] = normalize_url(api_base_url)
    if auth.email:
        data["auth"]["email"] = auth.email
    if auth.token:
        data["auth"]["token"] = auth.token
    if auth.cookie_header:
        data["auth"]["cookie_header"] = auth.cookie_header
    return data


def save_connection_config(
    *,
    site_url: str,
    auth: AuthConfig,
    path: Path | None = None,
    cloud_id: str | None = None,
    api_base_url: str = "",
) -> Path:
    """Save a v2 connection config."""
    cp = path or config_path()
    cp.parent.mkdir(parents=True, exist_ok=True)
    data = _config_to_v2_dict(
        site_url=site_url,
        auth=auth,
        cloud_id=cloud_id,
        api_base_url=api_base_url,
    )
    fd, tmp_name = tempfile.mkstemp(prefix=f".{cp.name}.", dir=cp.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, cp)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    cp.chmod(0o600)
    return cp


def save_config(cfg: Config) -> Path:
    """Save a legacy Config as a v2 global config."""
    auth_type = AuthMode.BEARER_PAT if cfg.use_bearer else (
        AuthMode.SCOPED_API_TOKEN if is_scoped_token(cfg.api_token) else AuthMode.BASIC_API_TOKEN
    )
    return save_connection_config(
        site_url=cfg.base_url,
        auth=AuthConfig(type=auth_type, email=cfg.email, token=cfg.api_token),
    )
