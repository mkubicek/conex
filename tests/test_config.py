"""Tests for config loading, saving, and validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from confluence_export.config import (
    ApiDialect,
    AuthConfig,
    AuthMode,
    Config,
    ConnectionProfileError,
    find_local_config,
    gateway_url,
    is_atlassian_site_url,
    is_gateway_url,
    is_scoped_token,
    load_connection_profile,
    load_config,
    resolve_cloud_id,
    save_config,
    save_connection_config,
)

# Every CONFLUENCE_* env var read by load_connection_profile (config.py _env_config).
_CONFLUENCE_ENV_VARS = (
    "CONFLUENCE_BASE_URL",
    "CONFLUENCE_SITE_URL",
    "CONFLUENCE_API_BASE_URL",
    "CONFLUENCE_CLOUD_ID",
    "CONFLUENCE_EMAIL",
    "CONFLUENCE_API_TOKEN",
    "CONFLUENCE_PAT",
    "CONFLUENCE_COOKIE",
    "CONFLUENCE_AUTH_TYPE",
)


@pytest.fixture(autouse=True)
def _hermetic_config(tmp_path_factory, monkeypatch):
    """Make config resolution hermetic by default (issue #26).

    ``load_connection_profile`` overlays global config file → local config → env →
    CLI args, so without isolation a test inherits the developer machine's real
    ``~/.config/confluence-export/config.json`` and any ``CONFLUENCE_*`` env vars —
    e.g. an inherited email flips ``test_scoped_token_without_email_is_not_bearer``
    from raising to ``BASIC_API_TOKEN``, making the suite pass/fail on machine state.

    Point the global config at a non-existent temp path and clear the env vars. A
    test that wants either sets its own ``monkeypatch.setattr(config_path, ...)`` /
    ``monkeypatch.setenv(...)``, which runs after this fixture and so wins.
    """
    empty_global = tmp_path_factory.mktemp("no-global-config") / "config.json"
    monkeypatch.setattr("confluence_export.config.config_path", lambda: empty_global)
    for var in _CONFLUENCE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestConfig:
    def test_needs_token_true(self):
        cfg = Config(base_url="https://x.atlassian.net", email="", api_token="")
        assert cfg.needs_token is True

    def test_needs_token_false(self):
        cfg = Config(base_url="https://x.atlassian.net", email="", api_token="tok")
        assert cfg.needs_token is False

    def test_use_bearer_no_email(self):
        cfg = Config(base_url="https://x.atlassian.net", email="", api_token="pat")
        assert cfg.use_bearer is True

    def test_use_bearer_with_email(self):
        cfg = Config(base_url="https://x.atlassian.net", email="a@b.com", api_token="tok")
        assert cfg.use_bearer is False

    def test_validate_missing_base_url(self):
        cfg = Config(base_url="", email="", api_token="tok")
        with pytest.raises(ValueError, match="base_url"):
            cfg.validate()

    def test_validate_ok_without_token(self):
        cfg = Config(base_url="https://x.atlassian.net", email="", api_token="")
        cfg.validate()  # should not raise


class TestLoadConfig:
    def test_from_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://env.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "env@test.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "env-token")
        # Point config path to non-existent file
        monkeypatch.setattr("confluence_export.config.config_path", lambda: tmp_path / "nope.json")

        cfg = load_config()
        assert cfg.base_url == "https://env.atlassian.net"
        assert cfg.email == "env@test.com"
        assert cfg.api_token == "env-token"

    def test_from_file(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "base_url": "https://file.atlassian.net/",
            "email": "file@test.com",
            "api_token": "file-token",
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: config_file)
        monkeypatch.delenv("CONFLUENCE_BASE_URL", raising=False)
        monkeypatch.delenv("CONFLUENCE_EMAIL", raising=False)
        monkeypatch.delenv("CONFLUENCE_API_TOKEN", raising=False)
        monkeypatch.delenv("CONFLUENCE_PAT", raising=False)

        cfg = load_config()
        assert cfg.base_url == "https://file.atlassian.net"  # trailing slash stripped
        assert cfg.email == "file@test.com"
        assert cfg.api_token == "file-token"

    def test_explicit_args_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://env.atlassian.net")
        monkeypatch.setattr("confluence_export.config.config_path", lambda: tmp_path / "nope.json")

        cfg = load_config(base_url="https://arg.atlassian.net", api_token="arg-tok")
        assert cfg.base_url == "https://arg.atlassian.net"
        assert cfg.api_token == "arg-tok"

    def test_pat_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://x.atlassian.net")
        monkeypatch.setenv("CONFLUENCE_PAT", "pat-value")
        monkeypatch.delenv("CONFLUENCE_API_TOKEN", raising=False)
        monkeypatch.delenv("CONFLUENCE_EMAIL", raising=False)
        monkeypatch.setattr("confluence_export.config.config_path", lambda: tmp_path / "nope.json")

        cfg = load_config()
        assert cfg.api_token == "pat-value"

    def test_missing_base_url_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONFLUENCE_BASE_URL", raising=False)
        monkeypatch.delenv("CONFLUENCE_EMAIL", raising=False)
        monkeypatch.delenv("CONFLUENCE_API_TOKEN", raising=False)
        monkeypatch.delenv("CONFLUENCE_PAT", raising=False)
        monkeypatch.setattr("confluence_export.config.config_path", lambda: tmp_path / "nope.json")

        with pytest.raises(ValueError, match="base_url"):
            load_config()


class TestIsScopedToken:
    def test_scoped_token_detected(self):
        # Real-world scoped token shape: ATATT3 + ... + =ADA<hex>
        token = "ATATT3xFfGF0_dummy_payload_TgVilzYuG3Sh8MtCp_8=ADA80198"
        assert is_scoped_token(token) is True

    def test_legacy_atatt_without_ada_suffix(self):
        # Old-style ATATT tokens (full-access) lack the =ADA scope marker
        assert is_scoped_token("ATATT3xFfGF0_dummy_no_scope_marker_here") is False

    def test_non_atatt_token(self):
        # Server PATs (Bearer-style) don't start with ATATT
        assert is_scoped_token("NDgyNDk2OTk2NzY3OmDsiR4mCSIaSjMqOg") is False

    def test_empty_token(self):
        assert is_scoped_token("") is False


class TestIsAtlassianSiteUrl:
    def test_site_url(self):
        assert is_atlassian_site_url("https://acme.atlassian.net") is True
        assert is_atlassian_site_url("https://acme.atlassian.net/") is True

    def test_requires_https(self):
        assert is_atlassian_site_url("http://acme.atlassian.net") is False

    def test_gateway_url(self):
        assert is_atlassian_site_url("https://api.atlassian.com/ex/confluence/abc") is False

    def test_self_hosted(self):
        assert is_atlassian_site_url("https://wiki.example.com") is False

    def test_empty(self):
        assert is_atlassian_site_url("") is False


class TestGatewayUrl:
    def test_format(self):
        cid = "6298609d-df12-4367-a2f6-2ead80671779"
        assert gateway_url(cid) == f"https://api.atlassian.com/ex/confluence/{cid}"

    def test_detects_gateway_url(self):
        assert is_gateway_url("https://api.atlassian.com/ex/confluence/abc") is True
        assert is_gateway_url("https://acme.atlassian.net") is False


class TestResolveCloudId:
    def test_rejects_non_atlassian_urls_without_request(self):
        with patch("confluence_export.config.requests.get") as mock_get:
            assert resolve_cloud_id("http://169.254.169.254") is None
            assert resolve_cloud_id("http://acme.atlassian.net") is None
            assert resolve_cloud_id("https://wiki.example.com") is None

        mock_get.assert_not_called()


class TestSaveConfig:
    def test_saves_and_restricts_permissions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("confluence_export.config._config_dir", lambda: tmp_path)

        cfg = Config(base_url="https://x.atlassian.net", email="a@b.com", api_token="secret")
        path = save_config(cfg)

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["version"] == 2
        assert data["site_url"] == "https://x.atlassian.net"
        assert data["auth"]["token"] == "secret"
        assert oct(path.stat().st_mode & 0o777) == "0o600"


class TestConnectionProfile:
    def test_cookie_profile_resolution(self):
        profile = load_connection_profile(
            site_url="https://x.atlassian.net/",
            cookie="tenant.session.token=abc",
            interactive=False,
        )

        assert profile.site_url == "https://x.atlassian.net"
        assert profile.api_base_url == "https://x.atlassian.net"
        assert profile.auth_mode is AuthMode.COOKIE
        assert profile.api_dialect is ApiDialect.COOKIE_V1
        assert profile.cloud_id is None

    def test_scoped_token_profile_resolution(self):
        profile = load_connection_profile(
            site_url="https://x.atlassian.net",
            email="a@b.com",
            api_token="ATATT3x_dummy=ADA123",
            interactive=False,
            resolve_cloud=lambda url: "cloud-123",
        )

        assert profile.site_url == "https://x.atlassian.net"
        assert profile.cloud_id == "cloud-123"
        assert profile.api_base_url == gateway_url("cloud-123")
        assert profile.auth_mode is AuthMode.SCOPED_API_TOKEN
        assert profile.api_dialect is ApiDialect.GATEWAY_V2

    def test_scoped_token_failure_without_cloud_id(self):
        with pytest.raises(ConnectionProfileError, match="cloud ID"):
            load_connection_profile(
                site_url="https://x.atlassian.net",
                email="a@b.com",
                api_token="ATATT3x_dummy=ADA123",
                interactive=False,
                resolve_cloud=lambda url: None,
            )

    def test_gateway_site_url_is_rejected(self):
        with pytest.raises(ConnectionProfileError, match="site_url"):
            load_connection_profile(
                site_url="https://api.atlassian.com/ex/confluence/cloud-123",
                api_base_url="https://api.atlassian.com/ex/confluence/cloud-123",
                email="a@b.com",
                api_token="ATATT3x_dummy=ADA123",
                interactive=False,
            )

    def test_scoped_token_to_internal_url_does_not_request_cloud_id(self):
        with patch("confluence_export.config.requests.get") as mock_get:
            with pytest.raises(ConnectionProfileError, match="HTTPS"):
                load_connection_profile(
                    site_url="http://169.254.169.254",
                    email="a@b.com",
                    api_token="ATATT3x_dummy=ADA123",
                    interactive=False,
                )

        mock_get.assert_not_called()

    def test_scoped_token_without_email_is_not_bearer(self):
        with pytest.raises(ConnectionProfileError, match="email and API token"):
            load_connection_profile(
                site_url="https://x.atlassian.net",
                api_token="ATATT3x_dummy=ADA123",
                interactive=False,
                resolve_cloud=lambda url: "cloud-123",
            )

    def test_legacy_token_profile_resolution(self):
        profile = load_connection_profile(
            site_url="https://x.atlassian.net",
            email="a@b.com",
            api_token="legacy-token",
            interactive=False,
        )

        assert profile.auth_mode is AuthMode.BASIC_API_TOKEN
        assert profile.api_dialect is ApiDialect.CLOUD_V2
        assert profile.api_base_url == "https://x.atlassian.net"

    def test_bearer_profile_resolution(self):
        profile = load_connection_profile(
            site_url="https://x.atlassian.net",
            api_token="pat-token",
            interactive=False,
        )

        assert profile.auth_mode is AuthMode.BEARER_PAT
        assert profile.api_dialect is ApiDialect.CLOUD_V2

    def test_cli_email_reinfers_saved_bearer_as_basic(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        save_connection_config(
            site_url="https://x.atlassian.net",
            auth=AuthConfig(type=AuthMode.BEARER_PAT, token="token"),
            path=config_file,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: config_file)

        profile = load_connection_profile(email="a@b.com", interactive=False)

        assert profile.auth_mode is AuthMode.BASIC_API_TOKEN
        assert profile.auth.email == "a@b.com"
        assert profile.auth.token == "token"

    def test_explicit_cli_bearer_auth_type_is_not_reinferred_from_email(self):
        profile = load_connection_profile(
            site_url="https://x.atlassian.net",
            auth_type=AuthMode.BEARER_PAT,
            email="a@b.com",
            api_token="pat-token",
            interactive=False,
        )

        assert profile.auth_mode is AuthMode.BEARER_PAT
        assert profile.api_dialect is ApiDialect.CLOUD_V2

    def test_cli_token_override_reinfers_saved_scoped_as_basic(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        save_connection_config(
            site_url="https://x.atlassian.net",
            cloud_id="cloud-old",
            api_base_url=gateway_url("cloud-old"),
            auth=AuthConfig(
                type=AuthMode.SCOPED_API_TOKEN,
                email="a@b.com",
                token="ATATT3x_dummy=ADA123",
            ),
            path=config_file,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: config_file)

        profile = load_connection_profile(api_token="legacy-token", interactive=False)

        assert profile.auth_mode is AuthMode.BASIC_API_TOKEN
        assert profile.api_dialect is ApiDialect.CLOUD_V2
        assert profile.api_base_url == "https://x.atlassian.net"
        assert profile.cloud_id is None
        assert profile.auth.email == "a@b.com"
        assert profile.auth.token == "legacy-token"

    def test_cli_site_override_drops_cached_gateway_route(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        save_connection_config(
            site_url="https://old.atlassian.net",
            cloud_id="cloud-old",
            api_base_url=gateway_url("cloud-old"),
            auth=AuthConfig(
                type=AuthMode.SCOPED_API_TOKEN,
                email="old@test.com",
                token="ATATT3x_dummy=ADA123",
            ),
            path=config_file,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: config_file)

        profile = load_connection_profile(
            site_url="https://new.atlassian.net",
            email="new@test.com",
            api_token="legacy-token",
            interactive=False,
        )

        assert profile.site_url == "https://new.atlassian.net"
        assert profile.auth_mode is AuthMode.BASIC_API_TOKEN
        assert profile.api_base_url == "https://new.atlassian.net"
        assert profile.cloud_id is None

    def test_cli_token_auth_override_drops_saved_cookie(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        save_connection_config(
            site_url="https://x.atlassian.net",
            auth=AuthConfig(type=AuthMode.COOKIE, cookie_header="session=abc"),
            path=config_file,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: config_file)

        profile = load_connection_profile(
            email="a@b.com",
            api_token="legacy-token",
            interactive=False,
        )

        assert profile.auth_mode is AuthMode.BASIC_API_TOKEN
        assert profile.api_dialect is ApiDialect.CLOUD_V2
        assert profile.auth.cookie_header == ""

    def test_non_scoped_auth_rejects_gateway_api_base_url(self):
        with pytest.raises(ConnectionProfileError, match="gateway api_base_url"):
            load_connection_profile(
                site_url="https://x.atlassian.net",
                api_base_url=gateway_url("cloud-123"),
                email="a@b.com",
                api_token="legacy-token",
                interactive=False,
            )

    def test_scoped_auth_rejects_non_gateway_api_base_url(self):
        with pytest.raises(ConnectionProfileError, match="OAuth gateway URL"):
            load_connection_profile(
                site_url="https://x.atlassian.net",
                api_base_url="https://x.atlassian.net",
                email="a@b.com",
                api_token="ATATT3x_dummy=ADA123",
                interactive=False,
                resolve_cloud=lambda url: "cloud-123",
            )

    def test_explicit_basic_auth_type_inherits_credentials_but_drops_gateway_route(
        self, tmp_path, monkeypatch
    ):
        config_file = tmp_path / "config.json"
        save_connection_config(
            site_url="https://x.atlassian.net",
            cloud_id="cloud-old",
            api_base_url=gateway_url("cloud-old"),
            auth=AuthConfig(
                type=AuthMode.SCOPED_API_TOKEN,
                email="a@b.com",
                token="legacy-token",
            ),
            path=config_file,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: config_file)

        profile = load_connection_profile(
            auth_type=AuthMode.BASIC_API_TOKEN,
            interactive=False,
        )

        assert profile.auth_mode is AuthMode.BASIC_API_TOKEN
        assert profile.api_base_url == "https://x.atlassian.net"
        assert profile.cloud_id is None
        assert profile.auth.email == "a@b.com"
        assert profile.auth.token == "legacy-token"

    def test_v1_config_migration_in_memory(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "base_url": "https://file.atlassian.net/",
            "email": "file@test.com",
            "api_token": "file-token",
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: config_file)

        profile = load_connection_profile(interactive=False)

        assert profile.site_url == "https://file.atlassian.net"
        assert profile.api_base_url == "https://file.atlassian.net"
        assert profile.auth_mode is AuthMode.BASIC_API_TOKEN

    def test_v1_gateway_base_url_fails_with_repair_instruction(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "base_url": "https://api.atlassian.com/ex/confluence/cloud-123",
            "email": "file@test.com",
            "api_token": "file-token",
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: config_file)

        with pytest.raises(ConnectionProfileError, match="OAuth gateway"):
            load_connection_profile(interactive=False)

    def test_v2_config_roundtrip(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        save_connection_config(
            site_url="https://x.atlassian.net",
            auth=AuthConfig(type=AuthMode.COOKIE, cookie_header="session=abc"),
            path=config_file,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: config_file)

        profile = load_connection_profile(interactive=False)

        assert profile.auth_mode is AuthMode.COOKIE
        assert profile.api_dialect is ApiDialect.COOKIE_V1

    def test_save_rejects_gateway_site_url(self, tmp_path):
        with pytest.raises(ConnectionProfileError, match="site_url"):
            save_connection_config(
                site_url="https://api.atlassian.com/ex/confluence/cloud-123",
                auth=AuthConfig(type=AuthMode.SCOPED_API_TOKEN, email="a@b.com", token="tok"),
                path=tmp_path / "config.json",
            )

    def test_local_config_precedence(self, tmp_path, monkeypatch):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(type=AuthMode.BEARER_PAT, token="global"),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        save_connection_config(
            site_url="https://local.atlassian.net",
            auth=AuthConfig(type=AuthMode.COOKIE, cookie_header="session=abc"),
            path=local_dir / "config.json",
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        profile = load_connection_profile(start_dir=tmp_path / "docs" / "export", interactive=False)

        assert profile.site_url == "https://local.atlassian.net"
        assert profile.auth_mode is AuthMode.COOKIE
        assert profile.config_source.endswith("docs/.conex/config.json")

    def test_local_host_override_cannot_borrow_global_credentials(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                token="global-secret",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://evil.example.com",
            "auth": {"type": "basic_api_token"},
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        with pytest.raises(ConnectionProfileError, match="email and API token"):
            load_connection_profile(
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_local_host_override_with_partial_auth_cannot_borrow_secret(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                token="global-secret",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://evil.example.com",
            "auth": {"type": "basic_api_token", "email": "attacker@example.com"},
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        with pytest.raises(ConnectionProfileError, match="email and API token"):
            load_connection_profile(
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_local_origin_override_cannot_borrow_global_credentials(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                token="global-secret",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://global.atlassian.net:8443",
            "auth": {"type": "basic_api_token"},
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        with pytest.raises(ConnectionProfileError, match="email and API token"):
            load_connection_profile(
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_cli_route_override_cannot_borrow_global_credentials(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                token="global-secret",
            ),
            path=global_config,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        with pytest.raises(ConnectionProfileError, match="authentication credentials"):
            load_connection_profile(
                site_url="https://evil.example.com",
                interactive=False,
            )

    def test_env_route_override_cannot_borrow_global_credentials(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                token="global-secret",
            ),
            path=global_config,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://evil.example.com")

        with pytest.raises(ConnectionProfileError, match="authentication credentials"):
            load_connection_profile(interactive=False)

    def test_env_token_cannot_attach_to_local_site_override(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                token="global-secret",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://evil.example.com",
            "auth": {"type": "basic_api_token", "email": "attacker@example.com"},
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "env-secret")

        with pytest.raises(ConnectionProfileError, match="credential-only"):
            load_connection_profile(
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_env_token_with_matching_site_url_can_complete_local_config(
        self, tmp_path, monkeypatch
    ):
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://docs.mycompany.com",
            "auth": {"type": "basic_api_token", "email": "user@example.com"},
        }))
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://docs.mycompany.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "env-secret")

        profile = load_connection_profile(
            start_dir=tmp_path / "docs" / "export",
            interactive=False,
        )

        assert profile.site_url == "https://docs.mycompany.com"
        assert profile.auth.token == "env-secret"

    def test_env_credentials_can_pair_with_cli_site_url(self, monkeypatch):
        monkeypatch.setenv("CONFLUENCE_EMAIL", "user@example.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "env-secret")

        profile = load_connection_profile(
            site_url="https://docs.mycompany.com",
            interactive=False,
        )

        assert profile.site_url == "https://docs.mycompany.com"
        assert profile.auth.type is AuthMode.BASIC_API_TOKEN
        assert profile.auth.email == "user@example.com"
        assert profile.auth.token == "env-secret"

    def test_env_credentials_can_pair_with_cli_site_url_over_local_config(
        self, tmp_path, monkeypatch
    ):
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://local.example.com",
            "auth": {"type": "basic_api_token"},
        }))
        monkeypatch.setenv("CONFLUENCE_EMAIL", "user@example.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "env-secret")

        profile = load_connection_profile(
            site_url="https://cli.example.com",
            start_dir=tmp_path / "docs" / "export",
            interactive=False,
        )

        assert profile.site_url == "https://cli.example.com"
        assert profile.auth.type is AuthMode.BASIC_API_TOKEN
        assert profile.auth.email == "user@example.com"
        assert profile.auth.token == "env-secret"

    def test_env_credentials_with_only_cli_cloud_id_cannot_attach_to_local_site(
        self, tmp_path, monkeypatch
    ):
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://local.example.com",
            "auth": {"type": "basic_api_token"},
        }))
        monkeypatch.setenv("CONFLUENCE_EMAIL", "user@example.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "env-secret")

        with pytest.raises(ConnectionProfileError, match="credential-only"):
            load_connection_profile(
                cloud_id="cloud-from-cli",
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_env_credentials_with_only_env_cloud_id_cannot_attach_to_local_site(
        self, tmp_path, monkeypatch
    ):
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://local.example.com",
            "auth": {"type": "basic_api_token"},
        }))
        monkeypatch.setenv("CONFLUENCE_EMAIL", "user@example.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "env-secret")
        monkeypatch.setenv("CONFLUENCE_CLOUD_ID", "cloud-from-env")

        with pytest.raises(ConnectionProfileError, match="credential-only"):
            load_connection_profile(
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_route_less_global_credentials_do_not_attach_to_local_site(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        global_config.write_text(json.dumps({
            "version": 2,
            "site_url": "",
            "auth": {
                "type": "basic_api_token",
                "email": "user@example.com",
                "token": "global-secret",
            },
        }))
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://local.example.com",
            "auth": {"type": "basic_api_token"},
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        with pytest.raises(ConnectionProfileError, match="email and API token"):
            load_connection_profile(
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_route_less_global_credentials_can_pair_with_cli_site_url(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        global_config.write_text(json.dumps({
            "version": 2,
            "site_url": "",
            "auth": {
                "type": "basic_api_token",
                "email": "user@example.com",
                "token": "global-secret",
            },
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        profile = load_connection_profile(
            site_url="https://cli.example.com",
            interactive=False,
        )

        assert profile.site_url == "https://cli.example.com"
        assert profile.auth.email == "user@example.com"
        assert profile.auth.token == "global-secret"

    def test_env_route_allows_cli_token_over_local_config(
        self, tmp_path, monkeypatch
    ):
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://local.example.com",
            "auth": {"type": "basic_api_token"},
        }))
        monkeypatch.setenv("CONFLUENCE_SITE_URL", "https://cli.example.com")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "user@example.com")

        profile = load_connection_profile(
            api_token="cli-secret",
            start_dir=tmp_path / "docs" / "export",
            interactive=False,
        )

        assert profile.site_url == "https://cli.example.com"
        assert profile.auth.type is AuthMode.BASIC_API_TOKEN
        assert profile.auth.email == "user@example.com"
        assert profile.auth.token == "cli-secret"

    def test_default_https_port_is_same_credential_scope(self, tmp_path, monkeypatch):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                token="global-secret",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://global.atlassian.net:443",
            "auth": {"type": "basic_api_token"},
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        profile = load_connection_profile(
            start_dir=tmp_path / "docs" / "export",
            interactive=False,
        )

        assert profile.auth.token == "global-secret"

    def test_malformed_override_port_raises_connection_profile_error(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                token="global-secret",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://local.atlassian.net:bad",
            "auth": {"type": "basic_api_token"},
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        with pytest.raises(ConnectionProfileError, match="site_url must be an HTTPS URL"):
            load_connection_profile(
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_malformed_ipv6_url_raises_connection_profile_error(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                token="global-secret",
            ),
            path=global_config,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        with pytest.raises(ConnectionProfileError, match="site_url must be an HTTPS URL"):
            load_connection_profile(
                site_url="https://[::1",
                auth_type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                api_token="cli-secret",
                interactive=False,
            )

    def test_local_gateway_cloud_override_cannot_borrow_scoped_token(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            cloud_id="cloud-old",
            api_base_url=gateway_url("cloud-old"),
            auth=AuthConfig(
                type=AuthMode.SCOPED_API_TOKEN,
                email="user@example.com",
                token="ATATT3x_dummy=ADA123",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://global.atlassian.net",
            "api_base_url": gateway_url("cloud-evil"),
            "auth": {"type": "scoped_api_token"},
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        with pytest.raises(ConnectionProfileError, match="email and API token"):
            load_connection_profile(
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_cli_cloud_id_can_complete_global_scoped_token_profile(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.SCOPED_API_TOKEN,
                email="user@example.com",
                token="ATATT3x_dummy=ADA123",
            ),
            path=global_config,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        profile = load_connection_profile(
            cloud_id="cloud-new",
            interactive=False,
        )

        assert profile.auth.token == "ATATT3x_dummy=ADA123"
        assert profile.cloud_id == "cloud-new"
        assert profile.api_base_url == gateway_url("cloud-new")

    def test_cli_cloud_id_can_complete_local_scoped_token_with_own_credentials(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            cloud_id="cloud-old",
            api_base_url=gateway_url("cloud-old"),
            auth=AuthConfig(
                type=AuthMode.SCOPED_API_TOKEN,
                email="global@example.com",
                token="ATATT3x_dummy=GLOBAL",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        save_connection_config(
            site_url="https://docs.mycompany.com",
            auth=AuthConfig(
                type=AuthMode.SCOPED_API_TOKEN,
                email="local@example.com",
                token="ATATT3x_dummy=LOCAL",
            ),
            path=local_dir / "config.json",
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        profile = load_connection_profile(
            start_dir=tmp_path / "docs" / "export",
            cloud_id="cloud-local",
            interactive=False,
            resolve_cloud=lambda _site: None,
        )

        assert profile.site_url == "https://docs.mycompany.com"
        assert profile.auth.email == "local@example.com"
        assert profile.auth.token == "ATATT3x_dummy=LOCAL"
        assert profile.cloud_id == "cloud-local"
        assert profile.api_base_url == gateway_url("cloud-local")

    def test_env_cloud_id_survives_env_credentials_with_cli_site_url(
        self, monkeypatch
    ):
        monkeypatch.setenv("CONFLUENCE_EMAIL", "user@example.com")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "ATATT3x_dummy=ADA123")
        monkeypatch.setenv("CONFLUENCE_CLOUD_ID", "cloud-env")

        profile = load_connection_profile(
            site_url="https://docs.example.com",
            interactive=False,
            resolve_cloud=lambda _site: None,
        )

        assert profile.cloud_id == "cloud-env"
        assert profile.api_base_url == gateway_url("cloud-env")

    def test_cli_cloud_id_rebuilds_stale_gateway_route(self, tmp_path, monkeypatch):
        global_config = tmp_path / "global.json"
        global_config.write_text(json.dumps({
            "version": 2,
            "site_url": "https://global.atlassian.net",
            "api_base_url": gateway_url("cloud-old"),
            "auth": {
                "type": "scoped_api_token",
                "email": "user@example.com",
                "token": "ATATT3x_dummy=ADA123",
            },
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        profile = load_connection_profile(
            cloud_id="cloud-new",
            interactive=False,
        )

        assert profile.cloud_id == "cloud-new"
        assert profile.api_base_url == gateway_url("cloud-new")

    def test_local_cloud_id_fill_cannot_borrow_global_scoped_token_without_cloud(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.SCOPED_API_TOKEN,
                email="user@example.com",
                token="ATATT3x_dummy=ADA123",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://global.atlassian.net",
            "cloud_id": "cloud-evil",
            "auth": {"type": "scoped_api_token"},
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        with pytest.raises(ConnectionProfileError, match="email and API token"):
            load_connection_profile(
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_local_cloud_id_override_cannot_borrow_scoped_token(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            cloud_id="cloud-old",
            api_base_url=gateway_url("cloud-old"),
            auth=AuthConfig(
                type=AuthMode.SCOPED_API_TOKEN,
                email="user@example.com",
                token="ATATT3x_dummy=ADA123",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text(json.dumps({
            "version": 2,
            "site_url": "https://global.atlassian.net",
            "cloud_id": "cloud-evil",
            "auth": {"type": "scoped_api_token", "email": "attacker@example.com"},
        }))
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        with pytest.raises(ConnectionProfileError, match="email and API token"):
            load_connection_profile(
                start_dir=tmp_path / "docs" / "export",
                interactive=False,
            )

    def test_non_https_site_url_is_rejected_for_basic_auth(self):
        with pytest.raises(ConnectionProfileError, match="HTTPS"):
            load_connection_profile(
                site_url="http://x.atlassian.net",
                email="a@b.com",
                api_token="legacy-token",
                interactive=False,
            )

    def test_local_custom_domain_with_own_credentials_is_allowed(
        self, tmp_path, monkeypatch
    ):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(
                type=AuthMode.BASIC_API_TOKEN,
                email="user@example.com",
                token="global-secret",
            ),
            path=global_config,
        )
        local_dir = tmp_path / "docs" / ".conex"
        local_dir.mkdir(parents=True)
        save_connection_config(
            site_url="https://docs.mycompany.com",
            auth=AuthConfig(type=AuthMode.COOKIE, cookie_header="tenant.session=abc"),
            path=local_dir / "config.json",
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        profile = load_connection_profile(
            start_dir=tmp_path / "docs" / "export",
            interactive=False,
        )

        assert profile.site_url == "https://docs.mycompany.com"
        assert profile.auth_mode is AuthMode.COOKIE
        assert profile.auth.cookie_header == "tenant.session=abc"

    def test_global_fallback(self, tmp_path, monkeypatch):
        global_config = tmp_path / "global.json"
        save_connection_config(
            site_url="https://global.atlassian.net",
            auth=AuthConfig(type=AuthMode.BEARER_PAT, token="global"),
            path=global_config,
        )
        monkeypatch.setattr("confluence_export.config.config_path", lambda: global_config)

        profile = load_connection_profile(start_dir=tmp_path / "docs", interactive=False)

        assert profile.site_url == "https://global.atlassian.net"
        assert profile.config_source == str(global_config)

    def test_find_local_config_from_output_upward(self, tmp_path):
        config_file = tmp_path / "docs" / ".conex" / "config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text("{}")

        assert find_local_config(tmp_path / "docs" / "export") == config_file
