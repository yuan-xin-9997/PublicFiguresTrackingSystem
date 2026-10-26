AI_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_credentials (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 secret_hash TEXT NOT NULL, scopes TEXT NOT NULL, created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, revoked_at TEXT, last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_tokens (
 token_hash TEXT PRIMARY KEY, credential_id TEXT NOT NULL REFERENCES ai_credentials(id),
 resource TEXT NOT NULL, scopes TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_requests (
 credential_id TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 status INTEGER, response TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(credential_id,request_key)
);
CREATE TABLE IF NOT EXISTS ai_jobs (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), credential_id TEXT NOT NULL,
 kind TEXT NOT NULL, target_id INTEGER NOT NULL, status TEXT NOT NULL,
 result TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_rate_limits (
 bucket TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_version(version,applied_at) VALUES(12,datetime('now'));
"""
