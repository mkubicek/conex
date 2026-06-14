"""Tests for conex.config — resolve_config, save_global_config, save_local_config.

Covers:
- Precedence matrix: CLI > env > local > global
- Local config discovery walking upward through parent directories
- Each auth mode -> correct auth headers + dialect
  - email + plain API token  -> CLOUD_V2 Basic
  - email + scoped token (ATATT…=ADA…) -> GATEWAY_V2 Basic + cloud-id lookup
  - PAT (no email)  -> CLOUD_V2 Bearer
  - cookie  -> COOKIE_V1 Cookie header
- Cloud-id gateway derivation (mocked /_edge/tenant_info)
- Cloud-id cached via explicit cloud_id param (no HTTP call needed)
- Secret files written with mode 0600
- Non-interactive (stdin not a tty): configure() raises ConfigError
- Missing credentials: clear actionable error message
- Missing site_url: clear actionable error message
- Non-https site_url: ConfigError
- Global and local save round-trip
- resolve_config returns ResolvedConfig with correct fields
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from conex.config import (
    Dialect,
    ResolvedConfig,
    _GLOBAL_CONFIG_PATH,
    _find_local_config,
    _infer_auth_type,
    _is_scoped_token,
    configure,
    resolve_config,
    save_global_config,
    save_local_config,
)
from conex.errors import AuthError, ConfigError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_v2_config(path: Path, **kwargs: object) -> None:
    """Write a v2 config file at path, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    auth: dict = {}
    if kwargs.get("email"):
        auth["email"] = kwargs["email"]
    if kwargs.get("token"):
        auth["token"] = kwargs["token"]
    if kwargs.get("cookie"):
        auth["cookie_header"] = kwargs["cookie"]
    if kwargs.get("auth_type"):
        auth["type"] = kwargs["auth_type"]
    data: dict = {
        "version": 2,
        "site_url": kwargs.get("site_url", "https://site.atlassian.net"),
        "auth": auth,
    }
    if kwargs.get("cloud_id"):
        data["cloud_id"] = kwargs["cloud_id"]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _basic_auth(email: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


SCOPED = "ATATT3xFfGF0abc123=ADABCtoken"  # looks like a scoped token


# ---------------------------------------------------------------------------
# _is_scoped_token
# ---------------------------------------------------------------------------


class TestIsScopedToken:
    def test_scoped_format(self) -> None:
        assert _is_scoped_token(SCOPED) is True

    def test_plain_token(self) -> None:
        assert _is_scoped_token("ABC123plaintoken") is False

    def test_empty(self) -> None:
        assert _is_scoped_token("") is False

    def test_starts_with_atatt_no_ada(self) -> None:
        assert _is_scoped_token("ATATTsomething") is False


# ---------------------------------------------------------------------------
# _infer_auth_type
# ---------------------------------------------------------------------------


class TestInferAuthType:
    def test_cookie_wins_over_token(self) -> None:
        assert _infer_auth_type(auth_type="", email="", token="t", cookie="c=1") == "cookie"

    def test_email_and_plain_token_basic(self) -> None:
        assert _infer_auth_type(auth_type="", email="a@b.com", token="plaintoken", cookie="") == "basic"

    def test_email_and_scoped_token(self) -> None:
        assert _infer_auth_type(auth_type="", email="a@b.com", token=SCOPED, cookie="") == "scoped"

    def test_token_only_pat(self) -> None:
        assert _infer_auth_type(auth_type="", email="", token="myPAT", cookie="") == "pat"

    def test_token_only_scoped_no_email(self) -> None:
        assert _infer_auth_type(auth_type="", email="", token=SCOPED, cookie="") == "scoped"

    def test_empty_returns_empty(self) -> None:
        assert _infer_auth_type(auth_type="", email="", token="", cookie="") == ""

    def test_explicit_cookie_type(self) -> None:
        assert _infer_auth_type(auth_type="cookie", email="e", token="t", cookie="") == "cookie"

    def test_explicit_basic_type(self) -> None:
        assert _infer_auth_type(auth_type="basic_api_token", email="e", token="t", cookie="") == "basic"

    def test_explicit_bearer_pat(self) -> None:
        assert _infer_auth_type(auth_type="bearer_pat", email="", token="t", cookie="") == "pat"


# ---------------------------------------------------------------------------
# Auth modes -> headers + dialect
# ---------------------------------------------------------------------------


class TestResolveConfigAuthModes:
    def test_basic_auth_mode(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="user@example.com",
            token="plaintoken",
        )
        cfg = resolve_config(tmp_path)
        expected = _basic_auth("user@example.com", "plaintoken")
        assert cfg.auth_headers == {"Authorization": expected}
        assert cfg.dialect is Dialect.CLOUD_V2
        assert cfg.site_url == "https://site.atlassian.net"
        assert cfg.api_base_url == "https://site.atlassian.net"

    def test_pat_bearer_mode(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            token="myPATtoken",
        )
        cfg = resolve_config(tmp_path)
        assert cfg.auth_headers == {"Authorization": "Bearer myPATtoken"}
        assert cfg.dialect is Dialect.CLOUD_V2

    def test_cookie_mode(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            cookie="session=abc123; other=xyz",
        )
        cfg = resolve_config(tmp_path)
        assert cfg.auth_headers == {"Cookie": "session=abc123; other=xyz"}
        assert cfg.dialect is Dialect.COOKIE_V1

    def test_scoped_token_gateway(self, tmp_path: Path) -> None:
        """Scoped token -> GATEWAY_V2 with cloud-id resolved via mock."""

        def fake_resolve(url: str) -> str | None:
            return "cloud-abc-123"

        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="user@example.com",
            token=SCOPED,
        )
        cfg = resolve_config(tmp_path, resolve_cloud=fake_resolve)
        assert cfg.dialect is Dialect.GATEWAY_V2
        assert "api.atlassian.com/ex/confluence/cloud-abc-123" in cfg.api_base_url
        expected = _basic_auth("user@example.com", SCOPED)
        assert cfg.auth_headers == {"Authorization": expected}

    def test_scoped_token_cached_cloud_id(self, tmp_path: Path) -> None:
        """Explicit cloud_id in config skips the HTTP lookup."""
        called = []

        def fake_resolve(url: str) -> str | None:
            called.append(url)
            return "should-not-be-called"

        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="user@example.com",
            token=SCOPED,
            cloud_id="existing-cloud-id",
        )
        cfg = resolve_config(tmp_path, resolve_cloud=fake_resolve)
        assert cfg.dialect is Dialect.GATEWAY_V2
        assert "existing-cloud-id" in cfg.api_base_url
        assert called == []  # no HTTP call made


