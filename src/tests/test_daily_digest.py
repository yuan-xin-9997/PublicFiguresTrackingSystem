from datetime import date, datetime, timezone
from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet

from app.backend.daily_digest import (
    DailyDigestScheduler,
    build_digest_message,
    create_digest_run,
    digest_window,
    effective_digest_config,
    next_scheduled_at,
    normalize_digest_config,
    preview_digest,
    save_digest_rule,
)
from app.backend.config import load_config
from app.backend.notifications import (
    NotificationWorker,
    save_email_overrides,
)
from app.backend.security import utc_now


def _seed_digest_data(db):
    now = utc_now()
    first_person = db.execute(
        "INSERT INTO public_figures(name,created_at,updated_at) VALUES(?,?,?)",
        ("甲人物", now, now),
    )
    second_person = db.execute(
        "INSERT INTO public_figures(name,created_at,updated_at) VALUES(?,?,?)",
        ("乙人物", now, now),
    )
    source_id = db.execute(
        "INSERT INTO information_sources(name,type,created_at,updated_at) VALUES(?,?,?,?)",
        ("日报来源", "manual", now, now),
    )
    document_id = db.execute(
        "INSERT INTO raw_documents(source_id,title,collected_at,content_text,content_hash) "
        "VALUES(?,?,?,?,?)",
        (source_id, "日报证据", now, "公开材料", "digest-document"),
    )

    def event(person_id, event_type, title, start_at, created_at, review_status="approved"):
        event_id = db.execute(
            "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,"
            "location_name,confirmation_status,review_status,dedup_key,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'confirmed',?,?,?,?)",
            (
                person_id, event_type, title, "摘要-" + title, start_at, "北京",
                review_status, "digest-" + title, created_at, created_at,
            ),
        )
        db.execute(
            "INSERT INTO event_evidence(event_id,document_id,evidence_text,evidence_locator) "
            "VALUES(?,?,?,?)",
            (event_id, document_id, "证据-" + title, "正文"),
        )
        return event_id

    late = event(
        first_person, "statement", "晚事件",
        "2026-07-29T08:00:00+00:00", "2026-07-29T08:05:00+00:00",
    )
    early = event(
        first_person, "itinerary", "早事件",
        "2026-07-29T01:00:00+00:00", "2026-07-29T01:05:00+00:00",
    )
    unknown = event(
        first_person, "statement", "未知时间", None, "2026-07-29T05:00:00+00:00",
    )
    event(
        second_person, "itinerary", "其他人物",
        "2026-07-29T02:00:00+00:00", "2026-07-29T02:05:00+00:00",
    )
    event(
        first_person, "itinerary", "已驳回",
        "2026-07-29T03:00:00+00:00", "2026-07-29T03:05:00+00:00", "rejected",
    )
    return first_person, second_person, [early, late, unknown]


def _enable_email(settings, db, monkeypatch, max_attempts=2):
    monkeypatch.setenv(
        "PFTS_NOTIFICATION_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii")
    )
    save_email_overrides(
        db, settings,
        {
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "security": "starttls",
            "from_address": "sender@example.com",
            "to_addresses": ["legacy@example.com"],
            "max_attempts": max_attempts,
            "retry_base_seconds": 1,
        },
        [], "", False, None,
    )


def test_digest_config_and_calendar_window_defaults(configured_app):
    settings = configured_app.state.settings
    config, sources = effective_digest_config(settings)
    assert config["timezone"] == "Asia/Shanghai"
    assert config["default_send_time"] == "08:30"
    assert config["default_window_mode"] == "previous_calendar_day"
    assert sources["default_send_time"] == "default"

    scheduled, start, end = digest_window(
        date(2026, 7, 30), "08:30", "previous_calendar_day", 24
    )
    assert scheduled.isoformat() == "2026-07-30T00:30:00+00:00"
    assert start.isoformat() == "2026-07-28T16:00:00+00:00"
    assert end.isoformat() == "2026-07-29T16:00:00+00:00"

    _, rolling_start, rolling_end = digest_window(
        date(2026, 7, 30), "08:30", "rolling_hours", 12
    )
    assert rolling_start.isoformat() == "2026-07-29T12:30:00+00:00"
    assert rolling_end.isoformat() == "2026-07-30T00:30:00+00:00"
    assert next_scheduled_at(
        "21:15", after=datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)
    ).isoformat() == "2026-07-31T13:15:00+00:00"


def test_digest_environment_overrides_are_tracked(tmp_path, monkeypatch):
    config_path = tmp_path / "app.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(
        "PFTS_NOTIFICATIONS__DAILY_DIGEST__DEFAULT_SEND_TIME", "07:20"
    )
    monkeypatch.setenv(
        "PFTS_NOTIFICATIONS__DAILY_DIGEST__DEFAULT_ROLLING_HOURS", "18"
    )
    settings = load_config(str(config_path))
    config, sources = effective_digest_config(settings)
    assert config["default_send_time"] == "07:20"
    assert config["default_rolling_hours"] == 18
    assert sources["default_send_time"] == "environment"


