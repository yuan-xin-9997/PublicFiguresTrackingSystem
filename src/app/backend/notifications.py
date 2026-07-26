import html
import json
import logging
import os
import re
import smtplib
import ssl
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .database import Database, json_text
from .security import utc_now


LOGGER = logging.getLogger("pfts.notifications")
EVENT_TYPES = {"itinerary", "statement", "other"}
EMAIL_FIELDS = {
    "enabled", "smtp_host", "smtp_port", "security", "username", "password_env",
    "credential_key_env", "from_address", "from_name", "to_addresses", "subject_prefix",
    "max_events_per_message", "worker_poll_seconds", "max_attempts",
    "retry_base_seconds", "timeout_seconds",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email_address(value: str) -> str:
    candidate = str(value or "").strip()
    _, parsed = parseaddr(candidate)
    if not candidate or parsed != candidate or not EMAIL_RE.match(parsed):
        raise ValueError("邮箱地址格式无效：{}".format(candidate[:100] or "空值"))
    return parsed.lower()


def normalize_email_config(values: Dict[str, Any]) -> Dict[str, Any]:
    normalized = deepcopy(values)
    unknown = set(normalized) - EMAIL_FIELDS
    if unknown:
        raise ValueError("包含未知邮件配置字段：{}".format(",".join(sorted(unknown))))
    normalized["enabled"] = bool(normalized.get("enabled", False))
    normalized["smtp_host"] = str(normalized.get("smtp_host") or "").strip()
    if len(normalized["smtp_host"]) > 255 or any(char.isspace() for char in normalized["smtp_host"]):
        raise ValueError("SMTP 主机格式无效")
    normalized["security"] = str(normalized.get("security") or "starttls").lower()
    if normalized["security"] not in {"none", "starttls", "ssl"}:
        raise ValueError("邮件安全模式必须是 none、starttls 或 ssl")
    integer_ranges = {
        "smtp_port": (1, 65535, 587),
        "max_events_per_message": (1, 100, 25),
        "worker_poll_seconds": (5, 3600, 15),
        "max_attempts": (1, 20, 5),
        "retry_base_seconds": (1, 86400, 60),
        "timeout_seconds": (1, 120, 15),
    }
    for field, (minimum, maximum, default) in integer_ranges.items():
        try:
            value = int(normalized.get(field, default))
        except (TypeError, ValueError):
            raise ValueError("{} 必须是整数".format(field))
        if value < minimum or value > maximum:
            raise ValueError("{} 必须在 {} 到 {} 之间".format(field, minimum, maximum))
        normalized[field] = value
    for field, maximum in {
        "username": 300, "password_env": 200, "credential_key_env": 200,
        "from_name": 200, "subject_prefix": 100,
    }.items():
        normalized[field] = str(normalized.get(field) or "").strip()
        if len(normalized[field]) > maximum:
            raise ValueError("{} 长度超出限制".format(field))
    from_address = str(normalized.get("from_address") or "").strip()
    normalized["from_address"] = _email_address(from_address) if from_address else ""
    raw_recipients = normalized.get("to_addresses") or []
    if isinstance(raw_recipients, str):
        raw_recipients = re.split(r"[,;\n，；]+", raw_recipients)
    if not isinstance(raw_recipients, list):
        raise ValueError("to_addresses 必须是邮箱地址数组")
    normalized["to_addresses"] = list(dict.fromkeys(
        _email_address(item) for item in raw_recipients if str(item).strip()
    ))
    return normalized


def _settings_row(db: Database) -> Dict[str, Any]:
    return db.fetch_one("SELECT * FROM notification_settings WHERE id=1") or {
        "overrides_json": "{}", "password_ciphertext": "", "updated_at": ""
    }


def _page_overrides(db: Database) -> Dict[str, Any]:
    try:
        value = json.loads(_settings_row(db).get("overrides_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _fernet(config: Dict[str, Any]) -> Fernet:
    env_name = str(config.get("credential_key_env") or "PFTS_NOTIFICATION_CREDENTIAL_KEY")
    key = os.getenv(env_name, "").encode("ascii", errors="ignore")
    if not key:
        raise ValueError("未配置邮件凭证加密主密钥环境变量 {}".format(env_name))
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise ValueError("邮件凭证加密主密钥格式无效")


def effective_email_config(
    settings: Settings,
    db: Database,
    include_secret: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    configured = deepcopy((settings.get("notifications") or {}).get("email") or {})
    sources = {field: settings.source("notifications", "email", field) for field in EMAIL_FIELDS}
    for field, value in _page_overrides(db).items():
        if field in EMAIL_FIELDS and value not in (None, ""):
            configured[field] = value
            sources[field] = "page"
    normalized = normalize_email_config(configured)
    row = _settings_row(db)
    ciphertext = str(row.get("password_ciphertext") or "")
    password = ""
    password_source = "none"
    if ciphertext:
        password_source = "page"
        if include_secret:
            try:
                password = _fernet(normalized).decrypt(ciphertext.encode("ascii")).decode("utf-8")
            except InvalidToken:
                raise ValueError("页面 SMTP 密码无法使用当前主密钥解密，请重新录入")
    else:
        env_name = str(normalized.get("password_env") or "")
        if env_name and os.getenv(env_name):
            password_source = "environment"
            if include_secret:
                password = os.getenv(env_name, "")
    normalized["password_configured"] = password_source != "none"
    normalized["password_source"] = password_source
    if include_secret:
        normalized["password"] = password
    return normalized, sources


def save_email_overrides(
    db: Database,
    settings: Settings,
    updates: Dict[str, Any],
    clear_fields: Sequence[str],
    password: str,
    clear_password: bool,
    actor_id: Optional[int],
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    invalid_clear = set(clear_fields) - EMAIL_FIELDS
    if invalid_clear:
        raise ValueError("包含未知清除字段：{}".format(",".join(sorted(invalid_clear))))
    current = _page_overrides(db)
    candidate = dict(current)
    for field in clear_fields:
        candidate.pop(field, None)
    for field, value in updates.items():
        if field not in EMAIL_FIELDS:
            raise ValueError("包含未知邮件配置字段：{}".format(field))
        if value in (None, ""):
            candidate.pop(field, None)
        else:
            candidate[field] = value
    base = deepcopy((settings.get("notifications") or {}).get("email") or {})
    merged = dict(base)
    merged.update(candidate)
    normalized = normalize_email_config(merged)
    normalized_overrides = {field: normalized[field] for field in candidate}
    row = _settings_row(db)
    ciphertext = str(row.get("password_ciphertext") or "")
    if password:
        ciphertext = _fernet(normalized).encrypt(password.encode("utf-8")).decode("ascii")
    elif clear_password:
        ciphertext = ""
    now = utc_now()
    db.execute(
        "INSERT INTO notification_settings(id,overrides_json,password_ciphertext,updated_by,updated_at) "
        "VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET overrides_json=excluded.overrides_json,"
        "password_ciphertext=excluded.password_ciphertext,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
        (json_text(normalized_overrides), ciphertext, actor_id, now),
    )
    return effective_email_config(settings, db)


def masked_email_config(settings: Settings, db: Database) -> Dict[str, Any]:
    config, sources = effective_email_config(settings, db)
    visible = {key: value for key, value in config.items() if key not in {"password"}}
    for key in ("password_env", "credential_key_env"):
        visible[key] = {
            "environment_variable": str(config.get(key) or ""),
            "configured": bool(os.getenv(str(config.get(key) or ""))),
        }
    return {"config": visible, "sources": sources, "updated_at": _settings_row(db).get("updated_at") or ""}


def validate_sendable(config: Dict[str, Any], require_enabled: bool = True) -> None:
    missing = []
    if require_enabled and not config.get("enabled"):
        missing.append("enabled")
    for field in ("smtp_host", "from_address"):
        if not config.get(field):
            missing.append(field)
    if not config.get("to_addresses"):
        missing.append("to_addresses")
    if config.get("username") and not config.get("password"):
        missing.append("password")
    if missing:
        raise ValueError("邮件配置不完整：{}".format(", ".join(missing)))


def list_rules(db: Database) -> List[Dict[str, Any]]:
    rules = db.fetch_all("SELECT * FROM notification_rules ORDER BY id")
    for rule in rules:
        try:
            rule["event_types"] = json.loads(rule.pop("event_types_json"))
        except (TypeError, ValueError):
            rule["event_types"] = []
        rule["task_ids"] = [
            row["task_id"] for row in db.fetch_all(
                "SELECT task_id FROM notification_rule_tasks WHERE rule_id=? ORDER BY task_id", (rule["id"],)
            )
        ]
        rule["person_ids"] = [
            row["person_id"] for row in db.fetch_all(
                "SELECT person_id FROM notification_rule_persons WHERE rule_id=? ORDER BY person_id",
                (rule["id"],),
            )
        ]
        rule["enabled"] = bool(rule["enabled"])
    return rules


def save_rule(
    db: Database,
    name: str,
    task_ids: Iterable[int],
    event_types: Iterable[str],
    enabled: bool,
    rule_id: Optional[int] = None,
    person_ids: Optional[Iterable[int]] = None,
) -> Dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 200:
        raise ValueError("规则名称不能为空且不能超过 200 个字符")
    clean_tasks = sorted(set(int(value) for value in task_ids))
    clean_types = sorted(set(str(value) for value in event_types))
    clean_persons = sorted(set(int(value) for value in (person_ids or [])))
    if not clean_tasks:
        raise ValueError("至少选择一个采集任务")
    if not clean_types or any(value not in EVENT_TYPES for value in clean_types):
        raise ValueError("至少选择一个有效事件类型")
    found = db.fetch_all(
        "SELECT id FROM collection_tasks WHERE id IN ({})".format(",".join("?" for _ in clean_tasks)),
        clean_tasks,
    )
    if len(found) != len(clean_tasks):
        raise ValueError("包含不存在的采集任务")
    if clean_persons:
        found_persons = db.fetch_all(
            "SELECT id FROM public_figures WHERE enabled=1 AND deleted_at IS NULL AND id IN ({})".format(
                ",".join("?" for _ in clean_persons)
            ),
            clean_persons,
        )
        if len(found_persons) != len(clean_persons):
            raise ValueError("包含不存在或不可用的人物")
    now = utc_now()
    with db.transaction() as connection:
        if rule_id is None:
            cursor = connection.execute(
                "INSERT INTO notification_rules(name,event_types_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?)",
                (clean_name, json_text(clean_types), int(enabled), now, now),
            )
            rule_id = int(cursor.lastrowid)
        else:
            cursor = connection.execute(
                "UPDATE notification_rules SET name=?,event_types_json=?,enabled=?,updated_at=? WHERE id=?",
                (clean_name, json_text(clean_types), int(enabled), now, rule_id),
            )
            if not cursor.rowcount:
                raise ValueError("推送规则不存在")
            connection.execute("DELETE FROM notification_rule_tasks WHERE rule_id=?", (rule_id,))
            connection.execute("DELETE FROM notification_rule_persons WHERE rule_id=?", (rule_id,))
        connection.executemany(
            "INSERT INTO notification_rule_tasks(rule_id,task_id) VALUES(?,?)",
            [(rule_id, task_id) for task_id in clean_tasks],
        )
        connection.executemany(
            "INSERT INTO notification_rule_persons(rule_id,person_id) VALUES(?,?)",
            [(rule_id, person_id) for person_id in clean_persons],
        )
    return next(rule for rule in list_rules(db) if int(rule["id"]) == rule_id)


def delete_rule(db: Database, rule_id: int) -> bool:
    with db.transaction() as connection:
        cursor = connection.execute("DELETE FROM notification_rules WHERE id=?", (rule_id,))
        return bool(cursor.rowcount)


def _chunks(values: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def enqueue_task_run(db: Database, settings: Settings, run_id: int) -> Dict[str, int]:
    config, _ = effective_email_config(settings, db)
    counters = {"candidates": 0, "enqueued": 0, "skipped": 0, "batches": 0}
    if not config.get("enabled") or not config.get("to_addresses"):
        return counters
    run = db.fetch_one("SELECT id,task_id FROM task_runs WHERE id=?", (run_id,))
    if not run:
        raise ValueError("任务运行不存在")
    rule_rows = db.fetch_all(
        "SELECT r.id,r.event_types_json FROM notification_rules r "
        "JOIN notification_rule_tasks rt ON rt.rule_id=r.id WHERE r.enabled=1 AND rt.task_id=?",
        (run["task_id"],),
    )
    matchers = []
    for row in rule_rows:
        try:
            allowed_types = {
                value for value in json.loads(row["event_types_json"]) if value in EVENT_TYPES
            }
        except (TypeError, ValueError):
            continue
        if not allowed_types:
            continue
        persons = {
            int(item["person_id"]) for item in db.fetch_all(
                "SELECT person_id FROM notification_rule_persons WHERE rule_id=?",
                (row["id"],),
            )
        }
        matchers.append((allowed_types, persons))
    if not matchers:
        return counters
    events = db.fetch_all(
        "SELECT tre.event_id,e.event_type,e.person_id FROM task_run_events tre "
        "JOIN timeline_events e ON e.id=tre.event_id "
        "JOIN public_figures p ON p.id=e.person_id "
        "WHERE tre.run_id=? AND p.enabled=1 AND p.deleted_at IS NULL ORDER BY tre.event_id",
        (run_id,),
    )
    event_ids = [
        int(row["event_id"]) for row in events
        if any(
            row["event_type"] in allowed_types
            and (not person_ids or int(row["person_id"]) in person_ids)
            for allowed_types, person_ids in matchers
        )
    ]
    counters["candidates"] = len(event_ids)
    if not event_ids:
        return counters
    now = utc_now()
    size = int(config["max_events_per_message"])
    with db.transaction() as connection:
        for recipient in config["to_addresses"]:
            for part_number, chunk in enumerate(_chunks(event_ids, size), 1):
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO email_delivery_batches(task_run_id,recipient,part_number,status,"
                    "next_attempt_at,message_id,created_at,updated_at) VALUES(?,?,?,'pending',?,'',?,?)",
                    (run_id, recipient, part_number, now, now, now),
                )
                batch = connection.execute(
                    "SELECT id FROM email_delivery_batches WHERE task_run_id=? AND recipient=? AND part_number=?",
                    (run_id, recipient, part_number),
                ).fetchone()
                batch_id = int(batch["id"])
                connection.execute(
                    "UPDATE email_delivery_batches SET message_id=? WHERE id=? AND message_id=''",
                    ("<pfts-{}@local>".format(batch_id), batch_id),
                )
                inserted = 0
                for event_id in chunk:
                    item = connection.execute(
                        "INSERT OR IGNORE INTO email_delivery_items(batch_id,task_run_id,event_id,recipient,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (batch_id, run_id, event_id, recipient, now),
                    )
                    inserted += max(0, int(item.rowcount))
                if cursor.rowcount and not inserted:
                    connection.execute("DELETE FROM email_delivery_batches WHERE id=?", (batch_id,))
                elif cursor.rowcount:
                    counters["batches"] += 1
                counters["enqueued"] += inserted
    counters["skipped"] = max(0, counters["candidates"] * len(config["to_addresses"]) - counters["enqueued"])
    return counters


def _beijing(value: Optional[str]) -> str:
    if not value:
        return "时间未知"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def _batch_rows(db: Database, batch_id: int) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    batch = db.fetch_one(
        "SELECT b.*,r.task_id,t.name AS task_name FROM email_delivery_batches b "
        "JOIN task_runs r ON r.id=b.task_run_id JOIN collection_tasks t ON t.id=r.task_id WHERE b.id=?",
        (batch_id,),
    )
    rows = db.fetch_all(
        "SELECT i.id AS item_id,i.event_id,i.status AS item_status,e.event_type,e.title,e.summary,e.start_at,"
        "e.location_name,e.confirmation_status,e.review_status,p.name AS person_name,"
        "COALESCE((SELECT GROUP_CONCAT(DISTINCT s.name) FROM event_evidence ev "
        "JOIN raw_documents d ON d.id=ev.document_id JOIN information_sources s ON s.id=d.source_id "
        "WHERE ev.event_id=e.id),'') AS source_names "
        "FROM email_delivery_items i LEFT JOIN timeline_events e ON e.id=i.event_id "
        "LEFT JOIN public_figures p ON p.id=e.person_id WHERE i.batch_id=? ORDER BY i.id",
        (batch_id,),
    )
    return batch, rows


def build_batch_message(
    db: Database,
    settings: Settings,
    batch_id: int,
    config: Dict[str, Any],
) -> Tuple[Optional[EmailMessage], List[int], List[int]]:
    batch, rows = _batch_rows(db, batch_id)
    if not batch:
        raise ValueError("邮件批次不存在")
    deliverable = [row for row in rows if row.get("event_id") and row.get("review_status") != "rejected"]
    skipped = [int(row["item_id"]) for row in rows if row not in deliverable]
    if not deliverable:
        return None, [], skipped
    labels = {"itinerary": "行程", "statement": "言论", "other": "其他"}
    plain_parts = []
    html_parts = []
    base_url = str(settings.get("server", "base_url", "") or "").rstrip("/") + "/"
    for index, event in enumerate(deliverable, 1):
        link = urljoin(base_url, "?event_id={}".format(event["event_id"])) if base_url != "/" else ""
        facts = [
            "{}. [{}] {} · {}".format(index, labels.get(event["event_type"], event["event_type"]), event["person_name"], event["title"]),
            "时间：{}（北京时间）".format(_beijing(event["start_at"])),
            "地点：{}".format(event["location_name"] or "未披露"),
            "状态：{} / {}".format(event["confirmation_status"], event["review_status"]),
            "来源：{}".format(event["source_names"] or "来源未知"),
            str(event["summary"] or ""),
        ]
        if link:
            facts.append("详情：{}".format(link))
        plain_parts.append("\n".join(facts))
        html_parts.append(
            "<article><h3>{}. [{}] {} · {}</h3><p>{}</p><p>时间：{}（北京时间）<br>"
            "地点：{}<br>状态：{} / {}<br>来源：{}{}</p></article>".format(
                index, html.escape(labels.get(event["event_type"], event["event_type"])),
                html.escape(str(event["person_name"] or "")), html.escape(str(event["title"] or "")),
                html.escape(str(event["summary"] or "")), html.escape(_beijing(event["start_at"])),
                html.escape(str(event["location_name"] or "未披露")),
                html.escape(str(event["confirmation_status"])), html.escape(str(event["review_status"])),
                html.escape(str(event["source_names"] or "来源未知")),
                '<br><a href="{}">查看详情</a>'.format(html.escape(link, quote=True)) if link else "",
            )
        )
    message = EmailMessage()
    prefix = str(config.get("subject_prefix") or "[PFTS]").strip()
    part_suffix = "（第 {} 部分）".format(batch["part_number"]) if int(batch["part_number"]) > 1 else ""
    message["Subject"] = "{} {}：新增 {} 条事件{}".format(prefix, batch["task_name"], len(deliverable), part_suffix)
    message["From"] = formataddr((str(config.get("from_name") or ""), str(config["from_address"])))
    message["To"] = str(batch["recipient"])
    message["Message-ID"] = str(batch["message_id"])
    message.set_content("\n\n".join(plain_parts))
    message.add_alternative("<html><body>{}</body></html>".format("".join(html_parts)), subtype="html")
    return message, [int(row["item_id"]) for row in deliverable], skipped


def send_message(config: Dict[str, Any], message: EmailMessage) -> None:
    validate_sendable(config)
    timeout = int(config["timeout_seconds"])
    context = ssl.create_default_context()
    if config["security"] == "ssl":
        client: Any = smtplib.SMTP_SSL(config["smtp_host"], int(config["smtp_port"]), timeout=timeout, context=context)
    else:
        client = smtplib.SMTP(config["smtp_host"], int(config["smtp_port"]), timeout=timeout)
    try:
        client.ehlo()
        if config["security"] == "starttls":
            client.starttls(context=context)
            client.ehlo()
        if config.get("username"):
            client.login(config["username"], config.get("password") or "")
        client.send_message(message)
    finally:
        try:
            client.quit()
        except Exception:
            client.close()


def sanitize_error(error: Exception) -> str:
    message = "{}: {}".format(type(error).__name__, str(error))
    message = re.sub(r"[^@\s]+@[^@\s]+", "[email]", message)
    return message.replace("\r", " ").replace("\n", " ")[:500]


def send_test_email(settings: Settings, db: Database) -> None:
    config, _ = effective_email_config(settings, db, include_secret=True)
    validate_sendable(config, require_enabled=False)
    message = EmailMessage()
    message["Subject"] = "{} 邮件推送测试".format(config.get("subject_prefix") or "[PFTS]")
    message["From"] = formataddr((str(config.get("from_name") or ""), str(config["from_address"])))
    message["To"] = ", ".join(config["to_addresses"])
    message["Message-ID"] = "<pfts-test-{}@local>".format(int(datetime.now(timezone.utc).timestamp() * 1000000))
    message.set_content("这是一封来自公开人物行程追踪系统的测试邮件。")
    message.add_alternative("<html><body><p>这是一封来自公开人物行程追踪系统的测试邮件。</p></body></html>", subtype="html")
    send_message({**config, "enabled": True}, message)


class NotificationWorker:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="pfts-notification-worker", daemon=True)

    def start(self) -> None:
        now = utc_now()
        self.db.execute(
            "UPDATE email_delivery_batches SET status='retrying',next_attempt_at=?,updated_at=? WHERE status='sending'",
            (now, now),
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                config, _ = effective_email_config(self.settings, self.db)
                poll = max(5, int(config.get("worker_poll_seconds", 15)))
                if config.get("enabled"):
                    self.process_once()
            except Exception:
                LOGGER.exception("notification worker iteration failed")
                poll = 15
            self.stop_event.wait(poll)

    def process_once(self) -> Optional[Dict[str, Any]]:
        now = utc_now()
        with self.db.transaction() as connection:
            batch = connection.execute(
                "SELECT * FROM email_delivery_batches WHERE status IN ('pending','retrying') "
                "AND next_attempt_at<=? ORDER BY id LIMIT 1",
                (now,),
            ).fetchone()
            if not batch:
                return None
            batch = dict(batch)
            changed = connection.execute(
                "UPDATE email_delivery_batches SET status='sending',updated_at=? "
                "WHERE id=? AND status IN ('pending','retrying')",
                (now, batch["id"]),
            )
            if not changed.rowcount:
                return None
        try:
            config, _ = effective_email_config(self.settings, self.db, include_secret=True)
            message, deliverable, skipped = build_batch_message(self.db, self.settings, int(batch["id"]), config)
            if skipped:
                placeholders = ",".join("?" for _ in skipped)
                self.db.execute(
                    "UPDATE email_delivery_items SET status='skipped',skip_reason='事件已删除或被驳回' "
                    "WHERE id IN ({})".format(placeholders),
                    skipped,
                )
            if message is None:
                self.db.execute(
                    "UPDATE email_delivery_batches SET status='skipped',updated_at=?,last_error='' WHERE id=?",
                    (utc_now(), batch["id"]),
                )
                return {"id": batch["id"], "status": "skipped"}
            send_message(config, message)
            finished = utc_now()
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE email_delivery_batches SET status='sent',attempt_count=attempt_count+1,"
                    "sent_at=?,updated_at=?,last_error='' WHERE id=?",
                    (finished, finished, batch["id"]),
                )
                if deliverable:
                    placeholders = ",".join("?" for _ in deliverable)
                    connection.execute(
                        "UPDATE email_delivery_items SET status='sent' WHERE id IN ({})".format(placeholders),
                        deliverable,
                    )
            return {"id": batch["id"], "status": "sent"}
        except Exception as exc:
            safe_error = sanitize_error(exc)
            config, _ = effective_email_config(self.settings, self.db)
            attempt = int(batch["attempt_count"]) + 1
            maximum = int(config.get("max_attempts", 5))
            status = "failed" if attempt >= maximum else "retrying"
            delay = int(config.get("retry_base_seconds", 60)) * (2 ** max(0, attempt - 1))
            next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
            self.db.execute(
                "UPDATE email_delivery_batches SET status=?,attempt_count=?,next_attempt_at=?,last_error=?,updated_at=? WHERE id=?",
                (status, attempt, next_attempt, safe_error, utc_now(), batch["id"]),
            )
            LOGGER.warning("email delivery batch %s failed: %s", batch["id"], safe_error)
            return {"id": batch["id"], "status": status, "error": safe_error}