# ---------------------------------------------------------------------------
# Precedence matrix
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_cli_overrides_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://env-site.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "env-token")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "env@example.com")

        cfg = resolve_config(
            tmp_path,
            overrides={
                "site_url": "https://cli-site.atlassian.net",
                "email": "cli@example.com",
                "api_token": "cli-token",
            },
        )
        assert cfg.site_url == "https://cli-site.atlassian.net"
        assert cfg.auth_headers == {"Authorization": _basic_auth("cli@example.com", "cli-token")}

    def test_env_overrides_local(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://local-site.atlassian.net",
            email="local@example.com",
            token="local-token",
        )
        monkeypatch.setenv("CONFLUENCE_EMAIL", "env@example.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "env-token")
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://env-site.atlassian.net")

        cfg = resolve_config(tmp_path)
        assert cfg.site_url == "https://env-site.atlassian.net"
        assert cfg.auth_headers == {"Authorization": _basic_auth("env@example.com", "env-token")}

    def test_local_overrides_global(self, tmp_path: Path) -> None:
        global_cfg = tmp_path / "global_config.json"
        local_dir = tmp_path / "project"
        local_dir.mkdir()

        global_cfg.write_text(
            json.dumps({
                "version": 2,
                "site_url": "https://global-site.atlassian.net",
                "auth": {"email": "global@example.com", "token": "global-token"},
            })
        )

        _write_v2_config(
            local_dir / ".conex" / "config.json",
            site_url="https://local-site.atlassian.net",
            email="local@example.com",
            token="local-token",
        )

        with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
            cfg = resolve_config(local_dir)

        assert cfg.site_url == "https://local-site.atlassian.net"
        assert cfg.auth_headers == {"Authorization": _basic_auth("local@example.com", "local-token")}

    def test_global_used_when_no_local(self, tmp_path: Path) -> None:
        global_cfg = tmp_path / "global_config.json"
        global_cfg.write_text(
            json.dumps({
                "version": 2,
                "site_url": "https://global-site.atlassian.net",
                "auth": {"email": "global@example.com", "token": "global-token"},
            })
        )

        project = tmp_path / "project"
        project.mkdir()

        with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
            cfg = resolve_config(project)

        assert cfg.site_url == "https://global-site.atlassian.net"

    def test_cli_overrides_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://env.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "env@example.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "env-token")

        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://local.atlassian.net",
            email="local@example.com",
            token="local-token",
        )

        cfg = resolve_config(
            tmp_path,
            overrides={
                "site_url": "https://cli.atlassian.net",
                "email": "cli@example.com",
                "api_token": "cli-token",
            },
        )
        assert cfg.site_url == "https://cli.atlassian.net"
        assert cfg.auth_headers == {"Authorization": _basic_auth("cli@example.com", "cli-token")}


# ---------------------------------------------------------------------------
# Local config discovery: upward walk
# ---------------------------------------------------------------------------


