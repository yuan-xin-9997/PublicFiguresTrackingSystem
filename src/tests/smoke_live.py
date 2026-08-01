"""Run against a live local service: python tests/smoke_live.py."""
import os
import sys

import httpx


BASE_URL = os.getenv("PFTS_SMOKE_URL", "http://127.0.0.1:28000")


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        ready = client.get("/api/v1/health/ready")
        ready.raise_for_status()
        login = client.post("/api/v1/auth/login", json={
            "username": os.getenv("PFTS_SMOKE_USER", "admin"),
            "password": os.getenv("PFTS_SMOKE_PASSWORD", "admin123"),
        })
        login.raise_for_status()
        dashboard = client.get("/api/v1/dashboard/summary")
        dashboard.raise_for_status()
        email_config = client.get("/api/v1/notifications/email/config")
        email_config.raise_for_status()
        notification_payload = email_config.json()["config"]
        assert "password" not in notification_payload
        assert isinstance(notification_payload.get("enabled"), bool)
        digest_config = client.get("/api/v1/notifications/digests/config")
        digest_config.raise_for_status()
        digest_payload = digest_config.json()["config"]
        assert digest_payload["default_send_time"] == "08:30"
        assert digest_payload["default_window_mode"] == "previous_calendar_day"
        digest_rules = client.get("/api/v1/notifications/digests/rules")
        digest_rules.raise_for_status()
        digest_runs = client.get("/api/v1/notifications/digests/runs?page_size=1")
        digest_runs.raise_for_status()
        incremental_config = client.get("/api/v1/notifications/incremental/config")
        incremental_config.raise_for_status()
        incremental_payload = incremental_config.json()["config"]
        assert incremental_payload["timezone"] == "Asia/Shanghai"
        assert incremental_payload["default_send_times"]
        incremental_runs = client.get("/api/v1/notifications/incremental/runs?page_size=1")
        incremental_runs.raise_for_status()
        print("SMOKE_OK", ready.json()["status"], dashboard.json()["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
