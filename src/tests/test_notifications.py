from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet

from app.backend.database import Database
from app.backend.notifications import (
    NotificationWorker,
    build_batch_message,
    effective_email_config,
    enqueue_task_run,
    save_email_overrides,
    save_rule,
    send_message,
)
from app.backend.security import utc_now
from app.backend.services import analyze_document, insert_document


def _seed_delivery_data(db, event_count=2):
    now = utc_now()
    person_id = db.execute(
        "INSERT INTO public_figures(name,created_at,updated_at) VALUES(?,?,?)",
        ("测试人物", now, now),
    )
    source_id = db.execute(
        "INSERT INTO information_sources(name,type,created_at,updated_at) VALUES(?,?,?,?)",
        ("测试来源", "manual", now, now),
    )
    task_id = db.execute(
        "INSERT INTO collection_tasks(name,source_id,created_at,updated_at) VALUES(?,?,?,?)",
        ("测试采集", source_id, now, now),
    )
    run_id = db.execute(
        "INSERT INTO task_runs(task_id,status,started_at,finished_at,correlation_id) VALUES(?,'success',?,?,?)",
        (task_id, now, now, "test-run"),
    )
    event_ids = []
    for index in range(event_count):
        event_id = db.execute(
            "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,location_name,"
            "confirmation_status,review_status,dedup_key,created_at,updated_at) "
            "VALUES(?,'itinerary',?,?,?,?,?,'approved',?,?,?)",
            (
                person_id, "新增事件 {}".format(index + 1), "公开活动摘要",
                "2026-07-26T02:00:00+00:00", "北京", "confirmed",
                "notification-test-{}".format(index), now, now,
            ),
        )
        db.execute(
            "INSERT INTO task_run_events(run_id,event_id,created_at) VALUES(?,?,?)",
            (run_id, event_id, now),
        )
        event_ids.append(event_id)
    return task_id, run_id, event_ids


