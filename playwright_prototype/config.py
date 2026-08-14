import json
import logging
import os
from pathlib import Path

from config.config_loader import get_config

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

LOGIN_URL: str = get_config("login_url", "https://avisbudget.palantirfoundry.com/workspace/fleet-operations-pwa/health")
FOUNDRY_HOME_URL: str = get_config("foundry_home_url", "https://avisbudget.palantirfoundry.com/")
SSO_EMAIL: str = get_config("credentials.sso_email", "")
STORAGE_STATE_PATH: Path = BASE_DIR / "storage_state.json"
DEFAULT_DEBUGGER_ADDRESS = "127.0.0.1:9222"
DEFAULT_BROWSER_MODE = "profile"
DEFAULT_EDGE_PROFILE_DIRECTORY = "Default"
DEFAULT_EDGE_USER_DATA_DIR = BASE_DIR / ".edge-playwright-profile"


def resolve_headless(config_path: Path | None = None) -> bool:
    """Check PLAYWRIGHT_HEADLESS env var first, then orchestrator_config.json, then default False."""
    env_val = os.getenv("PLAYWRIGHT_HEADLESS")
    if env_val is not None:
        result = env_val.strip().lower() not in ("0", "false", "no")
        log.info("Headless resolved from env PLAYWRIGHT_HEADLESS=%r → %s", env_val, result)
        return result

    path = config_path or (BASE_DIR / "orchestrator_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "headless" in data:
            result = bool(data["headless"])
            log.info("Headless resolved from %s → %s", path.name, result)
            return result
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    log.info("Headless defaulting to False")
    return False


def resolve_debugger_address(config_path: Path | None = None) -> str:
    """Resolve Edge debugger host:port for CDP attach mode.

    Priority: PLAYWRIGHT_DEBUGGER_ADDRESS env var -> orchestrator_config.json
    'debugger_address' key -> default 127.0.0.1:9222.
    """
    env_val = os.getenv("PLAYWRIGHT_DEBUGGER_ADDRESS")
    if env_val and env_val.strip():
        result = env_val.strip()
        log.info("Debugger address resolved from env PLAYWRIGHT_DEBUGGER_ADDRESS=%r", result)
        return result

    path = config_path or (BASE_DIR / "orchestrator_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        configured = str(data.get("debugger_address", "")).strip()
        if configured:
            log.info("Debugger address resolved from %s -> %s", path.name, configured)
            return configured
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    log.info("Debugger address defaulting to %s", DEFAULT_DEBUGGER_ADDRESS)
    return DEFAULT_DEBUGGER_ADDRESS


def resolve_browser_mode(config_path: Path | None = None) -> str:
    """Resolve Playwright browser startup mode.

    Supported values: "attach" and "profile".
    Priority: PLAYWRIGHT_BROWSER_MODE env var -> orchestrator_config.json
    'playwright_browser_mode' key -> default "profile".
    """
    env_val = os.getenv("PLAYWRIGHT_BROWSER_MODE")
    if env_val and env_val.strip():
        result = env_val.strip().lower()
        if result in {"attach", "profile"}:
            log.info("Browser mode resolved from env PLAYWRIGHT_BROWSER_MODE=%r", result)
            return result

    path = config_path or (BASE_DIR / "orchestrator_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        configured = str(data.get("playwright_browser_mode", "")).strip().lower()
        if configured in {"attach", "profile"}:
            log.info("Browser mode resolved from %s -> %s", path.name, configured)
            return configured
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    log.info("Browser mode defaulting to %s", DEFAULT_BROWSER_MODE)
    return DEFAULT_BROWSER_MODE


def resolve_edge_user_data_dir(config_path: Path | None = None) -> Path:
    """Resolve the Edge user data directory for profile-backed launches."""
    env_val = os.getenv("PLAYWRIGHT_EDGE_USER_DATA_DIR")
    if env_val and env_val.strip():
        result = Path(env_val.strip())
        log.info("Edge user data dir resolved from env PLAYWRIGHT_EDGE_USER_DATA_DIR=%s", result)
        return result

    path = config_path or (BASE_DIR / "orchestrator_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        configured = str(data.get("edge_user_data_dir", "")).strip()
        if configured:
            result = Path(configured)
            log.info("Edge user data dir resolved from %s -> %s", path.name, result)
            return result
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    log.info("Edge user data dir defaulting to %s", DEFAULT_EDGE_USER_DATA_DIR)
    return DEFAULT_EDGE_USER_DATA_DIR


def resolve_edge_profile_directory(config_path: Path | None = None) -> str:
    """Resolve the Edge profile directory name for profile-backed launches."""
    env_val = os.getenv("PLAYWRIGHT_EDGE_PROFILE_DIRECTORY")
    if env_val and env_val.strip():
        result = env_val.strip()
        log.info("Edge profile directory resolved from env PLAYWRIGHT_EDGE_PROFILE_DIRECTORY=%r", result)
        return result

    path = config_path or (BASE_DIR / "orchestrator_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        configured = str(data.get("edge_profile_directory", "")).strip()
        if configured:
            log.info("Edge profile directory resolved from %s -> %s", path.name, configured)
            return configured
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    log.info("Edge profile directory defaulting to %s", DEFAULT_EDGE_PROFILE_DIRECTORY)
    return DEFAULT_EDGE_PROFILE_DIRECTORY


def resolve_step_delay(config_path: Path | None = None) -> int:
    """Return inter-step delay in milliseconds.

    Priority: PLAYWRIGHT_STEP_DELAY env var (seconds) → orchestrator_config.local.json
    → orchestrator_config.json 'step_delay' key (seconds) → default 0.
    """
    env_val = os.getenv("PLAYWRIGHT_STEP_DELAY")
    if env_val is not None:
        try:
            result = int(float(env_val.strip()) * 1000)
            log.info("Step delay resolved from env PLAYWRIGHT_STEP_DELAY=%r → %dms", env_val, result)
            return result
        except ValueError:
            pass

    base_path = config_path or (BASE_DIR / "orchestrator_config.json")
    local_path = base_path.with_name(base_path.stem + ".local.json")

    data: dict = {}
    for path in (base_path, local_path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass

    if "step_delay" in data:
        try:
            result = int(float(data["step_delay"]) * 1000)
            log.info("Step delay resolved from config → %dms", result)
            return result
        except ValueError:
            pass

    return 0


def resolve_initial_delay(config_path: Path | None = None) -> int:
    """Return startup delay in milliseconds before first flow action.

    Priority: PLAYWRIGHT_INITIAL_DELAY env var (seconds) ->
    orchestrator_config.local.json / orchestrator_config.json 'initial_delay'
    key (seconds) -> default 5 seconds.
    """
    env_val = os.getenv("PLAYWRIGHT_INITIAL_DELAY")
    if env_val is not None:
        try:
            result = int(float(env_val.strip()) * 1000)
            log.info("Initial delay resolved from env PLAYWRIGHT_INITIAL_DELAY=%r → %dms", env_val, result)
            return result
        except ValueError:
            pass

    base_path = config_path or (BASE_DIR / "orchestrator_config.json")
    local_path = base_path.with_name(base_path.stem + ".local.json")

    data: dict = {}
    for path in (base_path, local_path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass

    if "initial_delay" in data:
        try:
            result = int(float(data["initial_delay"]) * 1000)
            log.info("Initial delay resolved from config → %dms", result)
            return result
        except ValueError:
            pass

    default_result = 5000
    log.info("Initial delay defaulting to %dms", default_result)
    return default_result
