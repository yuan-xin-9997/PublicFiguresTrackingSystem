import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional


SRC_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CONFIG: Dict[str, Any] = {
    "app": {"name": "公开人物行程动态言论跟踪系统", "timezone": "Asia/Shanghai"},
    "server": {"host": "127.0.0.1", "port": 8000, "base_url": ""},
    "database": {"path": "data/app.sqlite3", "busy_timeout_ms": 5000},
    "security": {
        "password_file": "data/password.txt",
        "session_hours": 12,
        "cookie_secure": False,
        "login_max_attempts": 8,
        "login_window_seconds": 300,
    },
    "tasks": {"scheduler_enabled": False, "poll_seconds": 30, "max_items_per_run": 50},
    "collector": {
        "provider": "webfetch",
        "webfetch_base_url": "",
        "webfetch_api_key_env": "PFTS_WEBFETCH_API_KEY",
        "webfetch_profile": "anonymous",
        "webfetch_proxy_policy": "auto",
        "webfetch_cache_ttl": 900,
        "save_rss_artifacts": False,
        "direct_fallback": False,
        "user_agent": "PFTS/1.0 (+public-information-research)",
        "timeout_seconds": 15,
        "max_response_bytes": 2_000_000,
        "allow_private_hosts": False,
    },
    "ai": {
        "provider": "local",
        "base_url": "",
        "model": "local-rules-v1",
        "api_key_env": "PFTS_AI_API_KEY",
        "timeout_seconds": 30,
        "review_threshold": 0.7,
    },
    "map": {
        "provider": "leaflet", "tile_url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors", "default_center": [35.0, 105.0], "default_zoom": 3,
        "api_key_env": "PFTS_MAP_API_KEY",
    },
    "logging": {"level": "INFO", "retention_days": 30, "path": "logs/app.log"},
}


def _deep_merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _parse_env_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        return value


class Settings:
    def __init__(self, values: Dict[str, Any], config_path: Path):
        self.values = values
        self.config_path = config_path
        self.src_root = config_path.parent.parent if config_path.parent.name == "config" else SRC_ROOT

    def get(self, section: str, key: Optional[str] = None, default: Any = None) -> Any:
        value = self.values.get(section, default)
        if key is None:
            return value
        if not isinstance(value, dict):
            return default
        return value.get(key, default)

    def path(self, section: str, key: str) -> Path:
        raw = Path(str(self.get(section, key)))
        return raw if raw.is_absolute() else (self.src_root / raw).resolve()

    def masked(self) -> Dict[str, Any]:
        sensitive = ("password", "secret", "token", "key", "cookie")

        def mask(value: Any, name: str = "") -> Any:
            if any(part in name.lower() for part in sensitive):
                if name.endswith("_env"):
                    return {"environment_variable": str(value), "configured": bool(os.getenv(str(value)))}
                return "******" if value not in (None, "") else ""
            if isinstance(value, dict):
                return {k: mask(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [mask(v, name) for v in value]
            return value

        return mask(deepcopy(self.values))


def load_config(config_path: Optional[str] = None) -> Settings:
    configured = config_path or os.getenv("PFTS_CONFIG")
    path = Path(configured).resolve() if configured else (SRC_ROOT / "config" / "app.json")
    values = deepcopy(DEFAULT_CONFIG)
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("app.json 顶层必须是 JSON 对象")
        values = _deep_merge(values, loaded)

    for env_name, env_value in os.environ.items():
        if not env_name.startswith("PFTS_") or env_name in {"PFTS_CONFIG", "PFTS_AI_API_KEY", "PFTS_MAP_API_KEY"}:
            continue
        parts = env_name[5:].lower().split("__")
        if len(parts) != 2 or parts[0] not in values:
            continue
        values.setdefault(parts[0], {})[parts[1]] = _parse_env_value(env_value)
    return Settings(values, path)