class TestLocalConfigDiscovery:
    def test_finds_config_in_output_dir(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://found.atlassian.net",
            email="u@example.com",
            token="tok",
        )
        cfg = resolve_config(tmp_path)
        assert cfg.site_url == "https://found.atlassian.net"

    def test_finds_config_in_parent(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://parent.atlassian.net",
            email="u@example.com",
            token="tok",
        )
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        cfg = resolve_config(deep)
        assert cfg.site_url == "https://parent.atlassian.net"

    def test_finds_nearest_ancestor(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://outer.atlassian.net",
            email="outer@example.com",
            token="outer-token",
        )
        inner = tmp_path / "sub"
        inner.mkdir()
        _write_v2_config(
            inner / ".conex" / "config.json",
            site_url="https://inner.atlassian.net",
            email="inner@example.com",
            token="inner-token",
        )
        cfg = resolve_config(inner)
        assert cfg.site_url == "https://inner.atlassian.net"

    def test_no_config_returns_error_without_env(self, tmp_path: Path) -> None:
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            with pytest.raises((ConfigError, AuthError)):
                resolve_config(tmp_path)

    def test_find_local_config_returns_none_when_absent(self, tmp_path: Path) -> None:
        result = _find_local_config(tmp_path)
        assert result is None

    def test_find_local_config_returns_path(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".conex" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{}")
        result = _find_local_config(tmp_path)
        assert result == config_path


# ---------------------------------------------------------------------------
# Error quality
# ---------------------------------------------------------------------------


class TestErrorMessages:
    def test_missing_site_url_error(self, tmp_path: Path) -> None:
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            with pytest.raises((ConfigError, AuthError)) as exc_info:
                resolve_config(tmp_path)
        assert "site_url" in str(exc_info.value).lower() or "credential" in str(exc_info.value).lower()

    def test_missing_credentials_error(self, tmp_path: Path) -> None:
        config_path = tmp_path / "cfg.json"
        config_path.write_text(
            json.dumps({"version": 2, "site_url": "https://site.atlassian.net", "auth": {}})
        )
        with patch("conex.config._GLOBAL_CONFIG_PATH", config_path):
            with pytest.raises(AuthError) as exc_info:
                resolve_config(tmp_path)
        msg = str(exc_info.value).lower()
        assert "credential" in msg or "token" in msg or "authentication" in msg

    def test_non_https_url_error(self, tmp_path: Path) -> None:
        config_path = tmp_path / "cfg.json"
        config_path.write_text(
            json.dumps({
                "version": 2,
                "site_url": "http://not-https.atlassian.net",
                "auth": {"email": "u@e.com", "token": "tok"},
            })
        )
        with patch("conex.config._GLOBAL_CONFIG_PATH", config_path):
            with pytest.raises(ConfigError) as exc_info:
                resolve_config(tmp_path)
        assert "https" in str(exc_info.value).lower()

    def test_scoped_token_no_cloud_id_error(self, tmp_path: Path) -> None:
        def no_cloud(url: str) -> None:
            return None

        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="user@example.com",
            token=SCOPED,
        )
        with pytest.raises(ConfigError) as exc_info:
            resolve_config(tmp_path, resolve_cloud=no_cloud)
        msg = str(exc_info.value).lower()
        assert "cloud id" in msg or "cloud_id" in msg or "gateway" in msg

    def test_cookie_mode_empty_cookie_error(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            auth_type="cookie",
        )
        with pytest.raises(AuthError) as exc_info:
            resolve_config(tmp_path)
        assert "cookie" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# File permissions (0600)
# ---------------------------------------------------------------------------


class TestFilePermissions:
    def test_save_global_config_mode_0600(self, tmp_path: Path) -> None:
        global_path = tmp_path / "global_config.json"
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_path):
            save_global_config(
                site_url="https://site.atlassian.net",
                email="u@e.com",
                token="mytoken",
            )
        mode = stat.S_IMODE(global_path.stat().st_mode)
        assert mode == 0o600

    def test_save_local_config_mode_0600(self, tmp_path: Path) -> None:
        path = save_local_config(
            tmp_path,
            site_url="https://site.atlassian.net",
            email="u@e.com",
            token="mytoken",
        )
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Save and round-trip
# ---------------------------------------------------------------------------


