import json
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet

from app.backend.database import Database
from app.backend.notifications import NotificationWorker, enqueue_task_run, save_email_overrides, save_rule
from app.backend.scheduled_incremental import (
    ScheduledIncrementalScheduler,
    build_incremental_message,
    create_incremental_run,
    effective_incremental_config,
    incremental_candidates,
    next_scheduled_at,
    normalize_incremental_config,
    normalize_send_times,
    preview_incremental,
)
from app.backend.security import utc_now


def _seed_scope(db):
    now = utc_now()
    person_id = db.execute(
        "INSERT INTO public_figures(name,created_at,updated_at) VALUES(?,?,?)",
        ("增量人物", now, now),
    )
    other_person = db.execute(
        "INSERT INTO public_figures(name,created_at,updated_at) VALUES(?,?,?)",
        ("范围外人物", now, now),
    )
    source_id = db.execute(
        "INSERT INTO information_sources(name,type,created_at,updated_at) VALUES(?,?,?,?)",
        ("增量来源", "manual", now, now),
    )
    task_id = db.execute(
        "INSERT INTO collection_tasks(name,source_id,created_at,updated_at) VALUES(?,?,?,?)",
        ("增量采集", source_id, now, now),
    )
    return person_id, other_person, task_id


def _seed_event(db, task_id, person_id, suffix, ingest_at, event_type="itinerary", review="approved"):
    run_id = db.execute(
        "INSERT INTO task_runs(task_id,status,started_at,finished_at,correlation_id) "
        "VALUES(?,'success',?,?,?)", (task_id, ingest_at, ingest_at, "inc-" + suffix),
    )
    event_id = db.execute(
        "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,location_name,"
        "confirmation_status,review_status,dedup_key,created_at,updated_at) "
        "VALUES(?,?,?,?,?,'北京','confirmed',?,?,?,?)",
        (
            person_id, event_type, "增量事件 " + suffix, "摘要 " + suffix,
            "2020-01-01T00:00:00+00:00", review, "inc-event-" + suffix,
            ingest_at, ingest_at,
        ),
    )
    db.execute(
        "INSERT INTO task_run_events(run_id,event_id,created_at) VALUES(?,?,?)",
        (run_id, event_id, ingest_at),
    )
    return run_id, event_id


def _enable_email(settings, db, monkeypatch, recipients=None, max_events=25, attempts=2):
    monkeypatch.setenv("PFTS_NOTIFICATION_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))
    save_email_overrides(
        db, settings, {
            "enabled": True, "smtp_host": "smtp.example.com", "smtp_port": 587,
            "security": "starttls", "from_address": "sender@example.com",
            "to_addresses": recipients or ["one@example.com"],
            "max_events_per_message": max_events, "max_attempts": attempts,
            "retry_base_seconds": 1,
        }, [], "", False, None,
    )


def test_incremental_config_validation_and_calendar_times(configured_app, monkeypatch):
    config, sources = effective_incremental_config(configured_app.state.settings)
    assert config == {
        "timezone": "Asia/Shanghai", "default_send_times": ["08:30"],
        "scheduler_poll_seconds": 30,
    }
    assert sources["timezone"] == "default"
    assert normalize_send_times(["20:30", "08:30", "08:30"]) == ["08:30", "20:30"]
    assert next_scheduled_at(
        ["08:30", "20:30"], "Asia/Shanghai",
        datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc),
    ).isoformat() == "2026-08-01T12:30:00+00:00"
    with pytest.raises(ValueError, match="HH:mm"):
        normalize_send_times([])
    with pytest.raises(ValueError, match="IANA"):
        normalize_incremental_config({"timezone": "Mars/Base"})
    with pytest.raises(ValueError, match="5 到 3600"):
        normalize_incremental_config({"scheduler_poll_seconds": 1})
    monkeypatch.setenv("PFTS_NOTIFICATIONS__SCHEDULED_INCREMENTAL__DEFAULT_SEND_TIMES", '["07:10","19:10"]')


