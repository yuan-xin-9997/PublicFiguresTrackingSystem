import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence


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
    review_status TEXT NOT NULL DEFAULT 'pending',
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
                        review_status TEXT NOT NULL DEFAULT 'pending', confidence REAL NOT NULL DEFAULT 0.5,
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

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
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
