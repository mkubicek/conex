"""Configuration resolution for conex v2.

This module starts as an orchestrator-seeded stub: `Dialect` and
`ResolvedConfig` are the shared types the api/ adapters compile against and
are FROZEN by SPEC-V2.md — extend this file with the loading/resolution
logic (config worker package) without changing these two definitions.

Precedence (highest to lowest): CLI overrides > env vars > local config
(.conex/config.json discovered upward from output_dir) > global config
(~/.config/confluence-export/config.json).

Auth modes:
- email + api-token  -> BASIC (basic auth Base64 header)
- scoped token (ATATT...=ADA...)  -> GATEWAY_V2 (Bearer + cloud-id lookup)
- PAT (no email)  -> CLOUD_V2 Bearer
- cookie header  -> COOKIE_V1

Secret files are written with mode 0600.
Never prompts when stdin is not a tty (non-interactive runs).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Frozen public types (SPEC-mandated — do NOT change)
# ---------------------------------------------------------------------------


class Dialect(Enum):
    """Which Confluence REST surface the resolved credentials can address."""

    CLOUD_V2 = "cloud_v2"      # /wiki/api/v2 on the site URL
    GATEWAY_V2 = "gateway_v2"  # v2 via https://api.atlassian.com/ex/confluence/{cloudId}
    COOKIE_V1 = "cookie_v1"    # legacy /wiki/rest/api with browser session cookies


@dataclass(frozen=True)
class ResolvedConfig:
    site_url: str
    api_base_url: str
    auth_headers: dict[str, str]
    dialect: Dialect
    email: str = ""
    verbose: bool = False
    source_description: str = ""


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_GLOBAL_CONFIG_DIR = Path.home() / ".config" / "confluence-export"
_GLOBAL_CONFIG_PATH = _GLOBAL_CONFIG_DIR / "config.json"
_LOCAL_CONFIG_DIR = ".conex"
_LOCAL_CONFIG_FILE = "config.json"
_GATEWAY_HOST = "api.atlassian.com"


# ---------------------------------------------------------------------------
# Errors  (re-exported from errors for callers who import from config)
# ---------------------------------------------------------------------------

from conex.errors import AuthError, ConfigError  # noqa: E402 (after stdlib)


# ---------------------------------------------------------------------------
# Token / URL helpers
# ---------------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _is_scoped_token(token: str) -> bool:
    """True if token is an Atlassian scoped API token (ATATT…=ADA…)."""
    return bool(token) and token.startswith("ATATT") and "=ADA" in token


def _is_atlassian_site_url(url: str) -> bool:
    """True when url is an https://*.atlassian.net URL."""
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host.endswith(".atlassian.net") and "." in host


def _gateway_url(cloud_id: str) -> str:
    return f"https://{_GATEWAY_HOST}/ex/confluence/{cloud_id}"


def _resolve_cloud_id(site_url: str) -> str | None:
    """Look up the Confluence cloud ID via /_edge/tenant_info.

    Returns None on any error or if the URL is not an Atlassian site.
    """
    site_url = _normalize_url(site_url)
    if not _is_atlassian_site_url(site_url):
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
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Internal raw-config dataclass (before validation)
# ---------------------------------------------------------------------------


@dataclass
class _RawConfig:
    """Mutable bag collected from one config source before merging."""

    site_url: str = ""
    cloud_id: str = ""
    api_base_url: str = ""  # explicit CONFLUENCE_API_BASE_URL override; "" = derive
    email: str = ""
    token: str = ""         # api-token or PAT
    cookie: str = ""
    auth_type: str = ""     # explicit override from env / CLI


# ---------------------------------------------------------------------------
# Config file I/O
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"config file must contain a JSON object: {path}")
    return data


