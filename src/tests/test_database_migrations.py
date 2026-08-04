from app.backend.database import Database


def test_migration_backfills_other_event_time_from_publication(tmp_path):
    db = Database(tmp_path / "app.sqlite3")
    db.initialize()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO public_figures(name,created_at,updated_at) VALUES('张三','2026-07-08','2026-07-08')"
        )
        connection.execute(
            "INSERT INTO information_sources(name,type,created_at,updated_at) VALUES('测试','web_page','2026-07-08','2026-07-08')"
        )
        connection.execute(
            "INSERT INTO raw_documents(source_id,title,published_at,collected_at,content_text,content_hash,status) "
            "VALUES(1,'文章','2026-07-08T09:30:00+08:00','2026-07-08T02:00:00+00:00','正文','hash','analyzed')"
        )
        connection.execute(
            "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,dedup_key,created_at,updated_at) "
            "VALUES(1,'other','其他事件','摘要',NULL,'key','2026-07-08','2026-07-08')"
        )
        connection.execute(
            "INSERT INTO event_evidence(event_id,document_id,evidence_text) VALUES(1,1,'正文')"
        )
        connection.execute("DELETE FROM schema_version WHERE version=2")

    db.initialize()

    event = db.fetch_one("SELECT start_at,time_precision FROM timeline_events WHERE id=1")
    assert event == {"start_at": "2026-07-08T09:30:00+08:00", "time_precision": "day"}


def test_migration_approves_pending_events_and_removes_review_permissions(tmp_path):
    db = Database(tmp_path / "app.sqlite3")
    db.initialize()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO users(username,password_hash,role,password_source_hash,created_at,updated_at) "
            "VALUES('analyst','hash','user','source','2026-07-08','2026-07-08')"
        )
        connection.execute(
            "INSERT INTO page_permissions(user_id,page_key,can_access) VALUES(1,'review',1)"
        )
        connection.execute(
            "INSERT INTO public_figures(name,created_at,updated_at) VALUES('张三','2026-07-08','2026-07-08')"
        )
        connection.executemany(
            "INSERT INTO timeline_events(person_id,event_type,title,summary,review_status,dedup_key,created_at,updated_at) "
            "VALUES(1,'other',?,?,?,?,'2026-07-08','2026-07-08')",
            [
                ("待审核事件", "摘要", "pending", "pending-key"),
                ("需复核事件", "摘要", "needs_review", "needs-key"),
                ("已驳回事件", "摘要", "rejected", "rejected-key"),
            ],
        )
        connection.execute("DELETE FROM schema_version WHERE version=8")

    db.initialize()
    db.initialize()

    statuses = {
        row["title"]: row["review_status"]
        for row in db.fetch_all("SELECT title,review_status FROM timeline_events ORDER BY id")
    }
    assert statuses == {
        "待审核事件": "approved",
        "需复核事件": "approved",
        "已驳回事件": "rejected",
    }
    assert db.fetch_one("SELECT page_key FROM page_permissions WHERE page_key='review'") is None
