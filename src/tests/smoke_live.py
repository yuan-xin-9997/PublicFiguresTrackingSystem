"""Run against a live local service: python tests/smoke_live.py."""
import os
import sys
import uuid

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
        digest_options = client.get("/api/v1/notifications/digests/options")
        digest_options.raise_for_status()
        assert "information_sources" in digest_options.json()
        suffix = uuid.uuid4().hex[:8]
        person = client.post("/api/v1/persons", json={
            "name": "冒烟测试人物-" + suffix,
            "native_name": "",
            "bio": "",
            "organization": "Smoke",
            "title": "测试",
            "country_region": "中国",
            "language": "zh-CN",
            "avatar_path": "",
            "enabled": True,
            "aliases": [],
        })
        person.raise_for_status()
        person_id = person.json()["id"]
        source = client.post("/api/v1/sources", json={
            "name": "冒烟人工来源-" + suffix,
            "type": "manual",
            "entry_url": "",
            "organization": "Smoke",
            "language": "zh-CN",
            "trust_level": 4,
            "schedule_seconds": 3600,
            "enabled": True,
            "person_ids": [person_id],
        })
        source.raise_for_status()
        document = client.post("/api/v1/documents/manual", json={
            "source_id": source.json()["id"],
            "title": "冒烟测试公开行程-" + suffix,
            "content_text": "2026年8月4日，冒烟测试人物-{}在北京出席公开测试活动。".format(suffix),
            "canonical_url": "https://example.com/smoke/" + suffix,
            "author": "smoke",
            "published_at": "2026-08-04T08:00:00+08:00",
        })
        document.raise_for_status()
        events = client.get("/api/v1/events", params={"person_id": person_id, "page_size": 10})
        events.raise_for_status()
        assert events.json()["total"] >= 1
        event_id = events.json()["items"][0]["id"]
        deleted = client.delete("/api/v1/events/{}".format(event_id))
        deleted.raise_for_status()
        after_delete = client.get("/api/v1/events/{}".format(event_id))
        assert after_delete.status_code == 404
        print("SMOKE_OK", ready.json()["status"], dashboard.json()["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