@pytest.mark.parametrize("values", [
    {"default_send_time": "24:00"},
    {"timezone": "Missing/Timezone"},
    {"default_window_mode": "bad"},
    {"default_rolling_hours": 200, "max_rolling_hours": 168},
])
def test_digest_config_rejects_invalid_values(values):
    with pytest.raises(ValueError):
        normalize_digest_config(values)


def test_digest_rule_preview_selection_order_and_idempotent_run(configured_app):
    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    first_person, _, expected_ids = _seed_digest_data(db)
    rule = save_digest_rule(
        db, settings, "每日摘要", [first_person], ["itinerary", "statement"],
        ["me@example.com"], send_time="08:30",
        window_mode="previous_calendar_day",
    )
    db.execute(
        "UPDATE daily_digest_rules SET enabled_at=? WHERE id=?",
        ("2026-07-01T00:00:00+00:00", rule["id"]),
    )
    preview = preview_digest(db, settings, rule["id"], date(2026, 7, 30))
    assert preview["candidate_count"] == 3
    assert [item["id"] for item in preview["sample"]] == expected_ids

    first = create_digest_run(
        db, settings, rule["id"], date(2026, 7, 30), trigger_type="manual"
    )
    second = create_digest_run(
        db, settings, rule["id"], date(2026, 7, 30), trigger_type="manual"
    )
    assert first["id"] == second["id"]
    assert db.fetch_one("SELECT COUNT(*) n FROM daily_digest_runs")["n"] == 1
    assert db.fetch_one("SELECT COUNT(*) n FROM daily_digest_batches")["n"] == 1
    items = db.fetch_all(
        "SELECT event_id FROM daily_digest_items ORDER BY id"
    )
    assert [item["event_id"] for item in items] == expected_ids


def test_digest_message_worker_success_empty_and_retry(configured_app, monkeypatch):
    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    first_person, _, _ = _seed_digest_data(db)
    _enable_email(settings, db, monkeypatch)
    rule = save_digest_rule(
        db, settings, "早报", [first_person], ["itinerary", "statement"],
        ["me@example.com"], send_time="08:30",
    )
    db.execute(
        "UPDATE daily_digest_rules SET enabled_at=? WHERE id=?",
        ("2026-07-01T00:00:00+00:00", rule["id"]),
    )
    run = create_digest_run(db, settings, rule["id"], date(2026, 7, 30), "manual")
    batch = db.fetch_one(
        "SELECT * FROM daily_digest_batches WHERE run_id=?", (run["id"],)
    )
    email_config = {
        **settings.get("notifications", "email"),
        "from_address": "sender@example.com",
        "from_name": "PFTS",
    }
    message, deliverable, skipped = build_digest_message(
        db, settings, batch["id"], email_config
    )
    assert isinstance(message, EmailMessage)
    plain = message.get_body(preferencelist=("plain",)).get_content()
    assert plain.index("早事件") < plain.index("晚事件") < plain.index("未知时间")
    assert "日报来源" in plain
    assert "时间未知" in plain
    assert deliverable and not skipped
    stable_id = message["Message-ID"]

    sent = []
    monkeypatch.setattr(
        "app.backend.notifications.send_message",
        lambda config, outgoing: sent.append(outgoing),
    )
    result = NotificationWorker(db, settings).process_once()
    assert result["kind"] == "daily_digest"
    assert result["status"] == "sent"
    assert sent[0]["Message-ID"] == stable_id
    assert db.fetch_one(
        "SELECT status FROM daily_digest_runs WHERE id=?", (run["id"],)
    )["status"] == "sent"

    empty_rule = save_digest_rule(
        db, settings, "空日报", [first_person], ["other"], ["empty@example.com"],
        send_when_empty=False,
    )
    db.execute(
        "UPDATE daily_digest_rules SET enabled_at=? WHERE id=?",
        ("2026-07-01T00:00:00+00:00", empty_rule["id"]),
    )
    empty_run = create_digest_run(
        db, settings, empty_rule["id"], date(2026, 7, 30), "manual"
    )
    assert empty_run["status"] == "empty"
    assert db.fetch_one(
        "SELECT COUNT(*) n FROM daily_digest_batches WHERE run_id=?",
        (empty_run["id"],),
    )["n"] == 0

    retry_rule = save_digest_rule(
        db, settings, "重试日报", [first_person], ["itinerary"],
        ["retry@example.com"],
    )
    db.execute(
        "UPDATE daily_digest_rules SET enabled_at=? WHERE id=?",
        ("2026-07-01T00:00:00+00:00", retry_rule["id"]),
    )
    retry_run = create_digest_run(
        db, settings, retry_rule["id"], date(2026, 7, 30), "manual"
    )
    monkeypatch.setattr(
        "app.backend.notifications.send_message",
        lambda config, outgoing: (_ for _ in ()).throw(TimeoutError("failed")),
    )
    failed = NotificationWorker(db, settings).process_once()
    assert failed["run_id"] == retry_run["id"]
    assert failed["status"] == "retrying"
    retry_batch = db.fetch_one(
        "SELECT id FROM daily_digest_batches WHERE run_id=?", (retry_run["id"],)
    )
    db.execute(
        "UPDATE daily_digest_batches SET next_attempt_at=? WHERE id=?",
        (utc_now(), retry_batch["id"]),
    )
    terminal = NotificationWorker(db, settings).process_once()
    assert terminal["status"] == "failed"
    assert db.fetch_one(
        "SELECT status FROM daily_digest_runs WHERE id=?", (retry_run["id"],)
    )["status"] == "failed"

    db.execute(
        "UPDATE daily_digest_batches SET status='sending' WHERE id=?",
        (retry_batch["id"],),
    )
    monkeypatch.setattr(
        "app.backend.notifications.NotificationWorker._loop", lambda self: None
    )
    worker = NotificationWorker(db, settings)
    worker.start()
    worker.stop()
    assert db.fetch_one(
        "SELECT status FROM daily_digest_batches WHERE id=?", (retry_batch["id"],)
    )["status"] == "retrying"


