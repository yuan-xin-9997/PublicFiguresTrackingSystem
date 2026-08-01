import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional


SRC_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CONFIG: Dict[str, Any] = {
    "app": {"name": "公开人物行程动态言论跟踪系统", "timezone": "Asia/Shanghai"},
    "server": {"host": "127.0.0.1", "port": 28000, "base_url": ""},
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
        "model": "local-rules-v2",
        "api_key_env": "PFTS_AI_API_KEY",
        "timeout_seconds": 30,
        "review_threshold": 0.7,
    },
    "map": {
        "provider": "leaflet", "tile_url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors", "default_center": [35.0, 105.0], "default_zoom": 3,
        "api_key_env": "PFTS_MAP_API_KEY",
    },
    "notifications": {
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "security": "starttls",
            "username": "",
            "password_env": "PFTS_SMTP_PASSWORD",
            "credential_key_env": "PFTS_NOTIFICATION_CREDENTIAL_KEY",
            "from_address": "",
            "from_name": "",
            "to_addresses": [],
            "subject_prefix": "[PFTS]",
            "max_events_per_message": 25,
            "worker_poll_seconds": 15,
            "max_attempts": 5,
            "retry_base_seconds": 60,
            "timeout_seconds": 15,
        },
        "daily_digest": {
            "timezone": "Asia/Shanghai",
            "default_send_time": "08:30",
            "default_window_mode": "previous_calendar_day",
            "default_rolling_hours": 24,
            "max_rolling_hours": 168,
            "scheduler_poll_seconds": 30,
        },
        "scheduled_incremental": {
            "timezone": "Asia/Shanghai",
            "default_send_times": ["08:30"],
            "scheduler_poll_seconds": 30,
        }
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
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value


def _mark_sources(value: Any, prefix: str, source: str, output: Dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _mark_sources(child, "{}.{}".format(prefix, key) if prefix else key, source, output)
    else:
        output[prefix] = source


def _set_nested(values: Dict[str, Any], parts: list, value: Any) -> bool:
    target: Any = values
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target or not isinstance(target[part], dict):
            return False
        target = target[part]
    if not isinstance(target, dict):
        return False
    target[parts[-1]] = value
    return True


class Settings:
    def __init__(self, values: Dict[str, Any], config_path: Path, sources: Optional[Dict[str, str]] = None):
        self.values = values
        self.config_path = config_path
        self.sources = sources or {}
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

    def source(self, *parts: str, default: str = "default") -> str:
        return self.sources.get(".".join(parts), default)

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
    sources: Dict[str, str] = {}
    _mark_sources(values, "", "default", sources)
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("app.json 顶层必须是 JSON 对象")
        values = _deep_merge(values, loaded)
        _mark_sources(loaded, "", "app.json", sources)

    for env_name, env_value in os.environ.items():
        if not env_name.startswith("PFTS_") or "__" not in env_name:
            continue
        parts = env_name[5:].lower().split("__")
        if len(parts) < 2 or parts[0] not in values:
            continue
        if _set_nested(values, parts, _parse_env_value(env_value)):
            sources[".".join(parts)] = "environment"
    return Settings(values, path, sources)