def _parse_config_file(path: Path) -> _RawConfig:
    """Parse a v2 or v1-legacy config file into a _RawConfig."""
    data = _read_json(path)
    if data.get("version") == 2:
        auth = data.get("auth") or {}
        if not isinstance(auth, dict):
            raise ConfigError(f"auth must be a JSON object in {path}")
        return _RawConfig(
            site_url=_normalize_url(str(data.get("site_url", "") or "")),
            cloud_id=str(data.get("cloud_id", "") or ""),
            api_base_url=_normalize_url(str(data.get("api_base_url", "") or "")),
            email=str(auth.get("email", "") or ""),
            token=str(auth.get("token", "") or ""),
            cookie=str(auth.get("cookie_header", "") or ""),
            auth_type=str(auth.get("type", "") or ""),
        )
    # v1 legacy shape: base_url / email / api_token / api_base_url
    base_url = _normalize_url(str(data.get("base_url", "") or ""))
    return _RawConfig(
        site_url=base_url,
        api_base_url=_normalize_url(str(data.get("api_base_url", "") or "")),
        email=str(data.get("email", "") or ""),
        token=str(data.get("api_token", "") or ""),
    )


def _find_local_config(output_dir: Path) -> Path | None:
    """Walk output_dir upward, returning the first .conex/config.json found."""
    current = output_dir.expanduser().resolve()
    for candidate in (current, *current.parents):
        path = candidate / _LOCAL_CONFIG_DIR / _LOCAL_CONFIG_FILE
        if path.exists():
            return path
    return None


def _config_source_label(path: Path | None) -> str:
    if path is None:
        return "CLI/environment"
    try:
        rel = path.resolve().relative_to(Path.home())
        return f"~/{rel}"
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Merging: later calls override earlier
# ---------------------------------------------------------------------------


def _url_origin(url: str) -> tuple[str, str, int] | None:
    """Return (scheme, host, port) for *url*, or None when it has no host.

    The default port is canonicalized (https→443, http→80) so the bare and
    explicit ``:443`` forms of the same site compare equal.
    """
    url = (url or "").strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port  # raises ValueError on a malformed port
    except ValueError:
        # Malformed URL/port → unknown origin (never crash); the site_url
        # well-formedness check in resolve_config rejects it cleanly.
        return None
    if not hostname:
        return None
    if port is None:
        port = {"https": 443, "http": 80}.get(parsed.scheme, 0)
    return (parsed.scheme, hostname.lower(), port)


def _layer_with(layers: list[_RawConfig], field: str) -> _RawConfig | None:
    """The highest-precedence (last) layer with a non-empty *field*, or None."""
    for lyr in reversed(layers):
        if getattr(lyr, field):
            return lyr
    return None