def test_incremental_migration_upgrades_legacy_rules_and_indexes(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(str(path))
    connection.executescript("""
        CREATE TABLE schema_version(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
        CREATE TABLE notification_rules(
            id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,event_types_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        INSERT INTO notification_rules(name,event_types_json,enabled,created_at,updated_at)
        VALUES('旧规则','["itinerary"]',1,'2026-01-01','2026-01-01');
    """)
    connection.commit(); connection.close()
    db = Database(path)
    db.initialize(); db.initialize()
    rule = db.fetch_one("SELECT * FROM notification_rules WHERE name='旧规则'")
    assert rule["delivery_mode"] == "immediate"
    assert json.loads(rule["send_times_json"]) == []
    assert db.fetch_one("SELECT MAX(version) version FROM schema_version")["version"] == 7
    indexes = {row["name"] for row in db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    assert {"idx_notification_rules_due", "idx_task_run_events_watermark",
            "idx_scheduled_batches_due"} <= indexes


def test_rule_lifecycle_watermark_and_immediate_compatibility(configured_app, monkeypatch):
    db = configured_app.state.db; settings = configured_app.state.settings; db.initialize()
    person, _, task = _seed_scope(db)
    first_run, first_event = _seed_event(db, task, person, "before", "2026-08-01T00:00:00+00:00")
    scheduled = save_rule(
        db, "定时规则", [task], ["itinerary"], True, person_ids=[person],
        delivery_mode="scheduled_incremental", send_times=["20:30", "08:30"], settings=settings,
    )
    assert scheduled["cursor_reset"] is True
    assert scheduled["send_times"] == ["08:30", "20:30"]
    assert scheduled["cursor"]["event_id"] == first_event
    preserved = save_rule(
        db, "改名", [task], ["itinerary"], True, rule_id=scheduled["id"],
        person_ids=[person], delivery_mode="scheduled_incremental", send_times=["09:00"],
        settings=settings,
    )
    assert preserved["cursor_reset"] is False
    assert preserved["cursor"]["event_id"] == first_event
    disabled = save_rule(
        db, "改名", [task], ["itinerary"], False, rule_id=scheduled["id"],
        person_ids=[person], delivery_mode="scheduled_incremental", send_times=["09:00"],
        settings=settings,
    )
    _seed_event(db, task, person, "disabled", "2026-08-01T00:01:00+00:00")
    enabled = save_rule(
        db, "改名", [task], ["itinerary"], True, rule_id=scheduled["id"],
        person_ids=[person], delivery_mode="scheduled_incremental", send_times=["09:00"],
        settings=settings,
    )
    assert disabled["next_run_at"] is None and enabled["cursor_reset"] is True

    _enable_email(settings, db, monkeypatch)
    immediate = save_rule(db, "即时", [task], ["itinerary"], True, settings=settings)
    run_id, _ = _seed_event(db, task, person, "instant", "2026-08-01T00:02:00+00:00")
    assert immediate["delivery_mode"] == "immediate"
    assert enqueue_task_run(db, settings, run_id)["candidates"] == 1


def test_incremental_candidates_boundaries_scope_and_preview(configured_app):
    db = configured_app.state.db; settings = configured_app.state.settings; db.initialize()
    person, other, task = _seed_scope(db)
    rule = save_rule(
        db, "窗口", [task], ["itinerary"], True, person_ids=[person],
        delivery_mode="scheduled_incremental", send_times=["08:30"], settings=settings,
    )
    lower = (rule["cursor_created_at"], rule["cursor_run_id"], rule["cursor_event_id"])
    first_run, first = _seed_event(db, task, person, "same-a", "2026-08-01T01:00:00+00:00")
    second_run, second = _seed_event(db, task, person, "same-b", "2026-08-01T01:00:00+00:00")
    _seed_event(db, task, other, "other-person", "2026-08-01T01:01:00+00:00")
    _seed_event(db, task, person, "rejected", "2026-08-01T01:02:00+00:00", review="rejected")
    upper = ("2026-08-01T01:00:00+00:00", second_run, second)
    rows = incremental_candidates(db, rule["id"], lower, upper)
    assert {row["id"] for row in rows} == {first, second}
    preview = preview_incremental(db, rule["id"])
    assert preview["candidate_count"] == 2
    unchanged = db.fetch_one("SELECT cursor_event_id FROM notification_rules WHERE id=?", (rule["id"],))
    assert unchanged["cursor_event_id"] == lower[2]


def test_run_snapshot_empty_idempotency_message_and_worker(configured_app, monkeypatch):
    db = configured_app.state.db; settings = configured_app.state.settings; db.initialize()
    person, _, task = _seed_scope(db)
    _enable_email(settings, db, monkeypatch, recipients=["one@example.com", "two@example.com"], max_events=1)
    rule = save_rule(
        db, "定时汇总", [task], ["itinerary"], True, person_ids=[person],
        delivery_mode="scheduled_incremental", send_times=["08:30"], settings=settings,
    )
    _seed_event(db, task, person, "late-old-date", "2026-08-01T02:00:00+00:00")
    planned = datetime(2026, 8, 1, 2, 30, tzinfo=timezone.utc)
    first = create_incremental_run(db, settings, rule["id"], planned)
    second = create_incremental_run(db, settings, rule["id"], planned)
    assert first["id"] == second["id"] and first["candidate_count"] == 1
    assert db.fetch_one("SELECT COUNT(*) n FROM scheduled_notification_batches")["n"] == 2
    batch = db.fetch_one("SELECT * FROM scheduled_notification_batches ORDER BY id")
    config = {
        "subject_prefix": "[PFTS]", "from_name": "", "from_address": "sender@example.com"
    }
    message, deliverable, skipped = build_incremental_message(db, settings, batch["id"], config)
    assert isinstance(message, EmailMessage)
    plain = message.get_body(preferencelist=("plain",)).get_content()
    assert "入库增量窗口" in plain and "late-old-date" in plain
    assert deliverable and not skipped
    stable = message["Message-ID"]
    sent = []
    monkeypatch.setattr("app.backend.notifications.send_message", lambda cfg, msg: sent.append(msg))
    result = NotificationWorker(db, settings).process_incremental_once()
    assert result["kind"] == "scheduled_incremental" and result["status"] == "sent"
    assert sent[0]["Message-ID"] == stable
    empty = create_incremental_run(
        db, settings, rule["id"], datetime(2026, 8, 1, 3, 30, tzinfo=timezone.utc)
    )
    assert empty["status"] == "empty" and empty["batch_count"] == 0


def test_two_consecutive_watermarks_have_no_gap_or_overlap(configured_app, monkeypatch):
    db = configured_app.state.db; settings = configured_app.state.settings; db.initialize()
    person, _, task = _seed_scope(db)
    _enable_email(settings, db, monkeypatch)
    rule = save_rule(
        db, "连续窗口", [task], ["itinerary"], True, person_ids=[person],
        delivery_mode="scheduled_incremental", send_times=["08:30"], settings=settings,
    )
    _, first_event = _seed_event(db, task, person, "window-one", "2026-08-01T05:00:00+00:00")
    first = create_incremental_run(
        db, settings, rule["id"], datetime(2026, 8, 1, 5, 30, tzinfo=timezone.utc)
    )
    _, second_event = _seed_event(db, task, person, "window-two", "2026-08-01T06:00:00+00:00")
    second = create_incremental_run(
        db, settings, rule["id"], datetime(2026, 8, 1, 6, 30, tzinfo=timezone.utc)
    )
    assert (first["upper_created_at"], first["upper_run_id"], first["upper_event_id"]) == (
        second["lower_created_at"], second["lower_run_id"], second["lower_event_id"]
    )
    first_items = {row["event_id"] for row in db.fetch_all(
        "SELECT DISTINCT event_id FROM scheduled_notification_items WHERE run_id=?", (first["id"],)
    )}
    second_items = {row["event_id"] for row in db.fetch_all(
        "SELECT DISTINCT event_id FROM scheduled_notification_items WHERE run_id=?", (second["id"],)
    )}
    assert first_items == {first_event}
    assert second_items == {second_event}
    assert first_items.isdisjoint(second_items)


def test_scheduler_recovers_latest_due_and_worker_failure_retry(configured_app, monkeypatch):
    db = configured_app.state.db; settings = configured_app.state.settings; db.initialize()
    person, _, task = _seed_scope(db)
    _enable_email(settings, db, monkeypatch, attempts=1)
    rule = save_rule(
        db, "恢复", [task], ["itinerary"], True, person_ids=[person],
        delivery_mode="scheduled_incremental", send_times=["08:30", "20:30"], settings=settings,
    )
    _seed_event(db, task, person, "recover", "2026-08-01T00:00:00+00:00")
    db.execute(
        "UPDATE notification_rules SET next_run_at=? WHERE id=?",
        ("2026-07-30T00:30:00+00:00", rule["id"]),
    )
    results = ScheduledIncrementalScheduler(db, settings).process_due_once(
        datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
    )
    assert len(results) == 1
    assert results[0]["scheduled_at"] == "2026-08-01T12:30:00+00:00"
    monkeypatch.setattr(
        "app.backend.notifications.send_message",
        lambda cfg, msg: (_ for _ in ()).throw(TimeoutError("to@example.com failed")),
    )
    failed = NotificationWorker(db, settings).process_incremental_once()
    assert failed["status"] == "failed" and "[email]" in failed["error"]


def test_incremental_api_permissions_preview_run_filters_and_retry(
    admin_client, configured_app, monkeypatch
):
    db = configured_app.state.db; settings = configured_app.state.settings
    _enable_email(settings, db, monkeypatch)
    person, _, task = _seed_scope(db)
    created = admin_client.post("/api/v1/notifications/rules", json={
        "name": "API 定时", "task_ids": [task], "person_ids": [person],
        "event_types": ["itinerary"], "enabled": True,
        "delivery_mode": "scheduled_incremental", "send_times": ["08:30", "20:30"],
    })
    assert created.status_code == 201, created.text
    rule = created.json()
    _seed_event(db, task, person, "api", "2026-08-01T04:00:00+00:00")
    preview = admin_client.post("/api/v1/notifications/rules/{}/preview".format(rule["id"]))
    assert preview.status_code == 200 and preview.json()["candidate_count"] == 1
    run = admin_client.post("/api/v1/notifications/rules/{}/run-now".format(rule["id"]))
    assert run.status_code == 200, run.text
    listed = admin_client.get(
        "/api/v1/notifications/incremental/runs?rule_id={}&run_status=pending".format(rule["id"])
    )
    assert listed.status_code == 200 and listed.json()["total"] == 1
    detail = admin_client.get(
        "/api/v1/notifications/incremental/runs/{}".format(run.json()["id"])
    )
    assert detail.status_code == 200 and detail.json()["batches"]
    batch_id = detail.json()["batches"][0]["id"]
    db.execute("UPDATE scheduled_notification_batches SET status='failed' WHERE id=?", (batch_id,))
    assert admin_client.post(
        "/api/v1/notifications/incremental/batches/{}/retry".format(batch_id)
    ).status_code == 200
    audits = admin_client.get("/api/v1/audit-logs").json()["items"]
    assert any(item["object_type"] == "scheduled_notification_run" for item in audits)

    users = admin_client.get("/api/v1/users").json()["items"]
    analyst = next(item for item in users if item["username"] == "analyst")
    admin_client.put("/api/v1/users/{}/permissions".format(analyst["id"]), json={"pages": ["notifications"]})
    admin_client.post("/api/v1/auth/logout")
    admin_client.post("/api/v1/auth/login", json={"username": "analyst", "password": "reader123"})
    assert admin_client.get("/api/v1/notifications/incremental/runs").status_code == 200
    assert admin_client.post("/api/v1/notifications/rules/{}/run-now".format(rule["id"])).status_code == 403