class TestSaveAndLoad:
    def test_save_global_creates_file(self, tmp_path: Path) -> None:
        global_path = tmp_path / "config.json"
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_path):
            result = save_global_config(
                site_url="https://site.atlassian.net",
                email="u@e.com",
                token="tok123",
            )
        assert result == global_path
        assert global_path.exists()
        data = json.loads(global_path.read_text())
        assert data["version"] == 2
        assert data["site_url"] == "https://site.atlassian.net"
        assert data["auth"]["email"] == "u@e.com"
        assert data["auth"]["token"] == "tok123"

    def test_save_local_creates_file(self, tmp_path: Path) -> None:
        result = save_local_config(
            tmp_path,
            site_url="https://site.atlassian.net",
            email="u@e.com",
            token="tok",
        )
        assert result == tmp_path / ".conex" / "config.json"
        assert result.exists()

    def test_global_config_round_trip(self, tmp_path: Path) -> None:
        global_path = tmp_path / "config.json"
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_path):
            save_global_config(
                site_url="https://site.atlassian.net",
                email="u@e.com",
                token="tok",
            )
            cfg = resolve_config(tmp_path)

        assert cfg.site_url == "https://site.atlassian.net"
        assert cfg.email == "u@e.com"
        assert cfg.dialect is Dialect.CLOUD_V2

    def test_local_config_round_trip(self, tmp_path: Path) -> None:
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            save_local_config(
                tmp_path,
                site_url="https://site.atlassian.net",
                email="u@e.com",
                token="tok",
            )
            cfg = resolve_config(tmp_path)

        assert cfg.site_url == "https://site.atlassian.net"
        assert cfg.dialect is Dialect.CLOUD_V2

    def test_cookie_save_and_load(self, tmp_path: Path) -> None:
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            save_local_config(
                tmp_path,
                site_url="https://site.atlassian.net",
                cookie="session=abc; id=xyz",
            )
            cfg = resolve_config(tmp_path)

        assert cfg.dialect is Dialect.COOKIE_V1
        assert cfg.auth_headers == {"Cookie": "session=abc; id=xyz"}

    def test_scoped_token_save_with_cloud_id(self, tmp_path: Path) -> None:
        """cloud_id saved in file avoids HTTP lookup on resolve."""
        called = []

        def fake_resolve(url: str) -> str | None:
            called.append(url)
            return "should-not-reach"

        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            save_local_config(
                tmp_path,
                site_url="https://site.atlassian.net",
                email="u@e.com",
                token=SCOPED,
                cloud_id="saved-cloud-id",
            )
            cfg = resolve_config(tmp_path, resolve_cloud=fake_resolve)

        assert cfg.dialect is Dialect.GATEWAY_V2
        assert "saved-cloud-id" in cfg.api_base_url
        assert called == []


# ---------------------------------------------------------------------------
# Non-interactive guard
# ---------------------------------------------------------------------------


class TestNonInteractiveGuard:
    def test_configure_raises_when_not_tty(self) -> None:
        with patch("conex.config._is_interactive", return_value=False):
            with pytest.raises(ConfigError) as exc_info:
                configure()
        msg = str(exc_info.value).lower()
        assert "interactive" in msg or "terminal" in msg or "tty" in msg


# ---------------------------------------------------------------------------
# ResolvedConfig shape
# ---------------------------------------------------------------------------


class TestResolvedConfigShape:
    def test_source_description_populated(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="u@e.com",
            token="tok",
        )
        cfg = resolve_config(tmp_path)
        assert cfg.source_description  # non-empty

    def test_verbose_false_by_default(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="u@e.com",
            token="tok",
        )
        cfg = resolve_config(tmp_path)
        assert cfg.verbose is False

    def test_verbose_from_overrides(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="u@e.com",
            token="tok",
        )
        cfg = resolve_config(tmp_path, {"verbose": True})
        assert cfg.verbose is True

    def test_email_in_resolved_config(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="me@example.com",
            token="tok",
        )
        cfg = resolve_config(tmp_path)
        assert cfg.email == "me@example.com"

    def test_frozen_dataclass(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="u@e.com",
            token="tok",
        )
        cfg = resolve_config(tmp_path)
        with pytest.raises(Exception):
            cfg.site_url = "https://other.atlassian.net"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Legacy v1 config format reading
# ---------------------------------------------------------------------------


class TestLegacyV1ConfigFormat:
    def test_v1_config_base_url_email_token(self, tmp_path: Path) -> None:
        config_path = tmp_path / "cfg.json"
        config_path.write_text(
            json.dumps({
                "base_url": "https://site.atlassian.net",
                "email": "v1@example.com",
                "api_token": "v1token",
            })
        )
        with patch("conex.config._GLOBAL_CONFIG_PATH", config_path):
            cfg = resolve_config(tmp_path)

        assert cfg.site_url == "https://site.atlassian.net"
        assert cfg.auth_headers == {"Authorization": _basic_auth("v1@example.com", "v1token")}
        assert cfg.dialect is Dialect.CLOUD_V2


# ---------------------------------------------------------------------------
# Env vars: CONFLUENCE_BASE_URL alias and PAT alias
# ---------------------------------------------------------------------------


