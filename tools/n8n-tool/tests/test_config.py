from pathlib import Path

import pytest

from n8n_tool.config import ConfigError, load_config


def test_load_config_missing_vars_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_API_KEY", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        load_config(env_file=tmp_path / "does-not-exist.env")
    assert "N8N_BASE_URL" in str(excinfo.value)
    assert "N8N_API_KEY" in str(excinfo.value)


def test_load_config_from_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("N8N_BASE_URL", "https://example.com/")
    monkeypatch.setenv("N8N_API_KEY", "supersecretkey")
    config = load_config(env_file=tmp_path / "does-not-exist.env")
    assert config.base_url == "https://example.com"  # trailing slash stripped
    assert config.api_key == "supersecretkey"


def test_load_config_from_dotenv_file(monkeypatch, tmp_path):
    monkeypatch.delenv("N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("N8N_BASE_URL=https://n8n.example.com\nN8N_API_KEY=abc123\n")
    config = load_config(env_file=env_file)
    assert config.base_url == "https://n8n.example.com"
    assert config.api_key == "abc123"


def test_load_config_rejects_bad_url_scheme(monkeypatch, tmp_path):
    monkeypatch.setenv("N8N_BASE_URL", "ftp://example.com")
    monkeypatch.setenv("N8N_API_KEY", "key")
    with pytest.raises(ConfigError):
        load_config(env_file=tmp_path / "does-not-exist.env")


def test_masked_api_key_never_reveals_full_key(monkeypatch, tmp_path):
    monkeypatch.setenv("N8N_BASE_URL", "https://example.com")
    monkeypatch.setenv("N8N_API_KEY", "abcdefghijklmnopqrstuvwxyz")
    config = load_config(env_file=tmp_path / "does-not-exist.env")
    masked = config.masked_api_key
    assert config.api_key not in masked
    assert masked.startswith("abcd")
    assert masked.endswith("wxyz")


def test_masked_api_key_short_key_fully_masked(monkeypatch, tmp_path):
    monkeypatch.setenv("N8N_BASE_URL", "https://example.com")
    monkeypatch.setenv("N8N_API_KEY", "short")
    config = load_config(env_file=tmp_path / "does-not-exist.env")
    assert config.masked_api_key == "*" * len("short")