def _apply_credential_origin_safety(
    merged: _RawConfig,
    layers: list[_RawConfig],
    local_layer: _RawConfig | None,
) -> _RawConfig:
    """Drop or refuse credentials a layer would redirect to a foreign origin.

    Two rules, ported from v1's connection-profile origin guard.  The active
    credential is the WINNING one for merged's effective auth type — a **cookie**
    is transported to ``site_url`` (COOKIE_V1 ignores api_base_url); a **token**
    to the resolved endpoint (``api_base_url`` else ``site_url``).  Default ports
    are canonicalized so ``:443`` == bare https.

    1. The winning credential's OWN layer declares an origin → it is bound there;
       if the resolved endpoint origin differs (a higher-priority layer
       redirected it), the credential is dropped.  The redirecting layer must
       supply its own credentials.  (Blocks the global-creds → attacker-site
       leak and the cross-layer cookie leak.)
    2. The credential's own layer declares NO origin (a portable secret, e.g. a
       token in env) → it inherits the resolved origin, UNLESS that origin comes
       from a local ``.conex/config.json`` — an untrusted, possibly directory-
       planted source.  We then refuse with an actionable error rather than send
       the secret to a host a checked-in file chose.  (A site in *global* config
       or env/CLI is trusted, so secret-in-env + site-in-global still works.)
    """
    if not (merged.token or merged.cookie):
        return merged

    eff = _infer_auth_type(
        auth_type=merged.auth_type, email=merged.email,
        token=merged.token, cookie=merged.cookie,
    )

    def _transport_origin(lyr: _RawConfig) -> tuple[str, str, int] | None:
        """The origin a credential in ``lyr`` is transported to under the active
        auth type: a cookie rides ``site_url`` (COOKIE_V1 ignores api_base_url);
        a token rides ``api_base_url`` else ``site_url``.  Default ports are
        canonicalized.  This is the unit the trust rules below reason over."""
        if eff == "cookie":
            return _url_origin(lyr.site_url)
        return _url_origin(lyr.api_base_url or lyr.site_url)

    cred_layer = _layer_with(layers, "cookie" if eff == "cookie" else "token")
    home = _transport_origin(cred_layer) if cred_layer else None
    resolved = _transport_origin(merged)

    if home is not None and home != resolved:
        return replace(merged, email="", token="", cookie="", auth_type="")

    # Scoped tokens route through the cloud-id gateway, so the tenant (cloud_id)
    # is part of the credential scope.  A cloud_id introduced by the untrusted
    # local .conex layer that differs from the credential layer's own cloud_id
    # redirects the scoped token to a different Atlassian tenant — drop it (v1
    # parity).  site_url/api_base hosts may match (both the gateway) while the
    # tenant differs, so the host check above does not catch this.
    if eff == "scoped" and merged.cloud_id:
        cred_cloud = cred_layer.cloud_id if cred_layer is not None else ""
        cloud_layer = _layer_with(layers, "cloud_id")
        if merged.cloud_id != cred_cloud and cloud_layer is local_layer:
            return replace(merged, email="", token="", cookie="", auth_type="")

    if home is not None:
        return merged

    # Portable secret (its own layer declares no origin): trusted unless the
    # resolved endpoint origin is supplied SOLELY by the untrusted local .conex
    # layer.  If a TRUSTED layer (global/env/CLI) also declares that same origin
    # the secret is honored even when a local .conex redundantly pins it — only
    # a local layer that is the sole source of the origin is refused.
    if local_layer is not None and resolved is not None:
        trusted_supplies_origin = any(
            lyr is not local_layer and _transport_origin(lyr) == resolved
            for lyr in layers
        )
        if not trusted_supplies_origin and _transport_origin(local_layer) == resolved:
            raise ConfigError(
                "credentials were supplied without a site URL, and the resolved "
                "site comes only from a local .conex/config.json — refusing to "
                "send credentials to a directory-supplied host.  Set "
                "CONFLUENCE_SITE_URL or --site-url alongside the credentials."
        )
    return merged


def _merge(base: _RawConfig, override: _RawConfig) -> _RawConfig:
    """Return a new _RawConfig with override fields winning over base (field-wise).

    Cross-origin credential safety is NOT enforced here — it is applied once in
    resolve_config against the ordered layer list (see
    :func:`_apply_credential_origin_safety`).  Doing it pairwise here is unsound:
    it can only ever drop the *lower* layer's secret, so a higher layer's secret
    still attaches to a lower (possibly attacker-controlled) layer's origin.
    """
    return _RawConfig(
        site_url=override.site_url or base.site_url,
        cloud_id=override.cloud_id or base.cloud_id,
        api_base_url=override.api_base_url or base.api_base_url,
        email=override.email or base.email,
        token=override.token or base.token,
        cookie=override.cookie or base.cookie,
        auth_type=override.auth_type or base.auth_type,
    )


# ---------------------------------------------------------------------------
# Env-var layer
# ---------------------------------------------------------------------------


def _env_raw() -> _RawConfig:
    """Read CONFLUENCE_* env vars into a _RawConfig.

    Supported variable names (exact v1 names):
      CONFLUENCE_SITE_URL, CONFLUENCE_BASE_URL (alias), CONFLUENCE_API_BASE_URL
      (explicit api_base override — stored in cloud_id slot only when not
      otherwise set; see _build_auth_headers), CONFLUENCE_CLOUD_ID,
      CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN, CONFLUENCE_PAT,
      CONFLUENCE_COOKIE, CONFLUENCE_AUTH_TYPE.
    """
    site_url = (
        os.environ.get("CONFLUENCE_SITE_URL")
        or os.environ.get("CONFLUENCE_BASE_URL")
        or ""
    )
    token = (
        os.environ.get("CONFLUENCE_API_TOKEN")
        or os.environ.get("CONFLUENCE_PAT")
        or ""
    )
    api_base_url = os.environ.get("CONFLUENCE_API_BASE_URL") or ""
    return _RawConfig(
        site_url=_normalize_url(site_url),
        cloud_id=os.environ.get("CONFLUENCE_CLOUD_ID") or "",
        api_base_url=_normalize_url(api_base_url),
        email=os.environ.get("CONFLUENCE_EMAIL") or "",
        token=token,
        cookie=os.environ.get("CONFLUENCE_COOKIE") or "",
        auth_type=os.environ.get("CONFLUENCE_AUTH_TYPE") or "",
    )