class TestEnvVarAliases:
    def test_confluence_base_url_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://alias.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "e@e.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.site_url == "https://alias.atlassian.net"

    def test_confluence_pat_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://site.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_PAT", "mypattoken")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.dialect is Dialect.CLOUD_V2
        assert cfg.auth_headers == {"Authorization": "Bearer mypattoken"}

    def test_all_env_var_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify all CONFLUENCE_* env vars are consumed."""
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://site.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "u@e.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
        monkeypatch.setenv("CONFLUENCE_CLOUD_ID", "cid-from-env")
        # Cookie overrides token+email (for this test just checking it's not crashed)
        monkeypatch.delenv("CONFLUENCE_COOKIE", raising=False)
        monkeypatch.delenv("CONFLUENCE_AUTH_TYPE", raising=False)
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.site_url == "https://site.atlassian.net"


# ---------------------------------------------------------------------------
# Cloud-id lookup mock
# ---------------------------------------------------------------------------


class TestCloudIdLookup:
    def test_resolve_cloud_id_called_for_scoped(self, tmp_path: Path) -> None:
        calls = []

        def tracking_resolve(url: str) -> str | None:
            calls.append(url)
            return "resolved-cloud-123"

        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="u@e.com",
            token=SCOPED,
        )
        cfg = resolve_config(tmp_path, resolve_cloud=tracking_resolve)
        assert calls == ["https://site.atlassian.net"]
        assert "resolved-cloud-123" in cfg.api_base_url

    def test_resolve_cloud_id_not_called_for_basic(self, tmp_path: Path) -> None:
        calls = []

        def tracking_resolve(url: str) -> str | None:
            calls.append(url)
            return "some-cloud"

        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="u@e.com",
            token="plaintoken",
        )
        resolve_config(tmp_path, resolve_cloud=tracking_resolve)
        assert calls == []

    def test_resolve_cloud_id_not_called_for_pat(self, tmp_path: Path) -> None:
        calls = []

        def tracking_resolve(url: str) -> str | None:
            calls.append(url)
            return "some-cloud"

        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            token="myPAT",
        )
        resolve_config(tmp_path, resolve_cloud=tracking_resolve)
        assert calls == []

    def test_resolve_cloud_id_not_called_for_cookie(self, tmp_path: Path) -> None:
        calls = []

        def tracking_resolve(url: str) -> str | None:
            calls.append(url)
            return "some-cloud"

        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            cookie="s=abc",
        )
        resolve_config(tmp_path, resolve_cloud=tracking_resolve)
        assert calls == []


# ---------------------------------------------------------------------------
# Env var CONFLUENCE_AUTH_TYPE explicit override
# ---------------------------------------------------------------------------


class TestAuthTypeOverride:
    def test_auth_type_env_cookie_forces_cookie_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://site.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_COOKIE", "sess=abc")
        monkeypatch.setenv("CONFLUENCE_AUTH_TYPE", "cookie")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.dialect is Dialect.COOKIE_V1
        assert cfg.auth_headers == {"Cookie": "sess=abc"}

    def test_auth_type_bearer_pat_overrides_infer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://site.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_PAT", "pattoken")
        monkeypatch.setenv("CONFLUENCE_AUTH_TYPE", "bearer_pat")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.dialect is Dialect.CLOUD_V2
        assert cfg.auth_headers == {"Authorization": "Bearer pattoken"}


# ---------------------------------------------------------------------------
# Non-dict auth field robustness (MAJOR fix: v1 isinstance guard port)
# ---------------------------------------------------------------------------


class TestNonDictAuthField:
    """A config file whose 'auth' key holds a non-dict value must raise
    ConfigError, NOT AttributeError.  Port of v1's isinstance guard."""

    def _write_bad_auth(self, path: Path, auth_value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 2, "site_url": "https://site.atlassian.net", "auth": auth_value}
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_auth_string_raises_config_error_local(self, tmp_path: Path) -> None:
        self._write_bad_auth(tmp_path / ".conex" / "config.json", "not-a-dict")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            with pytest.raises(ConfigError) as exc_info:
                resolve_config(tmp_path)
        assert "auth" in str(exc_info.value).lower()

    def test_auth_list_raises_config_error_local(self, tmp_path: Path) -> None:
        self._write_bad_auth(tmp_path / ".conex" / "config.json", ["email", "token"])
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            with pytest.raises(ConfigError):
                resolve_config(tmp_path)

    def test_auth_number_raises_config_error_local(self, tmp_path: Path) -> None:
        self._write_bad_auth(tmp_path / ".conex" / "config.json", 42)
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            with pytest.raises(ConfigError):
                resolve_config(tmp_path)

    def test_auth_string_raises_config_error_global(self, tmp_path: Path) -> None:
        bad_global = tmp_path / "bad_global.json"
        self._write_bad_auth(bad_global, "string-not-dict")
        with patch("conex.config._GLOBAL_CONFIG_PATH", bad_global):
            with pytest.raises(ConfigError) as exc_info:
                resolve_config(tmp_path)
        assert "auth" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# CONFLUENCE_API_BASE_URL env var (MAJOR fix: spec-named env var now read)
# ---------------------------------------------------------------------------


