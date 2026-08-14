"""
Unit tests for playwright_prototype.config resolver helpers.

resolve_headless() precedence:
    1. PLAYWRIGHT_HEADLESS env var (highest)
    2. 'headless' key in orchestrator_config.json
    3. Default False (lowest)

resolve_debugger_address() precedence:
    1. PLAYWRIGHT_DEBUGGER_ADDRESS env var (highest)
    2. 'debugger_address' key in orchestrator_config.json
    3. Default 127.0.0.1:9222 (lowest)

No browser or Playwright installation required.
"""

import json
import pytest

from playwright_prototype.config import (
    resolve_browser_mode,
    resolve_debugger_address,
    resolve_edge_profile_directory,
    resolve_edge_user_data_dir,
    resolve_headless,
)


class TestResolveHeadlessEnvVar:
    """Env var takes priority over everything else."""

    def test_env_zero_returns_false(self, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "0")
        assert resolve_headless() is False

    def test_env_false_returns_false(self, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "false")
        assert resolve_headless() is False

    def test_env_no_returns_false(self, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "NO")
        assert resolve_headless() is False

    def test_env_one_returns_true(self, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "1")
        assert resolve_headless() is True

    def test_env_true_returns_true(self, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "true")
        assert resolve_headless() is True


class TestResolveHeadlessConfigFile:
    """When env var is absent, reads from the config file passed as config_path."""

    def test_config_false_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_HEADLESS", raising=False)
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"headless": False}), encoding="utf-8")
        assert resolve_headless(config_path=cfg) is False

    def test_config_true_returns_true(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_HEADLESS", raising=False)
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"headless": True}), encoding="utf-8")
        assert resolve_headless(config_path=cfg) is True

    def test_env_overrides_config(self, monkeypatch, tmp_path):
        """Env var wins even when config file says the opposite."""
        monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "0")
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"headless": True}), encoding="utf-8")
        assert resolve_headless(config_path=cfg) is False


class TestResolveHeadlessDefaults:
    """Falls back to False when neither env var nor config key is present."""

    def test_missing_key_defaults_false(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_HEADLESS", raising=False)
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"other_key": "value"}), encoding="utf-8")
        assert resolve_headless(config_path=cfg) is False

    def test_missing_file_defaults_false(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_HEADLESS", raising=False)
        nonexistent = tmp_path / "nonexistent.json"
        assert resolve_headless(config_path=nonexistent) is False

    def test_invalid_json_defaults_false(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_HEADLESS", raising=False)
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text("not valid json {{", encoding="utf-8")
        assert resolve_headless(config_path=cfg) is False


class TestResolveDebuggerAddress:
    """Debugger address resolution uses env var, then config file, then fallback."""

    def test_env_overrides_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_DEBUGGER_ADDRESS", "127.0.0.1:9333")
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"debugger_address": "127.0.0.1:9444"}), encoding="utf-8")
        assert resolve_debugger_address(config_path=cfg) == "127.0.0.1:9333"

    def test_config_value_used_when_env_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_DEBUGGER_ADDRESS", raising=False)
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"debugger_address": "127.0.0.1:9555"}), encoding="utf-8")
        assert resolve_debugger_address(config_path=cfg) == "127.0.0.1:9555"

    def test_default_used_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_DEBUGGER_ADDRESS", raising=False)
        cfg = tmp_path / "missing.json"
        assert resolve_debugger_address(config_path=cfg) == "127.0.0.1:9222"


class TestResolveBrowserMode:
    def test_env_overrides_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSER_MODE", "attach")
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"playwright_browser_mode": "profile"}), encoding="utf-8")
        assert resolve_browser_mode(config_path=cfg) == "attach"

    def test_config_value_used_when_env_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_BROWSER_MODE", raising=False)
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"playwright_browser_mode": "attach"}), encoding="utf-8")
        assert resolve_browser_mode(config_path=cfg) == "attach"

    def test_default_used_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_BROWSER_MODE", raising=False)
        cfg = tmp_path / "missing.json"
        assert resolve_browser_mode(config_path=cfg) == "profile"


class TestResolveEdgeUserDataDir:
    def test_env_overrides_config(self, monkeypatch, tmp_path):
        expected = tmp_path / "EdgeEnv"
        monkeypatch.setenv("PLAYWRIGHT_EDGE_USER_DATA_DIR", str(expected))
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"edge_user_data_dir": "C:/ignored/path"}), encoding="utf-8")
        assert resolve_edge_user_data_dir(config_path=cfg) == expected

    def test_config_value_used_when_env_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_EDGE_USER_DATA_DIR", raising=False)
        expected = tmp_path / "EdgeConfig"
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"edge_user_data_dir": str(expected)}), encoding="utf-8")
        assert resolve_edge_user_data_dir(config_path=cfg) == expected

    def test_default_uses_dedicated_automation_profile(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_EDGE_USER_DATA_DIR", raising=False)
        cfg = tmp_path / "missing.json"
        result = resolve_edge_user_data_dir(config_path=cfg)
        assert result.name == ".edge-playwright-profile"
        assert result.parent.name == "GlassOrchestrator"


class TestResolveEdgeProfileDirectory:
    def test_env_overrides_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_EDGE_PROFILE_DIRECTORY", "Profile 2")
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"edge_profile_directory": "Default"}), encoding="utf-8")
        assert resolve_edge_profile_directory(config_path=cfg) == "Profile 2"

    def test_config_value_used_when_env_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_EDGE_PROFILE_DIRECTORY", raising=False)
        cfg = tmp_path / "orchestrator_config.json"
        cfg.write_text(json.dumps({"edge_profile_directory": "Profile 3"}), encoding="utf-8")
        assert resolve_edge_profile_directory(config_path=cfg) == "Profile 3"

    def test_default_used_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PLAYWRIGHT_EDGE_PROFILE_DIRECTORY", raising=False)
        cfg = tmp_path / "missing.json"
        assert resolve_edge_profile_directory(config_path=cfg) == "Default"