# ---------------------------------------------------------------------------
# Auth header construction
# ---------------------------------------------------------------------------


def _build_auth_headers(
    *,
    email: str,
    token: str,
    cookie: str,
    auth_type: str,
    site_url: str,
    cloud_id: str,
    api_base_url: str,
    resolve_cloud: Callable[[str], str | None],
) -> tuple[dict[str, str], Dialect, str]:
    """Return (headers, dialect, resolved_api_base_url).

    When api_base_url is set (from CONFLUENCE_API_BASE_URL) it overrides the
    derived URL for CLOUD_V2 and GATEWAY_V2 modes; cookie mode always uses
    site_url directly.

    Raises ConfigError / AuthError on missing or inconsistent credentials.
    """
    # Resolve effective auth type from explicit hint + credential shape
    eff_type = _infer_auth_type(
        auth_type=auth_type,
        email=email,
        token=token,
        cookie=cookie,
    )

    if eff_type == "cookie":
        if not cookie:
            raise AuthError(
                "cookie authentication requires a non-empty cookie header "
                "(set CONFLUENCE_COOKIE or pass --cookie)"
            )
        return (
            {"Cookie": cookie},
            Dialect.COOKIE_V1,
            site_url,
        )

    if eff_type == "scoped":
        if not email or not token:
            raise AuthError(
                "scoped API token authentication requires both email and token "
                "(set CONFLUENCE_EMAIL + CONFLUENCE_API_TOKEN)"
            )
        resolved_id = cloud_id or resolve_cloud(site_url)
        if not resolved_id:
            raise ConfigError(
                "scoped API token requires the Atlassian gateway but the cloud ID "
                "could not be resolved — set CONFLUENCE_CLOUD_ID or ensure the "
                "site URL is reachable (https://<site>.atlassian.net)"
            )
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
        derived = api_base_url or _gateway_url(resolved_id)
        return (
            {"Authorization": f"Basic {encoded}"},
            Dialect.GATEWAY_V2,
            derived,
        )

    if eff_type == "basic":
        if not email or not token:
            raise AuthError(
                "API token authentication requires both email and API token "
                "(set CONFLUENCE_EMAIL + CONFLUENCE_API_TOKEN)"
            )
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
        return (
            {"Authorization": f"Basic {encoded}"},
            Dialect.CLOUD_V2,
            api_base_url or site_url,
        )

    if eff_type == "pat":
        if not token:
            raise AuthError(
                "PAT/bearer authentication requires a token "
                "(set CONFLUENCE_PAT or CONFLUENCE_API_TOKEN)"
            )
        return (
            {"Authorization": f"Bearer {token}"},
            Dialect.CLOUD_V2,
            api_base_url or site_url,
        )

    # No credentials at all
    raise AuthError(
        "no authentication credentials found — set CONFLUENCE_EMAIL + "
        "CONFLUENCE_API_TOKEN (API token), CONFLUENCE_PAT (bearer PAT), "
        "or CONFLUENCE_COOKIE (cookie); or run `conex configure`"
    )