def test_digest_scheduler_recovers_only_latest_due_date(configured_app):
    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    first_person, _, _ = _seed_digest_data(db)
    rule = save_digest_rule(
        db, settings, "恢复日报", [first_person], ["itinerary"],
        ["me@example.com"], send_time="08:30",
    )
    db.execute(
        "UPDATE daily_digest_rules SET enabled_at=?,next_run_at=? WHERE id=?",
        (
            "2026-07-01T00:00:00+00:00",
            "2026-07-30T00:30:00+00:00",
            rule["id"],
        ),
    )
    results = DailyDigestScheduler(db, settings).process_due_once(
        datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)
    )
    assert len(results) == 1
    assert results[0]["scheduled_date"] == "2026-08-01"
    assert results[0]["missed_count"] == 2
    assert db.fetch_one("SELECT COUNT(*) n FROM daily_digest_runs")["n"] == 1
    assert DailyDigestScheduler(db, settings).process_due_once(
        datetime(2026, 8, 1, 1, 1, tzinfo=timezone.utc)
    ) == []


def test_digest_api_permissions_preview_and_run(
    admin_client, configured_app, monkeypatch
):
    db = configured_app.state.db
    person_id, _, _ = _seed_digest_data(db)
    created = admin_client.post("/api/v1/notifications/digests/rules", json={
        "name": "API 日报",
        "person_ids": [person_id],
        "event_types": ["itinerary", "statement"],
        "recipients": ["me@example.com"],
        "send_time": "09:45",
        "window_mode": "previous_calendar_day",
        "send_when_empty": False,
        "enabled": True,
    })
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["send_time"] == "09:45"
    assert admin_client.get(
        "/api/v1/notifications/digests/rules"
    ).json()["total"] == 1
    preview = admin_client.post(
        "/api/v1/notifications/digests/rules/{}/preview".format(rule["id"]),
        json={"scheduled_date": "2026-07-30"},
    )
    assert preview.status_code == 200
    assert preview.json()["candidate_count"] == 3

    db.execute(
        "UPDATE daily_digest_rules SET enabled_at=? WHERE id=?",
        ("2026-07-01T00:00:00+00:00", rule["id"]),
    )
    run = admin_client.post(
        "/api/v1/notifications/digests/rules/{}/runs".format(rule["id"]),
        json={"scheduled_date": "2026-07-30"},
    )
    assert run.status_code == 200, run.text
    assert admin_client.get(
        "/api/v1/notifications/digests/runs"
    ).json()["total"] == 1
    audits = admin_client.get("/api/v1/audit-logs").json()["items"]
    assert any(item["object_type"] == "daily_digest_rule" for item in audits)
    assert any(item["object_type"] == "daily_digest_run" for item in audits)

    users = admin_client.get("/api/v1/users").json()["items"]
    analyst = next(item for item in users if item["username"] == "analyst")
    admin_client.put(
        "/api/v1/users/{}/permissions".format(analyst["id"]),
        json={"pages": ["notifications"]},
    )
    admin_client.post("/api/v1/auth/logout")
    assert admin_client.post(
        "/api/v1/auth/login",
        json={"username": "analyst", "password": "reader123"},
    ).status_code == 200
    listed = admin_client.get("/api/v1/notifications/digests/rules")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["recipients"] == ["m***@example.com"]
    assert admin_client.post(
        "/api/v1/notifications/digests/rules/{}/preview".format(rule["id"]),
        json={"scheduled_date": "2026-07-30"},
    ).status_code == 403
    assert admin_client.post("/api/v1/notifications/digests/rules", json={
        "name": "越权", "person_ids": [person_id], "event_types": ["itinerary"],
        "recipients": ["x@example.com"],
    }).status_code == 403
    admin_client.post("/api/v1/auth/logout")
    assert admin_client.post(
        "/api/v1/auth/login",
        json={"username": "analyst", "password": "reader123"},
    ).status_code == 200
    configured_app.state.db.execute(
        "DELETE FROM page_permissions WHERE user_id=(SELECT id FROM users WHERE username='analyst')"
    )
    assert admin_client.get(
        "/api/v1/notifications/digests/rules"
    ).status_code == 403