def _enable_email(settings, db, monkeypatch, max_events=25, max_attempts=2):
    monkeypatch.setenv("PFTS_NOTIFICATION_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))
    save_email_overrides(
        db,
        settings,
        {
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "security": "starttls",
            "from_address": "sender@example.com",
            "to_addresses": ["one@example.com", "two@example.com"],
            "max_events_per_message": max_events,
            "max_attempts": max_attempts,
            "retry_base_seconds": 1,
        },
        [],
        "",
        False,
        None,
    )


def test_notification_migration_is_repeatable_and_preserves_existing_rows(tmp_path):
    db = Database(tmp_path / "migration.sqlite3")
    db.initialize()
    db.execute(
        "INSERT INTO public_figures(name,created_at,updated_at) VALUES(?,?,?)",
        ("保留人物", utc_now(), utc_now()),
    )
    db.initialize()
    assert db.fetch_one("SELECT name FROM public_figures")["name"] == "保留人物"
    assert db.fetch_one("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")["version"] == 5
    expected = {
        "notification_settings", "notification_rules", "notification_rule_tasks",
        "notification_rule_persons",
        "task_run_events", "email_delivery_batches", "email_delivery_items",
    }
    actual = {row["name"] for row in db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert expected <= actual


def test_page_config_encrypts_password_and_clear_falls_back(configured_app, monkeypatch):
    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("PFTS_NOTIFICATION_CREDENTIAL_KEY", key)
    _, sources = save_email_overrides(
        db,
        settings,
        {"smtp_host": "smtp.page.example", "from_address": "sender@example.com", "to_addresses": ["to@example.com"]},
        [],
        "page-secret",
        False,
        None,
    )
    row = db.fetch_one("SELECT * FROM notification_settings WHERE id=1")
    assert "page-secret" not in row["password_ciphertext"]
    secret_config, _ = effective_email_config(settings, db, include_secret=True)
    assert secret_config["password"] == "page-secret"
    assert sources["smtp_host"] == "page"

    save_email_overrides(db, settings, {}, ["smtp_host"], "", True, None)
    inherited, inherited_sources = effective_email_config(settings, db)
    assert inherited["smtp_host"] == ""
    assert inherited["password_configured"] is False
    assert inherited_sources["smtp_host"] in {"default", "app.json"}


def test_page_password_requires_external_key(configured_app, monkeypatch):
    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    monkeypatch.delenv("PFTS_NOTIFICATION_CREDENTIAL_KEY", raising=False)
    with pytest.raises(ValueError, match="加密主密钥"):
        save_email_overrides(db, settings, {}, [], "secret", False, None)


def test_outbox_filters_chunks_and_deduplicates_overlapping_rules(configured_app, monkeypatch):
    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    task_id, run_id, event_ids = _seed_delivery_data(db, 2)
    _enable_email(settings, db, monkeypatch, max_events=1)
    save_rule(db, "规则一", [task_id], ["itinerary"], True)
    save_rule(db, "规则二", [task_id], ["itinerary", "statement"], True)

    first = enqueue_task_run(db, settings, run_id)
    second = enqueue_task_run(db, settings, run_id)
    assert first == {"candidates": 2, "enqueued": 4, "skipped": 0, "batches": 4}
    assert second["enqueued"] == 0
    assert db.fetch_one("SELECT COUNT(*) n FROM email_delivery_items")["n"] == 4
    assert db.fetch_one("SELECT COUNT(*) n FROM email_delivery_batches")["n"] == 4
    assert {row["event_id"] for row in db.fetch_all("SELECT event_id FROM email_delivery_items")} == set(event_ids)


def test_outbox_person_filter_is_optional_and_limits_matching(configured_app, monkeypatch):
    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    task_id, run_id, event_ids = _seed_delivery_data(db, 1)
    first_person = db.fetch_one("SELECT person_id FROM timeline_events WHERE id=?", (event_ids[0],))["person_id"]
    now = utc_now()
    second_person = db.execute(
        "INSERT INTO public_figures(name,created_at,updated_at) VALUES(?,?,?)",
        ("第二人物", now, now),
    )
    second_event = db.execute(
        "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,location_name,"
        "confirmation_status,review_status,dedup_key,created_at,updated_at) "
        "VALUES(?,'itinerary',?,?,?,?,?,'approved',?,?,?)",
        (
            second_person, "第二人物事件", "摘要", "2026-07-26T03:00:00+00:00",
            "上海", "confirmed", "notification-person-second", now, now,
        ),
    )
    db.execute(
        "INSERT INTO task_run_events(run_id,event_id,created_at) VALUES(?,?,?)",
        (run_id, second_event, now),
    )
    _enable_email(settings, db, monkeypatch)

    all_rule = save_rule(db, "全部人物", [task_id], ["itinerary"], True, person_ids=[])
    assert all_rule["person_ids"] == []
    assert enqueue_task_run(db, settings, run_id)["candidates"] == 2

    db.execute("DELETE FROM email_delivery_batches")
    db.execute("DELETE FROM notification_rules")
    selected_rule = save_rule(
        db, "指定人物", [task_id], ["itinerary"], True, person_ids=[first_person],
    )
    assert selected_rule["person_ids"] == [first_person]
    assert enqueue_task_run(db, settings, run_id)["candidates"] == 1
    assert {
        row["event_id"] for row in db.fetch_all("SELECT event_id FROM email_delivery_items")
    } == {event_ids[0]}


def test_outbox_skips_selected_person_after_person_becomes_unavailable(configured_app, monkeypatch):
    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    task_id, run_id, event_ids = _seed_delivery_data(db, 1)
    person_id = db.fetch_one("SELECT person_id FROM timeline_events WHERE id=?", (event_ids[0],))["person_id"]
    _enable_email(settings, db, monkeypatch)
    save_rule(db, "指定人物", [task_id], ["itinerary"], True, person_ids=[person_id])
    db.execute(
        "UPDATE public_figures SET enabled=0,deleted_at=?,updated_at=? WHERE id=?",
        (utc_now(), utc_now(), person_id),
    )

    assert enqueue_task_run(db, settings, run_id) == {
        "candidates": 0, "enqueued": 0, "skipped": 0, "batches": 0,
    }


def test_analyze_records_only_events_first_created_by_task_run(configured_app):
    db = configured_app.state.db
    db.initialize()
    now = utc_now()
    person_id = db.execute(
        "INSERT INTO public_figures(name,created_at,updated_at) VALUES(?,?,?)",
        ("张三", now, now),
    )
    source_id = db.execute(
        "INSERT INTO information_sources(name,type,language,created_at,updated_at) VALUES(?,?,?,?,?)",
        ("人工来源", "manual", "zh-CN", now, now),
    )
    db.execute("INSERT INTO source_persons(source_id,person_id) VALUES(?,?)", (source_id, person_id))
    task_id = db.execute(
        "INSERT INTO collection_tasks(name,source_id,created_at,updated_at) VALUES(?,?,?,?)",
        ("采集", source_id, now, now),
    )
    run_ids = [
        db.execute(
            "INSERT INTO task_runs(task_id,status,started_at,correlation_id) VALUES(?,'running',?,?)",
            (task_id, now, "run-{}".format(index)),
        )
        for index in (1, 2)
    ]
    new_ids = []
    first_doc, _ = insert_document(db, source_id, {
        "title": "张三会见来宾", "content_text": "张三在北京会见来宾。",
        "canonical_url": "https://example.com/first", "published_at": "2026-07-26T08:00:00+08:00",
    }, "zh-CN")
    analyze_document(
        db, first_doc, configured_app.state.settings.get("ai"),
        task_run_id=run_ids[0], new_event_ids=new_ids,
    )
    assert new_ids
    second_new_ids = []
    second_doc, _ = insert_document(db, source_id, {
        "title": "张三会见来宾", "content_text": "张三在北京会见来宾，双方进行了交流。",
        "canonical_url": "https://example.com/second", "published_at": "2026-07-26T08:10:00+08:00",
    }, "zh-CN")
    analyze_document(
        db, second_doc, configured_app.state.settings.get("ai"),
        task_run_id=run_ids[1], new_event_ids=second_new_ids,
    )
    assert second_new_ids == []
    assert db.fetch_one("SELECT COUNT(*) n FROM task_run_events WHERE run_id=?", (run_ids[0],))["n"] >= 1
    assert db.fetch_one("SELECT COUNT(*) n FROM task_run_events WHERE run_id=?", (run_ids[1],))["n"] == 0


def test_message_content_and_worker_success(configured_app, monkeypatch):
    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    task_id, run_id, _ = _seed_delivery_data(db, 1)
    _enable_email(settings, db, monkeypatch)
    save_rule(db, "规则", [task_id], ["itinerary"], True)
    enqueue_task_run(db, settings, run_id)
    batch = db.fetch_one("SELECT * FROM email_delivery_batches ORDER BY id")
    config, _ = effective_email_config(settings, db, include_secret=True)
    message, deliverable, skipped = build_batch_message(db, settings, batch["id"], config)
    assert isinstance(message, EmailMessage)
    assert "测试采集" in message["Subject"]
    assert "新增事件 1" in message.get_body(preferencelist=("plain",)).get_content()
    assert "北京时间" in message.get_body(preferencelist=("plain",)).get_content()
    assert deliverable and not skipped
    stable_id = message["Message-ID"]

    sent = []
    monkeypatch.setattr("app.backend.notifications.send_message", lambda cfg, msg: sent.append(msg))
    result = NotificationWorker(db, settings).process_once()
    assert result["status"] == "sent"
    assert sent[0]["Message-ID"] == stable_id
    assert db.fetch_one("SELECT status FROM email_delivery_batches WHERE id=?", (batch["id"],))["status"] == "sent"


def test_worker_retries_then_fails_without_changing_task_run(configured_app, monkeypatch):
    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    task_id, run_id, _ = _seed_delivery_data(db, 1)
    _enable_email(settings, db, monkeypatch, max_attempts=2)
    save_rule(db, "规则", [task_id], ["itinerary"], True)
    enqueue_task_run(db, settings, run_id)
    monkeypatch.setattr(
        "app.backend.notifications.send_message",
        lambda cfg, msg: (_ for _ in ()).throw(TimeoutError("to@example.com timed out")),
    )
    worker = NotificationWorker(db, settings)
    first = worker.process_once()
    assert first["status"] == "retrying"
    db.execute("UPDATE email_delivery_batches SET next_attempt_at=? WHERE id=?", (utc_now(), first["id"]))
    second = worker.process_once()
    assert second["status"] == "failed"
    assert "[email]" in second["error"]
    assert db.fetch_one("SELECT status FROM task_runs WHERE id=?", (run_id,))["status"] == "success"


def test_smtp_transport_modes_and_restart_recovery(configured_app, monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            calls.append(("connect", host, port, context is not None))

        def ehlo(self):
            calls.append(("ehlo",))

        def starttls(self, context=None):
            calls.append(("starttls", context is not None))

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message["Message-ID"]))

        def quit(self):
            calls.append(("quit",))

        def close(self):
            calls.append(("close",))

    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "to@example.com"
    message["Message-ID"] = "<transport@test>"
    message.set_content("test")
    base = {
        "enabled": True, "smtp_host": "smtp.example.com", "smtp_port": 587,
        "from_address": "sender@example.com", "to_addresses": ["to@example.com"],
        "timeout_seconds": 5, "username": "user", "password": "secret",
    }
    monkeypatch.setattr("app.backend.notifications.smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("app.backend.notifications.smtplib.SMTP_SSL", FakeSMTP)
    send_message({**base, "security": "starttls"}, message)
    assert ("starttls", True) in calls
    calls.clear()
    send_message({**base, "security": "ssl", "smtp_port": 465}, message)
    assert calls[0] == ("connect", "smtp.example.com", 465, True)
    assert not any(call[0] == "starttls" for call in calls)

    db = configured_app.state.db
    settings = configured_app.state.settings
    db.initialize()
    task_id, run_id, _ = _seed_delivery_data(db, 1)
    _enable_email(settings, db, monkeypatch)
    save_rule(db, "恢复规则", [task_id], ["itinerary"], True)
    enqueue_task_run(db, settings, run_id)
    db.execute("UPDATE email_delivery_batches SET status='sending'")
    monkeypatch.setattr("app.backend.notifications.NotificationWorker._loop", lambda self: None)
    worker = NotificationWorker(db, settings)
    worker.start()
    worker.stop()
    assert db.fetch_one("SELECT status FROM email_delivery_batches ORDER BY id")["status"] == "retrying"


def test_notification_api_permissions_rules_and_audit(admin_client, configured_app, monkeypatch):
    monkeypatch.setenv("PFTS_NOTIFICATION_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))
    config = admin_client.put("/api/v1/notifications/email/config", json={
        "smtp_host": "smtp.example.com",
        "from_address": "sender@example.com",
        "to_addresses": ["to@example.com"],
        "password": "secret",
    })
    assert config.status_code == 200, config.text
    assert config.json()["config"]["password_configured"] is True
    assert "secret" not in config.text

    task_id, _, _ = _seed_delivery_data(configured_app.state.db, 0)
    person_id = configured_app.state.db.fetch_one(
        "SELECT id FROM public_figures WHERE name='测试人物'"
    )["id"]
    options = admin_client.get("/api/v1/notifications/options")
    assert options.status_code == 200
    assert options.json()["tasks"] == [{
        "id": task_id, "name": "测试采集", "enabled": 1, "source_name": "测试来源",
    }]
    assert options.json()["persons"] == [{
        "id": person_id, "name": "测试人物", "organization": "", "title": "",
    }]
    created = admin_client.post("/api/v1/notifications/rules", json={
        "name": "API 规则", "task_ids": [task_id], "person_ids": [person_id],
        "event_types": ["statement"], "enabled": True,
    })
    assert created.status_code == 201, created.text
    assert created.json()["person_ids"] == [person_id]
    assert admin_client.get("/api/v1/notifications/rules").json()["total"] == 1

    monkeypatch.setattr("app.backend.main.send_test_email", lambda settings, db: None)
    assert admin_client.post("/api/v1/notifications/email/test").status_code == 200
    audits = admin_client.get("/api/v1/audit-logs").json()["items"]
    assert any(item["object_type"] == "notification_email_config" for item in audits)
    assert any(item["object_type"] == "notification_rule" for item in audits)

    users = admin_client.get("/api/v1/users").json()["items"]
    analyst = next(item for item in users if item["username"] == "analyst")
    admin_client.put("/api/v1/users/{}/permissions".format(analyst["id"]), json={"pages": ["notifications"]})
    admin_client.post("/api/v1/auth/logout")
    assert admin_client.post("/api/v1/auth/login", json={"username": "analyst", "password": "reader123"}).status_code == 200
    assert admin_client.get("/api/v1/notifications/rules").status_code == 200
    forbidden = admin_client.post("/api/v1/notifications/rules", json={
        "name": "越权规则", "task_ids": [task_id], "event_types": ["other"], "enabled": True,
    })
    assert forbidden.status_code == 403