def _infer_auth_type(
    *,
    auth_type: str,
    email: str,
    token: str,
    cookie: str,
) -> str:
    """Return canonical auth type string: 'basic' | 'scoped' | 'pat' | 'cookie' | ''."""
    explicit = (auth_type or "").strip().lower()
    if explicit in ("basic_api_token", "basic"):
        return "basic"
    if explicit in ("scoped_api_token", "scoped"):
        return "scoped"
    if explicit in ("bearer_pat", "pat", "bearer"):
        return "pat"
    if explicit == "cookie":
        return "cookie"

    # Infer from credentials present
    if cookie:
        return "cookie"
    if email and token:
        return "scoped" if _is_scoped_token(token) else "basic"
    if token:
        return "scoped" if _is_scoped_token(token) else "pat"
    return ""


# ---------------------------------------------------------------------------
# Public API: resolve_config
# ---------------------------------------------------------------------------


def resolve_config(
    output_dir: str | Path,
    overrides: dict | None = None,
    *,
    resolve_cloud: Callable[[str], str | None] = _resolve_cloud_id,
) -> ResolvedConfig:
    """Resolve site URL, auth headers, and dialect from all config sources.

    Precedence (highest first): CLI overrides > env vars > local config
    (.conex/config.json discovered upward from output_dir) > global config
    (~/.config/confluence-export/config.json).

    Raises ConfigError for missing/invalid config, AuthError for missing creds.
    Never prompts when stdin is not a tty.
    """
    overrides = overrides or {}
    merged = _RawConfig()
    source_path: Path | None = None
    layers: list[_RawConfig] = []  # low → high precedence, for cred binding
    local_layer: _RawConfig | None = None  # the untrusted .conex/config.json layer

    # Global config (lowest priority)
    if _GLOBAL_CONFIG_PATH.exists():
        try:
            gcfg = _parse_config_file(_GLOBAL_CONFIG_PATH)
        except (json.JSONDecodeError, OSError) as exc:
            raise ConfigError(
                f"could not read global config {_GLOBAL_CONFIG_PATH}: {exc}"
            ) from exc
        merged = _merge(merged, gcfg)
        layers.append(gcfg)
        source_path = _GLOBAL_CONFIG_PATH

    # Local config
    out_dir = Path(output_dir).expanduser().resolve()
    local_path = _find_local_config(out_dir)
    if local_path is not None:
        try:
            lcfg = _parse_config_file(local_path)
        except (json.JSONDecodeError, OSError) as exc:
            raise ConfigError(
                f"could not read local config {local_path}: {exc}"
            ) from exc
        merged = _merge(merged, lcfg)
        layers.append(lcfg)
        local_layer = lcfg
        source_path = local_path

    # Env vars
    env = _env_raw()
    merged = _merge(merged, env)
    layers.append(env)
    if any((env.site_url, env.api_base_url, env.email, env.token, env.cookie, env.auth_type)):
        source_path = None  # CLI/environment label

    # CLI overrides (highest priority)
    cli_raw = _RawConfig(
        site_url=_normalize_url(str(overrides.get("site_url") or "")),
        cloud_id=str(overrides.get("cloud_id") or ""),
        api_base_url=_normalize_url(str(overrides.get("api_base_url") or "")),
        email=str(overrides.get("email") or ""),
        token=str(overrides.get("api_token") or ""),
        cookie=str(overrides.get("cookie") or ""),
        auth_type=str(overrides.get("auth_type") or ""),
    )
    merged = _merge(merged, cli_raw)
    layers.append(cli_raw)
    if any((cli_raw.site_url, cli_raw.api_base_url, cli_raw.email, cli_raw.token, cli_raw.cookie)):
        source_path = None

    # Cross-origin credential safety (trust-by-source, ported from v1): a secret
    # is dropped when a higher layer redirects it off its own declared origin,
    # and a portable secret is refused only when its endpoint origin comes from
    # an untrusted local .conex/config.json.
    merged = _apply_credential_origin_safety(merged, layers, local_layer)

    if not merged.site_url:
        raise ConfigError(
            "site_url is required — set CONFLUENCE_SITE_URL or run `conex configure`"
        )

    parsed = urlparse(merged.site_url)
    try:
        _ = parsed.port  # malformed port raises ValueError
    except ValueError:
        raise ConfigError(
            f"site_url is not a valid URL (bad port): {merged.site_url!r}"
        ) from None
    if parsed.scheme != "https":
        raise ConfigError(
            f"site_url must be an https:// URL, got: {merged.site_url!r}"
        )
    if (parsed.hostname or "").lower() == _GATEWAY_HOST:
        raise ConfigError(
            f"site_url must be the Atlassian site (https://<site>.atlassian.net), "
            f"not the OAuth gateway {merged.site_url!r} — the gateway is derived "
            "automatically for scoped tokens; set CONFLUENCE_SITE_URL to the site"
        )
    # api_base_url carries Basic/Bearer credentials for token modes, so it must
    # be https — never emit a credential to a plaintext endpoint (v1 parity).
    if merged.api_base_url:
        api_parsed = urlparse(merged.api_base_url)
        try:
            _ = api_parsed.port
        except ValueError:
            raise ConfigError(
                f"api_base_url is not a valid URL (bad port): {merged.api_base_url!r}"
            ) from None
        if api_parsed.scheme != "https":
            raise ConfigError(
                f"api_base_url must be an https:// URL, got: {merged.api_base_url!r}"
            )

    auth_headers, dialect, api_base_url = _build_auth_headers(
        email=merged.email,
        token=merged.token,
        cookie=merged.cookie,
        auth_type=merged.auth_type,
        site_url=merged.site_url,
        cloud_id=merged.cloud_id,
        api_base_url=merged.api_base_url,
        resolve_cloud=resolve_cloud,
    )

    verbose = bool(overrides.get("verbose", False))
    source_desc = _config_source_label(source_path)
    if not source_desc:
        source_desc = "CLI/environment"

    return ResolvedConfig(
        site_url=merged.site_url,
        api_base_url=api_base_url,
        auth_headers=auth_headers,
        dialect=dialect,
        email=merged.email,
        verbose=verbose,
        source_description=source_desc,
    )


