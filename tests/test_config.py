"""Tests for config loading, saving, and validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from confluence_export.config import Config, load_config, save_config


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


class TestSaveConfig:
    def test_saves_and_restricts_permissions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("confluence_export.config._config_dir", lambda: tmp_path)

        cfg = Config(base_url="https://x.atlassian.net", email="a@b.com", api_token="secret")
        path = save_config(cfg)

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["base_url"] == "https://x.atlassian.net"
        assert data["api_token"] == "secret"
        assert oct(path.stat().st_mode & 0o777) == "0o600"
