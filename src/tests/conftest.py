import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from app.backend.main import create_app


@pytest.fixture()
def configured_app(tmp_path, monkeypatch):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    config_dir = tmp_path / "config"
    data.mkdir(); logs.mkdir(); config_dir.mkdir()
    (data / "password.txt").write_text(
        "admin:admin123:admin\nanalyst:reader123:user\n", encoding="utf-8"
    )
    config = {
        "server": {"host": "127.0.0.1", "port": 8000},
        "database": {"path": str(data / "test.sqlite3"), "busy_timeout_ms": 5000},
        "security": {
            "password_file": str(data / "password.txt"), "session_hours": 1,
            "cookie_secure": False, "login_max_attempts": 4, "login_window_seconds": 60,
        },
        "tasks": {"scheduler_enabled": False, "max_items_per_run": 10},
        "collector": {"allow_private_hosts": False, "timeout_seconds": 2, "max_response_bytes": 100000},
        "ai": {"provider": "local", "model": "local-rules-v1", "review_threshold": 0.7},
        "logging": {"path": str(logs / "app.log"), "level": "WARNING", "retention_days": 2},
    }
    config_path = config_dir / "app.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return create_app(str(config_path))


@pytest.fixture()
def client(configured_app):
    with TestClient(configured_app) as test_client:
        yield test_client


@pytest.fixture()
def admin_client(client):
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200
    return client

