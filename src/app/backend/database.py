import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','user')),
    enabled INTEGER NOT NULL DEFAULT 1,
    password_source_hash TEXT NOT NULL DEFAULT '',
    last_login_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS page_permissions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    page_key TEXT NOT NULL,
    can_access INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(user_id, page_key)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    attempt_key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS public_figures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    native_name TEXT NOT NULL DEFAULT '',
    bio TEXT NOT NULL DEFAULT '',
    organization TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    country_region TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    avatar_path TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_figures_enabled ON public_figures(enabled, deleted_at);
CREATE TABLE IF NOT EXISTS person_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES public_figures(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(person_id, alias)
);
CREATE TABLE IF NOT EXISTS information_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('rss','web_page','manual')),
    entry_url TEXT NOT NULL DEFAULT '',
    organization TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    trust_level INTEGER NOT NULL DEFAULT 3 CHECK(trust_level BETWEEN 1 AND 5),
    schedule_seconds INTEGER NOT NULL DEFAULT 3600,
    parser_config TEXT NOT NULL DEFAULT '{}',
    secret_ref TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    last_status TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_persons (
    source_id INTEGER NOT NULL REFERENCES information_sources(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES public_figures(id) ON DELETE CASCADE,
    PRIMARY KEY(source_id, person_id)
);
CREATE TABLE IF NOT EXISTS collection_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES information_sources(id) ON DELETE CASCADE,
    schedule_seconds INTEGER NOT NULL DEFAULT 3600,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES collection_tasks(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id, started_at DESC);
CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    logged_at TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS raw_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES information_sources(id),
    canonical_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    collected_at TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    content_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    fetch_metadata_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'collected',
    created_by INTEGER REFERENCES users(id),
    UNIQUE(source_id, canonical_url)
);
CREATE INDEX IF NOT EXISTS idx_docs_hash ON raw_documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_docs_published ON raw_documents(published_at DESC);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES raw_documents(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES public_figures(id),
    event_type TEXT NOT NULL CHECK(event_type IN ('itinerary','statement','other')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    start_at TEXT,
    end_at TEXT,
    original_timezone TEXT NOT NULL DEFAULT '',
    time_precision TEXT NOT NULL DEFAULT 'unknown',
    location_name TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    location_precision TEXT NOT NULL DEFAULT 'unknown',
    confirmation_status TEXT NOT NULL DEFAULT 'rumored',
    review_status TEXT NOT NULL DEFAULT 'approved',
    confidence REAL NOT NULL DEFAULT 0.5,
    quote_text TEXT NOT NULL DEFAULT '',
    translated_text TEXT NOT NULL DEFAULT '',
    original_language TEXT NOT NULL DEFAULT '',
    speech_context TEXT NOT NULL DEFAULT '',
    dedup_key TEXT NOT NULL,
    human_locked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_events_time ON timeline_events(start_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_person ON timeline_events(person_id, start_at DESC);
CREATE TABLE IF NOT EXISTS event_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES raw_documents(id) ON DELETE CASCADE,
    evidence_text TEXT NOT NULL,
    evidence_locator TEXT NOT NULL DEFAULT '',
    supports_fields_json TEXT NOT NULL DEFAULT '[]',
    source_claim_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(event_id, document_id, evidence_text)
);
CREATE TABLE IF NOT EXISTS model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES raw_documents(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    usage_json TEXT NOT NULL DEFAULT '{}',
    error_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    operator_id INTEGER REFERENCES users(id),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL,
    ip_address TEXT NOT NULL DEFAULT '',
    change_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);
CREATE TABLE IF NOT EXISTS notification_settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    overrides_json TEXT NOT NULL DEFAULT '{}',
    password_ciphertext TEXT NOT NULL DEFAULT '',
    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_run_events (
    run_id INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_task_run_events_event ON task_run_events(event_id, run_id);
CREATE INDEX IF NOT EXISTS idx_task_run_events_created ON task_run_events(created_at);
CREATE INDEX IF NOT EXISTS idx_task_run_events_watermark
    ON task_run_events(created_at,run_id,event_id);
CREATE TABLE IF NOT EXISTS email_delivery_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_run_id INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    recipient TEXT NOT NULL,
    part_number INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sending','retrying','sent','failed','skipped')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_run_id, recipient, part_number)
);
CREATE INDEX IF NOT EXISTS idx_email_batches_due ON email_delivery_batches(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_email_batches_task ON email_delivery_batches(task_run_id, id);
CREATE TABLE IF NOT EXISTS email_delivery_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES email_delivery_batches(id) ON DELETE CASCADE,
    task_run_id INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES timeline_events(id) ON DELETE SET NULL,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','skipped')),
    skip_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(task_run_id, event_id, recipient)
);
CREATE INDEX IF NOT EXISTS idx_email_items_batch ON email_delivery_items(batch_id, id);
CREATE TABLE IF NOT EXISTS daily_digest_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    event_types_json TEXT NOT NULL,
    send_time TEXT NOT NULL,
    window_mode TEXT NOT NULL
        CHECK(window_mode IN ('previous_calendar_day','rolling_hours')),
    rolling_hours INTEGER NOT NULL DEFAULT 24,
    send_when_empty INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    enabled_at TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_digest_rules_due
    ON daily_digest_rules(enabled, next_run_at, id);
CREATE TABLE IF NOT EXISTS daily_digest_rule_persons (
    rule_id INTEGER NOT NULL REFERENCES daily_digest_rules(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES public_figures(id) ON DELETE CASCADE,
    PRIMARY KEY(rule_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_digest_rule_persons_person
    ON daily_digest_rule_persons(person_id, rule_id);
CREATE TABLE IF NOT EXISTS daily_digest_rule_recipients (
    rule_id INTEGER NOT NULL REFERENCES daily_digest_rules(id) ON DELETE CASCADE,
    recipient TEXT NOT NULL,
    PRIMARY KEY(rule_id, recipient)
);
CREATE TABLE IF NOT EXISTS daily_digest_rule_sources (
    rule_id INTEGER NOT NULL REFERENCES daily_digest_rules(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES information_sources(id) ON DELETE CASCADE,
    PRIMARY KEY(rule_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_digest_rule_sources_source
    ON daily_digest_rule_sources(source_id, rule_id);
CREATE TABLE IF NOT EXISTS daily_digest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL REFERENCES daily_digest_rules(id) ON DELETE RESTRICT,
    scheduled_date TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('scheduled','manual')),
    status TEXT NOT NULL
        CHECK(status IN ('pending','empty','sending','sent','partial','failed','skipped')),
    candidate_count INTEGER NOT NULL DEFAULT 0,
    batch_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    missed_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(rule_id, scheduled_date)
);
CREATE INDEX IF NOT EXISTS idx_digest_runs_rule
    ON daily_digest_runs(rule_id, scheduled_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_digest_runs_status
    ON daily_digest_runs(status, id);
CREATE TABLE IF NOT EXISTS daily_digest_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES daily_digest_runs(id) ON DELETE CASCADE,
    recipient TEXT NOT NULL,
    part_number INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sending','retrying','sent','failed','skipped')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, recipient, part_number)
);
CREATE INDEX IF NOT EXISTS idx_digest_batches_due
    ON daily_digest_batches(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_digest_batches_run
    ON daily_digest_batches(run_id, id);
CREATE TABLE IF NOT EXISTS daily_digest_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES daily_digest_batches(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES daily_digest_runs(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES timeline_events(id) ON DELETE SET NULL,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sent','skipped')),
    skip_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, event_id, recipient)
);
CREATE INDEX IF NOT EXISTS idx_digest_items_batch
    ON daily_digest_items(batch_id, id);
"""


class Database:
    def __init__(self, path: Path, busy_timeout_ms: int = 5000):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        # SQLite PRAGMA statements do not support DB-API parameter placeholders.
        connection.execute("PRAGMA busy_timeout = {}".format(max(0, int(self.busy_timeout_ms))))
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            event_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='timeline_events'"
            ).fetchone()
            if event_sql_row and "'other'" not in (event_sql_row[0] or ""):
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.executescript("""
                    CREATE TABLE timeline_events_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        person_id INTEGER NOT NULL REFERENCES public_figures(id),
                        event_type TEXT NOT NULL CHECK(event_type IN ('itinerary','statement','other')),
                        title TEXT NOT NULL, summary TEXT NOT NULL, start_at TEXT, end_at TEXT,
                        original_timezone TEXT NOT NULL DEFAULT '', time_precision TEXT NOT NULL DEFAULT 'unknown',
                        location_name TEXT NOT NULL DEFAULT '', latitude REAL, longitude REAL,
                        location_precision TEXT NOT NULL DEFAULT 'unknown', confirmation_status TEXT NOT NULL DEFAULT 'rumored',
                        review_status TEXT NOT NULL DEFAULT 'approved', confidence REAL NOT NULL DEFAULT 0.5,
                        quote_text TEXT NOT NULL DEFAULT '', translated_text TEXT NOT NULL DEFAULT '',
                        original_language TEXT NOT NULL DEFAULT '', speech_context TEXT NOT NULL DEFAULT '',
                        dedup_key TEXT NOT NULL, human_locked INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(dedup_key)
                    );
                    INSERT INTO timeline_events_new SELECT * FROM timeline_events;
                    DROP TABLE timeline_events;
                    ALTER TABLE timeline_events_new RENAME TO timeline_events;
                    CREATE INDEX idx_events_time ON timeline_events(start_at DESC, id DESC);
                    CREATE INDEX idx_events_person ON timeline_events(person_id, start_at DESC);
                """)
                connection.commit()
                connection.execute("PRAGMA foreign_keys = ON")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(raw_documents)").fetchall()}
            if "fetch_metadata_json" not in columns:
                connection.execute(
                    "ALTER TABLE raw_documents ADD COLUMN fetch_metadata_json TEXT NOT NULL DEFAULT '{}'"
                )
            source_columns = {row[1] for row in connection.execute("PRAGMA table_info(information_sources)").fetchall()}
            if "deleted_at" not in source_columns:
                connection.execute("ALTER TABLE information_sources ADD COLUMN deleted_at TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(1, datetime('now'))"
            )
            migration_2 = connection.execute("SELECT 1 FROM schema_version WHERE version=2").fetchone()
            if not migration_2:
                # Existing un-timed "other" events should use their evidence article's
                # publication time, matching the extraction behavior for new events.
                connection.execute("""
                    UPDATE timeline_events
                    SET start_at=(
                        SELECT COALESCE(d.published_at, d.collected_at)
                        FROM event_evidence ev
                        JOIN raw_documents d ON d.id=ev.document_id
                        WHERE ev.event_id=timeline_events.id
                        ORDER BY COALESCE(d.published_at, d.collected_at), d.id
                        LIMIT 1
                    ), time_precision='day'
                    WHERE event_type='other' AND start_at IS NULL
                      AND EXISTS (
                        SELECT 1 FROM event_evidence ev
                        JOIN raw_documents d ON d.id=ev.document_id
                        WHERE ev.event_id=timeline_events.id
                          AND COALESCE(d.published_at, d.collected_at) IS NOT NULL
                      )
                """)
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(2, datetime('now'))"
                )
            # Repair events created by the former month/day parser, which used
            # the server's current year for historical articles. Human-edited
            # records are intentionally excluded.
            if not connection.execute("SELECT 1 FROM schema_version WHERE version=3").fetchone():
                connection.execute("""
                    UPDATE timeline_events
                    SET start_at = (
                            SELECT MIN(d.published_at)
                            FROM event_evidence ee
                            JOIN raw_documents d ON d.id=ee.document_id
                            WHERE ee.event_id=timeline_events.id AND d.published_at IS NOT NULL
                        ),
                        time_precision='exact', original_timezone='Asia/Shanghai'
                    WHERE human_locked=0
                      AND substr(start_at,1,4)=strftime('%Y','now')
                      AND EXISTS (
                          SELECT 1 FROM event_evidence ee
                          JOIN raw_documents d ON d.id=ee.document_id
                          WHERE ee.event_id=timeline_events.id
                            AND d.published_at IS NOT NULL
                            AND substr(d.published_at,1,4) < substr(timeline_events.start_at,1,4)
                      )
                """)
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(3, datetime('now'))"
                )
            if not connection.execute("SELECT 1 FROM schema_version WHERE version=4").fetchone():
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(4, datetime('now'))"
                )
            if not connection.execute("SELECT 1 FROM schema_version WHERE version=5").fetchone():
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(5, datetime('now'))"
                )
            if not connection.execute("SELECT 1 FROM schema_version WHERE version=6").fetchone():
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(6, datetime('now'))"
                )
            if not connection.execute("SELECT 1 FROM schema_version WHERE version=7").fetchone():
                # The push-rule tables are retired in migration 9; only repair
                # them on databases that still have notification_rules.
                has_rules_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_rules'"
                ).fetchone()
                if has_rules_table:
                    rule_columns = {
                        row[1] for row in connection.execute(
                            "PRAGMA table_info(notification_rules)"
                        ).fetchall()
                    }
                    additions = {
                        "delivery_mode": "TEXT NOT NULL DEFAULT 'immediate'",
                        "send_times_json": "TEXT NOT NULL DEFAULT '[]'",
                        "enabled_at": "TEXT",
                        "next_run_at": "TEXT",
                        "cursor_created_at": "TEXT NOT NULL DEFAULT ''",
                        "cursor_run_id": "INTEGER NOT NULL DEFAULT 0",
                        "cursor_event_id": "INTEGER NOT NULL DEFAULT 0",
                        "deleted_at": "TEXT",
                    }
                    for name, declaration in additions.items():
                        if name not in rule_columns:
                            connection.execute(
                                "ALTER TABLE notification_rules ADD COLUMN {} {}".format(
                                    name, declaration
                                )
                            )
                    connection.execute(
                        "UPDATE notification_rules SET delivery_mode='immediate' "
                        "WHERE delivery_mode IS NULL OR delivery_mode=''"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_notification_rules_due "
                        "ON notification_rules(delivery_mode,enabled,next_run_at,id)"
                    )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_run_events_watermark "
                    "ON task_run_events(created_at,run_id,event_id)"
                )
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(7, datetime('now'))"
                )
            if not connection.execute("SELECT 1 FROM schema_version WHERE version=8").fetchone():
                connection.execute(
                    "UPDATE timeline_events SET review_status='approved',updated_at=datetime('now') "
                    "WHERE review_status IN ('pending','needs_review')"
                )
                connection.execute(
                    "DELETE FROM page_permissions WHERE page_key='review'"
                )
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(8, datetime('now'))"
                )
            if not connection.execute("SELECT 1 FROM schema_version WHERE version=9").fetchone():
                # Retire the push-rule and scheduled-incremental tables; the page
                # now exposes only the "动态推送" (daily digest) mechanism. Drop in
                # foreign-key dependency order so PRAGMA foreign_keys=ON stays
                # satisfied. email_delivery_* and task_run_events are kept for
                # historical delivery records and event extraction.
                connection.executescript("""
                    DROP TABLE IF EXISTS scheduled_notification_items;
                    DROP TABLE IF EXISTS scheduled_notification_batches;
                    DROP TABLE IF EXISTS scheduled_notification_runs;
                    DROP TABLE IF EXISTS notification_rule_persons;
                    DROP TABLE IF EXISTS notification_rule_tasks;
                    DROP TABLE IF EXISTS notification_rules;
                """)
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(9, datetime('now'))"
                )
            if not connection.execute("SELECT 1 FROM schema_version WHERE version=10").fetchone():
                # Re-source event occurrence time from the article's publication
                # time. Repair non-locked events whose start_at was overwritten
                # by body-text date extraction; recompute dedup_key so future
                # re-analysis stays consistent.
                migrate_event_time_to_publish_time(connection)
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(10, datetime('now'))"
                )

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
            return dict(row) if row else None

    def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(sql, params)
            connection.commit()
            return int(cursor.lastrowid)

    def execute_many(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        with self.connect() as connection:
            connection.executemany(sql, params)
            connection.commit()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def migrate_event_time_to_publish_time(connection: sqlite3.Connection) -> Dict[str, int]:
    """One-shot migration (schema_version 10): re-source event ``start_at``
    from each event's earliest evidence document publication time.

    For every ``human_locked=0`` event with at least one evidence document
    whose ``COALESCE(published_at, collected_at)`` is non-null, this migration
    normalizes that timestamp via ``extractor._publish_time`` and recomputes
    ``dedup_key`` via ``extractor.event_dedup_key`` (text = first evidence
    snippet, falling back to the event title). Collisions with locked events,
    with unrepairable non-locked events, or among the repair set itself are
    resolved by dropping the redundant non-locked record; human-locked events
    are never modified or deleted.

    Idempotent: re-running on a repaired database recomputes the same
    ``start_at`` and ``dedup_key`` values, producing no further changes.
    """
    from app.backend.extractor import _publish_time, event_dedup_key

    counts: Dict[str, int] = {"repaired": 0, "skipped_locked": 0, "deleted_duplicates": 0}

    repair_rows = connection.execute(
        """
        SELECT te.id AS id, te.person_id AS person_id, te.event_type AS event_type,
               te.dedup_key AS old_key, te.start_at AS old_start, te.title AS title,
               (SELECT COALESCE(d.published_at, d.collected_at)
                FROM event_evidence ev JOIN raw_documents d ON d.id = ev.document_id
                WHERE ev.event_id = te.id
                ORDER BY COALESCE(d.published_at, d.collected_at), d.id
                LIMIT 1) AS doc_time,
               (SELECT ee.evidence_text FROM event_evidence ee
                WHERE ee.event_id = te.id ORDER BY ee.id LIMIT 1) AS evidence_text
        FROM timeline_events te
        WHERE te.human_locked = 0
          AND EXISTS (
              SELECT 1 FROM event_evidence ev
              JOIN raw_documents d ON d.id = ev.document_id
              WHERE ev.event_id = te.id
                AND COALESCE(d.published_at, d.collected_at) IS NOT NULL
          )
        """
    ).fetchall()

    proposed: List[Dict[str, Any]] = []
    by_new_key: Dict[str, List[int]] = {}
    for row in repair_rows:
        new_start = _publish_time(row["doc_time"])
        if not new_start:
            # Unparseable document timestamp: leave the event untouched
            # (keeps its prior start_at and dedup_key, treated as protected below).
            continue
        text = row["evidence_text"] or row["title"] or ""
        new_key = event_dedup_key(row["person_id"], row["event_type"], new_start, text)
        # Idempotency: skip rows that already carry the proposed values, so a
        # repeated migration reports zero further changes.
        if row["old_key"] == new_key and row["old_start"] == new_start:
            continue
        proposed.append({"id": row["id"], "new_start": new_start, "new_key": new_key})
        by_new_key.setdefault(new_key, []).append(row["id"])

    # Protected keys: locked events + non-locked events outside the repair set
    # (including the unparseable rows above). Their current dedup_key must not
    # be claimed by any survivor.
    repair_ids = [p["id"] for p in proposed]
    if repair_ids:
        placeholders = ",".join("?" * len(repair_ids))
        protected_rows = connection.execute(
            f"SELECT dedup_key FROM timeline_events "
            f"WHERE human_locked = 1 OR (human_locked = 0 AND id NOT IN ({placeholders}))",
            tuple(repair_ids),
        ).fetchall()
    else:
        protected_rows = connection.execute(
            "SELECT dedup_key FROM timeline_events WHERE human_locked = 1"
        ).fetchall()
    protected_keys: Set[str] = {r["dedup_key"] for r in protected_rows}

    to_delete: Set[int] = set()
    survivors: List[Dict[str, Any]] = []
    for new_key, ids in by_new_key.items():
        ids_sorted = sorted(ids)
        if new_key in protected_keys:
            # Locked / unrepairable record already owns this key: drop the
            # whole repair group rather than overwrite a protected event.
            to_delete.update(ids_sorted)
            continue
        survivors.append(next(p for p in proposed if p["id"] == ids_sorted[0]))
        to_delete.update(ids_sorted[1:])

    # Stage: rewrite survivors' dedup_key to a per-row temporary value so the
    # final UPDATE never trips a UNIQUE collision with a peer's old key.
    if survivors:
        connection.executemany(
            "UPDATE timeline_events SET dedup_key = ? WHERE id = ?",
            [(f"repair-migration-{p['id']}", p["id"]) for p in survivors],
        )
    if to_delete:
        for event_id in sorted(to_delete):
            connection.execute("DELETE FROM timeline_events WHERE id = ?", (event_id,))
    if survivors:
        connection.executemany(
            "UPDATE timeline_events SET start_at = ?, dedup_key = ?, "
            "time_precision = 'day', original_timezone = 'Asia/Shanghai' WHERE id = ?",
            [(p["new_start"], p["new_key"], p["id"]) for p in survivors],
        )

    counts["repaired"] = len(survivors)
    counts["deleted_duplicates"] = len(to_delete)
    counts["skipped_locked"] = int(
        connection.execute("SELECT COUNT(*) FROM timeline_events WHERE human_locked = 1").fetchone()[0]
    )
    connection.execute(
        "INSERT INTO audit_logs(actor_id,action,object_type,result,change_summary,created_at) "
        "VALUES(NULL,'migrate_event_time_to_publish','timeline_events','success',?,datetime('now'))",
        (
            "修复 {} 条 start_at；跳过 {} 条人工锁定；删除 {} 条重复。".format(
                counts["repaired"], counts["skipped_locked"], counts["deleted_duplicates"]
            ),
        ),
    )
    return counts