class TestApiBaseUrlEnvVar:
    """CONFLUENCE_API_BASE_URL must be consumed and honored as the api_base_url
    override for non-cookie auth modes."""

    def test_api_base_url_env_overrides_derived_basic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://site.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "u@e.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
        monkeypatch.setenv("CONFLUENCE_API_BASE_URL", "https://proxy.example.com/confluence")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.api_base_url == "https://proxy.example.com/confluence"
        assert cfg.site_url == "https://site.atlassian.net"  # user-facing URL unchanged
        assert cfg.dialect is Dialect.CLOUD_V2

    def test_api_base_url_env_overrides_derived_pat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://site.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_PAT", "mypattoken")
        monkeypatch.setenv("CONFLUENCE_API_BASE_URL", "https://proxy.example.com")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.api_base_url == "https://proxy.example.com"

    def test_api_base_url_env_overrides_gateway_url_for_scoped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://site.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "u@e.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", SCOPED)
        monkeypatch.setenv("CONFLUENCE_API_BASE_URL", "https://my-gateway.example.com")
        monkeypatch.setenv("CONFLUENCE_CLOUD_ID", "cloud-xyz")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.api_base_url == "https://my-gateway.example.com"
        assert cfg.dialect is Dialect.GATEWAY_V2

    def test_api_base_url_env_absent_uses_site_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://site.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "u@e.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
        monkeypatch.delenv("CONFLUENCE_API_BASE_URL", raising=False)
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.api_base_url == "https://site.atlassian.net"

    def test_cookie_mode_ignores_api_base_url_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cookie mode always returns site_url as api_base_url regardless."""
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://site.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_COOKIE", "sess=abc")
        monkeypatch.setenv("CONFLUENCE_API_BASE_URL", "https://should-be-ignored.example.com")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.dialect is Dialect.COOKIE_V1
        assert cfg.api_base_url == "https://site.atlassian.net"


class TestCrossOriginCredentialSafety:
    """A credential must never be sent to an origin a *different* config layer
    introduced.  Regression for the cross-origin credential-leak P0.
    """

    def test_global_creds_not_leaked_to_local_different_origin(self, tmp_path: Path) -> None:
        # Global config carries creds scoped to the victim site.
        global_cfg = tmp_path / "global_config.json"
        global_cfg.write_text(json.dumps({
            "version": 2,
            "site_url": "https://victim.atlassian.net",
            "auth": {"email": "victim@example.com", "token": "victim-token"},
        }))
        # Higher-priority local config points at a DIFFERENT origin, no creds.
        project = tmp_path / "project"
        project.mkdir()
        _write_v2_config(
            project / ".conex" / "config.json",
            site_url="https://attacker.example.com",
        )
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
            with pytest.raises(AuthError):
                resolve_config(project)

    def test_env_api_base_different_origin_drops_inherited_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Local config has token creds for the site...
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://victim.atlassian.net",
            email="victim@example.com",
            token="victim-token",
        )
        # ...and a stray env api_base points the endpoint at another host.
        monkeypatch.setenv("CONFLUENCE_API_BASE_URL", "https://evil.example.com")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            with pytest.raises(AuthError):
                resolve_config(tmp_path)

    def test_same_origin_local_override_keeps_global_creds(self, tmp_path: Path) -> None:
        global_cfg = tmp_path / "global_config.json"
        global_cfg.write_text(json.dumps({
            "version": 2,
            "site_url": "https://site.atlassian.net",
            "auth": {"email": "g@example.com", "token": "g-token"},
        }))
        project = tmp_path / "project"
        project.mkdir()
        # Same origin, just present without creds → inherited creds still apply.
        _write_v2_config(
            project / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
        )
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
            cfg = resolve_config(project)
        assert cfg.auth_headers == {"Authorization": _basic_auth("g@example.com", "g-token")}

    def test_env_creds_only_not_leaked_to_attacker_local_site(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BYPASS regression: higher-priority env creds (no site) must NOT attach
        to a lower-priority local config's attacker-controlled site_url."""
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://attacker.example.com",
        )
        monkeypatch.setenv("CONFLUENCE_EMAIL", "victim@example.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "secret")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            # Refused with an actionable error: a portable secret must not be
            # sent to a site supplied by an untrusted local .conex config.
            with pytest.raises(ConfigError):
                resolve_config(tmp_path)

    def test_global_creds_only_not_leaked_to_attacker_local_site(self, tmp_path: Path) -> None:
        """BYPASS regression: URL-less global creds must not attach to an
        attacker-controlled local site_url."""
        global_cfg = tmp_path / "global_config.json"
        global_cfg.write_text(json.dumps({
            "version": 2,
            "auth": {"email": "victim@example.com", "token": "secret"},
        }))
        project = tmp_path / "project"
        project.mkdir()
        _write_v2_config(
            project / ".conex" / "config.json",
            site_url="https://attacker.example.com",
        )
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
            with pytest.raises(ConfigError):
                resolve_config(project)

    def test_cookie_with_token_and_api_base_not_leaked_cross_origin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BYPASS regression: a cookie is transported to site_url even when a
        token + api_base coexist (COOKIE_V1 ignores api_base).  The binding must
        follow effective auth (cookie→site), so a lower attacker site_url drops
        the cookie rather than leaking it to api_base's apparent origin."""
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://attacker.example.com",
        )
        monkeypatch.setenv("CONFLUENCE_COOKIE", "victim-sess=secret")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "plainjunk")
        monkeypatch.setenv("CONFLUENCE_API_BASE_URL", "https://victim.atlassian.net")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            with pytest.raises(ConfigError):
                resolve_config(tmp_path)

    def test_global_and_local_same_site_with_env_creds_works(self, tmp_path, monkeypatch):
        """Over-refusal regression: when a TRUSTED layer (global) declares the
        same site a local .conex redundantly pins, an env secret must still be
        honored — refuse only when the local .conex is the SOLE origin source."""
        global_cfg = tmp_path / "global_config.json"
        global_cfg.write_text(json.dumps({
            "version": 2,
            "site_url": "https://trusted.atlassian.net",
        }))
        project = tmp_path / "project"
        project.mkdir()
        _write_v2_config(
            project / ".conex" / "config.json",
            site_url="https://trusted.atlassian.net",
        )
        monkeypatch.setenv("CONFLUENCE_EMAIL", "u@e.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
            cfg = resolve_config(project)
        assert cfg.auth_headers == {"Authorization": _basic_auth("u@e.com", "tok")}

    def test_secret_in_env_with_site_in_global_config_works(self, tmp_path, monkeypatch):
        """Trusted split: secret in env + site in GLOBAL config (not local .conex)
        is the standard CI/CD pattern and MUST yield working credentials."""
        global_cfg = tmp_path / "global_config.json"
        global_cfg.write_text(json.dumps({
            "version": 2,
            "site_url": "https://site.atlassian.net",
        }))
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("CONFLUENCE_EMAIL", "u@e.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
            cfg = resolve_config(project)
        assert cfg.auth_headers == {"Authorization": _basic_auth("u@e.com", "tok")}
        assert cfg.site_url == "https://site.atlassian.net"

    def test_cloud_id_from_local_config_does_not_redirect_scoped_token(self, tmp_path):
        """A planted local .conex cloud_id must not redirect a scoped token's
        gateway to a different (attacker) Atlassian tenant while keeping the
        victim's credentials."""
        global_cfg = tmp_path / "global_config.json"
        global_cfg.write_text(json.dumps({
            "version": 2,
            "site_url": "https://victim.atlassian.net",
            "auth": {"email": "victim@corp.com", "token": SCOPED},
        }))
        project = tmp_path / "project"
        project.mkdir()
        _write_v2_config(
            project / ".conex" / "config.json",
            site_url="https://victim.atlassian.net",
            cloud_id="ATTACKER-TENANT-9999",
        )
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
            with pytest.raises((AuthError, ConfigError)):
                resolve_config(project, resolve_cloud=lambda u: "victim-cloud")

    def test_cross_layer_cookie_leak_blocked(self, tmp_path, monkeypatch):
        """A victim cookie in global config must not be exfiltrated to an
        attacker site set in env, even when a junk token sits in env."""
        global_cfg = tmp_path / "global_config.json"
        global_cfg.write_text(json.dumps({
            "version": 2,
            "site_url": "https://victim.atlassian.net",
            "auth": {"cookie_header": "VICTIM-SESSION=topsecret"},
        }))
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://attacker.evil.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "junk")
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
            with pytest.raises((AuthError, ConfigError)):
                resolve_config(project)

    def test_explicit_default_port_is_same_origin(self, tmp_path: Path) -> None:
        """Regression: site.atlassian.net and site.atlassian.net:443 are the same
        origin — an explicit :443 in one layer must NOT drop inherited creds."""
        global_cfg = tmp_path / "global_config.json"
        global_cfg.write_text(json.dumps({
            "version": 2,
            "site_url": "https://site.atlassian.net",
            "auth": {"email": "u@e.com", "token": "tok"},
        }))
        project = tmp_path / "project"
        project.mkdir()
        _write_v2_config(
            project / ".conex" / "config.json",
            site_url="https://site.atlassian.net:443",
        )
        with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
            cfg = resolve_config(project)
        assert cfg.auth_headers == {"Authorization": _basic_auth("u@e.com", "tok")}

    def test_cookie_not_dropped_by_unrelated_env_api_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cookie auth in a local config; api_base is irrelevant to cookie mode
        # and must NOT drop the cookie even on a different origin.
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            cookie="sess=abc",
            auth_type="cookie",
        )
        monkeypatch.setenv("CONFLUENCE_API_BASE_URL", "https://unrelated.example.com")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.dialect is Dialect.COOKIE_V1
        assert cfg.auth_headers == {"Cookie": "sess=abc"}


class TestApiBaseUrlCliFlag:
    """The --api-base-url CLI flag must be honored (was silently ignored)."""

    def test_cli_api_base_url_honored_with_colocated_creds(self, tmp_path: Path) -> None:
        # site + api_base + creds all supplied together (CLI layer) → intentional.
        cfg = resolve_config(tmp_path, overrides={
            "site_url": "https://site.atlassian.net",
            "api_base_url": "https://proxy.internal",
            "email": "e@example.com",
            "api_token": "tok",
        })
        assert cfg.api_base_url == "https://proxy.internal"
        assert cfg.auth_headers == {"Authorization": _basic_auth("e@example.com", "tok")}

    def test_cli_api_base_url_same_origin_keeps_config_creds(self, tmp_path: Path) -> None:
        _write_v2_config(
            tmp_path / ".conex" / "config.json",
            site_url="https://site.atlassian.net",
            email="e@example.com",
            token="tok",
        )
        cfg = resolve_config(
            tmp_path, overrides={"api_base_url": "https://site.atlassian.net/wiki"}
        )
        assert cfg.api_base_url == "https://site.atlassian.net/wiki"
        assert cfg.auth_headers == {"Authorization": _basic_auth("e@example.com", "tok")}


class TestApiBaseUrlFromConfigFile:
    def test_api_base_url_in_config_file_honored(self, tmp_path: Path) -> None:
        """A config-file api_base_url (v1 read it) must be honored, not dropped —
        proxy/gateway users rely on it.  Co-located with creds, so the cross-origin
        guard treats it as an intentional same-layer endpoint."""
        cfg_path = tmp_path / ".conex" / "config.json"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(json.dumps({
            "version": 2,
            "site_url": "https://site.atlassian.net",
            "api_base_url": "https://proxy.internal/wiki",
            "auth": {"email": "e@example.com", "token": "tok"},
        }))
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            cfg = resolve_config(tmp_path)
        assert cfg.api_base_url == "https://proxy.internal/wiki"
        assert cfg.auth_headers == {"Authorization": _basic_auth("e@example.com", "tok")}


class TestApiBaseUrlHttpsRequired:
    def test_http_api_base_url_rejected(self, tmp_path: Path) -> None:
        """api_base_url carries Basic/Bearer creds, so a plaintext http:// value
        must be rejected (never emit a credential to an unencrypted endpoint)."""
        cfg_path = tmp_path / ".conex" / "config.json"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(json.dumps({
            "version": 2,
            "site_url": "https://site.atlassian.net",
            "api_base_url": "http://proxy.internal/wiki",
            "auth": {"email": "e@example.com", "token": "tok"},
        }))
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            with pytest.raises(ConfigError):
                resolve_config(tmp_path)


class TestMalformedUrl:
    def test_malformed_port_site_url_raises_configerror_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo'd port in site_url must raise a clean ConfigError, never an
        unhandled ValueError traceback (the origin check must not crash)."""
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://acme.atlassian.net:8O80")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "u@e.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            with pytest.raises(ConfigError):
                resolve_config(tmp_path)


class TestGatewayUrlRejectedAsSiteUrl:
    def test_gateway_url_as_site_url_rejected(self, tmp_path: Path) -> None:
        with patch("conex.config._GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.json"):
            with pytest.raises(ConfigError, match="gateway"):
                resolve_config(tmp_path, overrides={
                    "site_url": "https://api.atlassian.com/ex/confluence/abc",
                    "cookie": "sess=abc",
                })


def test_scoped_token_api_base_tenant_redirect_blocked(tmp_path):
    """A planted local .conex api_base_url must not redirect a victim scoped
    token to a DIFFERENT tenant on the same Atlassian gateway host. The host is
    identical (api.atlassian.com) and the cloud_id field is unchanged, so only a
    tenant-PATH-aware guard catches this cross-tenant credential leak."""
    global_cfg = tmp_path / "global_config.json"
    global_cfg.write_text(json.dumps({
        "version": 2,
        "site_url": "https://victim.atlassian.net",
        "cloud_id": "VICTIM-CLOUD",
        "api_base_url": "https://api.atlassian.com/ex/confluence/VICTIM-CLOUD",
        "auth": {"email": "victim@corp.com", "token": SCOPED},
    }))
    project = tmp_path / "project"
    (project / ".conex").mkdir(parents=True)
    (project / ".conex" / "config.json").write_text(json.dumps({
        "version": 2,
        "api_base_url": "https://api.atlassian.com/ex/confluence/ATTACKER-CLOUD",
        "auth": {},
    }))
    with patch("conex.config._GLOBAL_CONFIG_PATH", global_cfg):
        with pytest.raises((AuthError, ConfigError)):
            resolve_config(project, resolve_cloud=lambda u: "VICTIM-CLOUD")
