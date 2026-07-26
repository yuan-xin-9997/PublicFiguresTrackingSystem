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
        print("SMOKE_OK", ready.json()["status"], dashboard.json()["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