def _is_interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Config file writing (0600)
# ---------------------------------------------------------------------------


def _write_config(path: Path, data: dict) -> None:
    """Atomically write data as JSON to path with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    path.chmod(0o600)


def _build_config_dict(
    *,
    site_url: str,
    email: str,
    token: str,
    cookie: str,
    cloud_id: str,
    auth_type: str,
) -> dict:
    effective_type = _infer_auth_type(
        auth_type=auth_type, email=email, token=token, cookie=cookie
    )
    type_map = {
        "basic": "basic_api_token",
        "scoped": "scoped_api_token",
        "pat": "bearer_pat",
        "cookie": "cookie",
    }
    data: dict = {
        "version": 2,
        "site_url": _normalize_url(site_url),
        "auth": {"type": type_map.get(effective_type, effective_type)},
    }
    if cloud_id:
        data["cloud_id"] = cloud_id
    if email:
        data["auth"]["email"] = email
    if token:
        data["auth"]["token"] = token
    if cookie:
        data["auth"]["cookie_header"] = cookie
    return data


# ---------------------------------------------------------------------------
# Public save helpers
# ---------------------------------------------------------------------------


def save_global_config(
    *,
    site_url: str,
    email: str = "",
    token: str = "",
    cookie: str = "",
    cloud_id: str = "",
    auth_type: str = "",
) -> Path:
    """Save credentials to the global config file with mode 0600.

    Returns the path written.
    """
    data = _build_config_dict(
        site_url=site_url,
        email=email,
        token=token,
        cookie=cookie,
        cloud_id=cloud_id,
        auth_type=auth_type,
    )
    _write_config(_GLOBAL_CONFIG_PATH, data)
    return _GLOBAL_CONFIG_PATH


def save_local_config(
    output_dir: str | Path,
    *,
    site_url: str,
    email: str = "",
    token: str = "",
    cookie: str = "",
    cloud_id: str = "",
    auth_type: str = "",
) -> Path:
    """Save credentials to .conex/config.json under output_dir with mode 0600.

    Returns the path written.
    """
    out_dir = Path(output_dir).expanduser().resolve()
    path = out_dir / _LOCAL_CONFIG_DIR / _LOCAL_CONFIG_FILE
    data = _build_config_dict(
        site_url=site_url,
        email=email,
        token=token,
        cookie=cookie,
        cloud_id=cloud_id,
        auth_type=auth_type,
    )
    _write_config(path, data)
    return path


# ---------------------------------------------------------------------------
# Interactive configure flow (called by cli.py)
# ---------------------------------------------------------------------------


def _prompt(msg: str, *, secret: bool = False) -> str:
    """Prompt interactively; use getpass for secrets."""
    import getpass

    if secret:
        return getpass.getpass(msg)
    return input(msg).strip()


def configure(
    output_dir: str | Path | None = None,
    *,
    local: bool = False,
    resolve_cloud: Callable[[str], str | None] = _resolve_cloud_id,
) -> ResolvedConfig:
    """Interactive configure flow.

    Prompts for site_url, auth mode, and credentials; saves to global config
    (or local .conex/config.json when local=True); returns a resolved config.

    Raises ConfigError when stdin is not a tty (non-interactive runs must
    not prompt).
    """
    if not _is_interactive():
        raise ConfigError(
            "configure requires an interactive terminal; "
            "set CONFLUENCE_* environment variables instead"
        )

    print("conex configure")
    print("-" * 40)
    site_url = _normalize_url(_prompt("Confluence site URL (https://yoursite.atlassian.net): "))
    if not site_url:
        raise ConfigError("site_url is required")

    print("\nAuth mode:")
    print("  1) Email + API token  (recommended for Confluence Cloud)")
    print("  2) PAT / Bearer token  (for on-prem or scoped tokens)")
    print("  3) Cookie header  (for legacy/on-prem)")
    choice = _prompt("Choice [1]: ").strip() or "1"

    email = token = cookie = cloud_id = ""
    auth_type = ""

    if choice == "1":
        email = _prompt("Email: ")
        token = _prompt("API token: ", secret=True)
        auth_type = "scoped_api_token" if _is_scoped_token(token) else "basic_api_token"
        if _is_scoped_token(token):
            print("Resolving cloud ID for scoped token gateway routing…")
            cloud_id = resolve_cloud(site_url) or ""
            if not cloud_id:
                cloud_id = _prompt("Cloud ID (not found automatically): ")
    elif choice == "2":
        token = _prompt("PAT / Bearer token: ", secret=True)
        if _is_scoped_token(token):
            # A scoped Atlassian Cloud token (ATATT…=ADA…) is NOT a site-bearer
            # PAT: it authenticates with Basic auth via the cloud-id gateway,
            # which needs an email.  Persisting it as bearer_pat sends
            # `Bearer ATATT…` to the site and 401s on every export, so route it
            # through the scoped path instead (the menu offers this choice "for
            # on-prem or scoped tokens").
            print("Detected a scoped Atlassian API token — using gateway routing.")
            email = _prompt("Email (required for scoped token): ")
            auth_type = "scoped_api_token"
            print("Resolving cloud ID for scoped token gateway routing…")
            cloud_id = resolve_cloud(site_url) or ""
            if not cloud_id:
                cloud_id = _prompt("Cloud ID (not found automatically): ")
        else:
            auth_type = "bearer_pat"
    elif choice == "3":
        cookie = _prompt("Cookie header value: ", secret=True)
        auth_type = "cookie"
    else:
        raise ConfigError(f"unknown choice {choice!r}")

    out_dir = Path(output_dir).expanduser().resolve() if output_dir else Path.cwd()
    if local:
        path = save_local_config(
            out_dir,
            site_url=site_url,
            email=email,
            token=token,
            cookie=cookie,
            cloud_id=cloud_id,
            auth_type=auth_type,
        )
        print(f"Saved local config to {path}")
    else:
        path = save_global_config(
            site_url=site_url,
            email=email,
            token=token,
            cookie=cookie,
            cloud_id=cloud_id,
            auth_type=auth_type,
        )
        print(f"Saved global config to {path}")

    return resolve_config(
        out_dir,
        resolve_cloud=resolve_cloud,
    )
