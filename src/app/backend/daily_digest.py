import html
import json
import logging
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .database import Database, json_text
from .security import utc_now


LOGGER = logging.getLogger("pfts.daily_digest")
WINDOW_MODES = {"previous_calendar_day", "rolling_hours"}
EVENT_TYPES = {"itinerary", "statement", "other"}
SEND_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_datetime(value: Any, default_timezone: timezone = timezone.utc) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed


def _email_address(value: str) -> str:
    candidate = str(value or "").strip()
    _, parsed = parseaddr(candidate)
    if not candidate or parsed != candidate or not EMAIL_RE.match(parsed):
        raise ValueError("邮箱地址格式无效：{}".format(candidate[:100] or "空值"))
    return parsed.lower()


def normalize_digest_config(values: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "timezone", "default_send_time", "default_window_mode",
        "default_rolling_hours", "max_rolling_hours", "scheduler_poll_seconds",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError("包含未知日报配置字段：{}".format(",".join(sorted(unknown))))
    normalized = {
        "timezone": str(values.get("timezone") or "Asia/Shanghai").strip(),
        "default_send_time": str(values.get("default_send_time") or "08:30").strip(),
        "default_window_mode": str(
            values.get("default_window_mode") or "previous_calendar_day"
        ).strip(),
    }
    try:
        ZoneInfo(normalized["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("日报时区必须是有效的 IANA 时区")
    if not SEND_TIME_RE.match(normalized["default_send_time"]):
        raise ValueError("日报默认发送时间必须是 HH:mm")
    if normalized["default_window_mode"] not in WINDOW_MODES:
        raise ValueError("日报默认汇总模式无效")
    integer_ranges = {
        "default_rolling_hours": (1, 8760, 24),
        "max_rolling_hours": (1, 8760, 168),
        "scheduler_poll_seconds": (5, 3600, 30),
    }
    for field, (minimum, maximum, default) in integer_ranges.items():
        try:
            parsed = int(values.get(field, default))
        except (TypeError, ValueError):
            raise ValueError("{} 必须是整数".format(field))
        if parsed < minimum or parsed > maximum:
            raise ValueError("{} 必须在 {} 到 {} 之间".format(field, minimum, maximum))
        normalized[field] = parsed
    if normalized["default_rolling_hours"] > normalized["max_rolling_hours"]:
        raise ValueError("默认最近小时数不能大于最大最近小时数")
    return normalized


def effective_digest_config(settings: Settings) -> Tuple[Dict[str, Any], Dict[str, str]]:
    values = (settings.get("notifications") or {}).get("daily_digest") or {}
    normalized = normalize_digest_config(values)
    sources = {
        field: settings.source("notifications", "daily_digest", field)
        for field in normalized
    }
    return normalized, sources


def scheduled_datetime(
    scheduled_date: date,
    send_time: str,
    timezone_name: str = "Asia/Shanghai",
) -> datetime:
    if not SEND_TIME_RE.match(str(send_time or "")):
        raise ValueError("发送时间必须是 HH:mm")
    hour, minute = (int(part) for part in send_time.split(":"))
    zone = ZoneInfo(timezone_name)
    return datetime.combine(scheduled_date, time(hour=hour, minute=minute), tzinfo=zone)


def next_scheduled_at(
    send_time: str,
    timezone_name: str = "Asia/Shanghai",
    after: Optional[datetime] = None,
) -> datetime:
    moment = _parse_datetime(after or datetime.now(timezone.utc))
    local = moment.astimezone(ZoneInfo(timezone_name))
    candidate = scheduled_datetime(local.date(), send_time, timezone_name)
    if candidate <= local:
        candidate = scheduled_datetime(local.date() + timedelta(days=1), send_time, timezone_name)
    return candidate.astimezone(timezone.utc)


def digest_window(
    scheduled_date_value: date,
    send_time: str,
    window_mode: str,
    rolling_hours: int,
    timezone_name: str = "Asia/Shanghai",
) -> Tuple[datetime, datetime, datetime]:
    planned = scheduled_datetime(scheduled_date_value, send_time, timezone_name)
    if window_mode == "previous_calendar_day":
        zone = ZoneInfo(timezone_name)
        end = datetime.combine(scheduled_date_value, time.min, tzinfo=zone)
        start = datetime.combine(scheduled_date_value - timedelta(days=1), time.min, tzinfo=zone)
    elif window_mode == "rolling_hours":
        end = planned
        start = end - timedelta(hours=int(rolling_hours))
    else:
        raise ValueError("日报汇总模式无效")
    return (
        planned.astimezone(timezone.utc),
        start.astimezone(timezone.utc),
        end.astimezone(timezone.utc),
    )


def _rule_lists(db: Database, rule_id: int) -> Tuple[List[int], List[str], List[int]]:
    persons = [
        int(row["person_id"]) for row in db.fetch_all(
            "SELECT person_id FROM daily_digest_rule_persons WHERE rule_id=? ORDER BY person_id",
            (rule_id,),
        )
    ]
    recipients = [
        str(row["recipient"]) for row in db.fetch_all(
            "SELECT recipient FROM daily_digest_rule_recipients WHERE rule_id=? ORDER BY recipient",
            (rule_id,),
        )
    ]
    sources = [
        int(row["source_id"]) for row in db.fetch_all(
            "SELECT source_id FROM daily_digest_rule_sources WHERE rule_id=? ORDER BY source_id",
            (rule_id,),
        )
    ]
    return persons, recipients, sources


def _hydrate_rule(db: Database, row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    try:
        item["event_types"] = [
            value for value in json.loads(item.pop("event_types_json"))
            if value in EVENT_TYPES
        ]
    except (TypeError, ValueError):
        item["event_types"] = []
    item["person_ids"], item["recipients"], item["source_ids"] = _rule_lists(db, int(item["id"]))
    item["enabled"] = bool(item["enabled"])
    item["send_when_empty"] = bool(item["send_when_empty"])
    return item


def list_digest_rules(db: Database, include_deleted: bool = False) -> List[Dict[str, Any]]:
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    return [
        _hydrate_rule(db, row)
        for row in db.fetch_all(
            "SELECT * FROM daily_digest_rules {} ORDER BY id".format(where)
        )
    ]


def get_digest_rule(db: Database, rule_id: int, include_deleted: bool = False) -> Dict[str, Any]:
    sql = "SELECT * FROM daily_digest_rules WHERE id=?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    row = db.fetch_one(sql, (rule_id,))
    if not row:
        raise ValueError("日报规则不存在")
    return _hydrate_rule(db, row)


def _normalize_rule_values(
    settings: Settings,
    name: str,
    person_ids: Iterable[int],
    event_types: Iterable[str],
    recipients: Iterable[str],
    source_ids: Iterable[int],
    send_time: Optional[str],
    window_mode: Optional[str],
    rolling_hours: Optional[int],
) -> Dict[str, Any]:
    config, _ = effective_digest_config(settings)
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 200:
        raise ValueError("规则名称不能为空且不能超过 200 个字符")
    clean_persons = sorted(set(int(value) for value in person_ids))
    if not clean_persons:
        raise ValueError("日报规则至少选择一个人物")
    clean_sources = sorted(set(int(value) for value in source_ids))
    clean_types = sorted(set(str(value) for value in event_types))
    if not clean_types or any(value not in EVENT_TYPES for value in clean_types):
        raise ValueError("日报规则至少选择一个有效事件类型")
    clean_recipients = sorted(set(_email_address(value) for value in recipients))
    if not clean_recipients:
        raise ValueError("日报规则至少配置一个收件人")
    clean_time = str(send_time or config["default_send_time"]).strip()
    if not SEND_TIME_RE.match(clean_time):
        raise ValueError("发送时间必须是有效的 HH:mm")
    clean_mode = str(window_mode or config["default_window_mode"]).strip()
    if clean_mode not in WINDOW_MODES:
        raise ValueError("日报汇总模式无效")
    try:
        clean_hours = int(
            config["default_rolling_hours"] if rolling_hours is None else rolling_hours
        )
    except (TypeError, ValueError):
        raise ValueError("最近小时数必须是整数")
    if clean_hours < 1 or clean_hours > int(config["max_rolling_hours"]):
        raise ValueError("最近小时数必须在 1 到 {} 之间".format(config["max_rolling_hours"]))
    return {
        "name": clean_name,
        "person_ids": clean_persons,
        "event_types": clean_types,
        "source_ids": clean_sources,
        "recipients": clean_recipients,
        "send_time": clean_time,
        "window_mode": clean_mode,
        "rolling_hours": clean_hours,
    }


def save_digest_rule(
    db: Database,
    settings: Settings,
    name: str,
    person_ids: Iterable[int],
    event_types: Iterable[str],
    recipients: Iterable[str],
    source_ids: Iterable[int] = (),
    enabled: bool = True,
    send_time: Optional[str] = None,
    window_mode: Optional[str] = None,
    rolling_hours: Optional[int] = None,
    send_when_empty: bool = False,
    rule_id: Optional[int] = None,
) -> Dict[str, Any]:
    values = _normalize_rule_values(
        settings, name, person_ids, event_types, recipients,
        source_ids, send_time, window_mode, rolling_hours,
    )
    placeholders = ",".join("?" for _ in values["person_ids"])
    found = db.fetch_all(
        "SELECT id FROM public_figures WHERE enabled=1 AND deleted_at IS NULL "
        "AND id IN ({})".format(placeholders),
        values["person_ids"],
    )
    if len(found) != len(values["person_ids"]):
        raise ValueError("日报规则包含不存在或不可用的人物")
    if values["source_ids"]:
        source_placeholders = ",".join("?" for _ in values["source_ids"])
        found_sources = db.fetch_all(
            "SELECT id FROM information_sources WHERE deleted_at IS NULL "
            "AND id IN ({})".format(source_placeholders),
            values["source_ids"],
        )
        if len(found_sources) != len(values["source_ids"]):
            raise ValueError("日报规则包含不存在或不可用的信息源")
    config, _ = effective_digest_config(settings)
    now = utc_now()
    next_run = _utc_iso(next_scheduled_at(
        values["send_time"], config["timezone"], _parse_datetime(now)
    )) if enabled else None
    with db.transaction() as connection:
        if rule_id is None:
            cursor = connection.execute(
                "INSERT INTO daily_digest_rules(name,event_types_json,send_time,window_mode,"
                "rolling_hours,send_when_empty,enabled,enabled_at,next_run_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    values["name"], json_text(values["event_types"]), values["send_time"],
                    values["window_mode"], values["rolling_hours"], int(send_when_empty),
                    int(enabled), now if enabled else None, next_run, now, now,
                ),
            )
            rule_id = int(cursor.lastrowid)
        else:
            current = connection.execute(
                "SELECT enabled,enabled_at FROM daily_digest_rules "
                "WHERE id=? AND deleted_at IS NULL",
                (rule_id,),
            ).fetchone()
            if not current:
                raise ValueError("日报规则不存在")
            enabled_at = current["enabled_at"]
            if enabled and not bool(current["enabled"]):
                enabled_at = now
            connection.execute(
                "UPDATE daily_digest_rules SET name=?,event_types_json=?,send_time=?,window_mode=?,"
                "rolling_hours=?,send_when_empty=?,enabled=?,enabled_at=?,next_run_at=?,updated_at=? "
                "WHERE id=?",
                (
                    values["name"], json_text(values["event_types"]), values["send_time"],
                    values["window_mode"], values["rolling_hours"], int(send_when_empty),
                    int(enabled), enabled_at, next_run, now, rule_id,
                ),
            )
            connection.execute(
                "DELETE FROM daily_digest_rule_persons WHERE rule_id=?", (rule_id,)
            )
            connection.execute(
                "DELETE FROM daily_digest_rule_recipients WHERE rule_id=?", (rule_id,)
            )
            connection.execute(
                "DELETE FROM daily_digest_rule_sources WHERE rule_id=?", (rule_id,)
            )
        connection.executemany(
            "INSERT INTO daily_digest_rule_persons(rule_id,person_id) VALUES(?,?)",
            [(rule_id, value) for value in values["person_ids"]],
        )
        connection.executemany(
            "INSERT INTO daily_digest_rule_recipients(rule_id,recipient) VALUES(?,?)",
            [(rule_id, value) for value in values["recipients"]],
        )
        connection.executemany(
            "INSERT INTO daily_digest_rule_sources(rule_id,source_id) VALUES(?,?)",
            [(rule_id, value) for value in values["source_ids"]],
        )
    return get_digest_rule(db, int(rule_id))


def delete_digest_rule(db: Database, rule_id: int) -> bool:
    now = utc_now()
    with db.transaction() as connection:
        changed = connection.execute(
            "UPDATE daily_digest_rules SET enabled=0,next_run_at=NULL,deleted_at=?,updated_at=? "
            "WHERE id=? AND deleted_at IS NULL",
            (now, now, rule_id),
        )
        return bool(changed.rowcount)


def mask_digest_rule(rule: Dict[str, Any], reveal_recipients: bool) -> Dict[str, Any]:
    item = dict(rule)
    if not reveal_recipients:
        item["recipients"] = [
            "{}***@{}".format(value[:1], value.split("@", 1)[1])
            for value in item.get("recipients", [])
        ]
    return item


def _event_timestamp(row: Dict[str, Any]) -> Optional[datetime]:
    raw = row.get("start_at") or row.get("created_at")
    if not raw:
        return None
    default_zone = timezone.utc
    if row.get("start_at") and row.get("original_timezone"):
        try:
            default_zone = ZoneInfo(str(row["original_timezone"]))
        except ZoneInfoNotFoundError:
            default_zone = timezone.utc
    try:
        return _parse_datetime(raw, default_zone).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def digest_candidates(
    db: Database,
    person_ids: Sequence[int],
    event_types: Sequence[str],
    window_start: datetime,
    window_end: datetime,
    source_ids: Optional[Sequence[int]] = None,
) -> List[Dict[str, Any]]:
    if not person_ids or not event_types:
        return []
    person_placeholders = ",".join("?" for _ in person_ids)
    type_placeholders = ",".join("?" for _ in event_types)
    params: List[Any] = list(person_ids) + list(event_types)
    source_clause = ""
    clean_sources = [int(value) for value in (source_ids or []) if value]
    if clean_sources:
        source_placeholders = ",".join("?" for _ in clean_sources)
        source_clause = (
            " AND EXISTS (SELECT 1 FROM event_evidence ev "
            "JOIN raw_documents d ON d.id=ev.document_id "
            "WHERE ev.event_id=e.id AND d.source_id IN ({}))".format(source_placeholders)
        )
        params += clean_sources
    rows = db.fetch_all(
        "SELECT e.*,p.name AS person_name,"
        "COALESCE((SELECT GROUP_CONCAT(DISTINCT s.name) FROM event_evidence ev "
        "JOIN raw_documents d ON d.id=ev.document_id "
        "JOIN information_sources s ON s.id=d.source_id "
        "WHERE ev.event_id=e.id),'') AS source_names "
        "FROM timeline_events e JOIN public_figures p ON p.id=e.person_id "
        "WHERE e.review_status!='rejected' AND p.enabled=1 AND p.deleted_at IS NULL "
        "AND e.person_id IN ({}) AND e.event_type IN ({}){}".format(
            person_placeholders, type_placeholders, source_clause
        ),
        params,
    )
    selected = []
    for row in rows:
        selected_at = _event_timestamp(row)
        if selected_at is not None and window_start <= selected_at < window_end:
            selected.append(row)
    selected.sort(key=lambda row: (
        row.get("start_at") is None,
        _event_timestamp({**row, "created_at": row.get("start_at")}) or datetime.max.replace(tzinfo=timezone.utc),
        int(row["person_id"]),
        str(row["event_type"]),
        int(row["id"]),
    ))
    return selected


def preview_digest(
    db: Database,
    settings: Settings,
    rule_id: int,
    scheduled_date_value: Optional[date] = None,
    sample_limit: int = 20,
) -> Dict[str, Any]:
    rule = get_digest_rule(db, rule_id)
    config, _ = effective_digest_config(settings)
    if scheduled_date_value is None:
        next_at = _parse_datetime(
            rule.get("next_run_at") or _utc_iso(
                next_scheduled_at(rule["send_time"], config["timezone"])
            )
        ).astimezone(ZoneInfo(config["timezone"]))
        scheduled_date_value = next_at.date()
    planned, start, end = digest_window(
        scheduled_date_value, rule["send_time"], rule["window_mode"],
        int(rule["rolling_hours"]), config["timezone"],
    )
    rows = digest_candidates(
        db, rule["person_ids"], rule["event_types"], start, end,
        source_ids=rule.get("source_ids"),
    )
    return {
        "rule_id": rule_id,
        "scheduled_date": scheduled_date_value.isoformat(),
        "scheduled_at": _utc_iso(planned),
        "window_start": _utc_iso(start),
        "window_end": _utc_iso(end),
        "timezone": config["timezone"],
        "candidate_count": len(rows),
        "sample": [
            {
                "id": row["id"], "person_name": row["person_name"],
                "event_type": row["event_type"], "title": row["title"],
                "start_at": row["start_at"],
            }
            for row in rows[:max(0, min(100, int(sample_limit)))]
        ],
    }


def _chunks(values: Sequence[Dict[str, Any]], size: int) -> Iterable[Sequence[Dict[str, Any]]]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def create_digest_run(
    db: Database,
    settings: Settings,
    rule_id: int,
    scheduled_date_value: date,
    trigger_type: str = "scheduled",
    missed_count: int = 0,
) -> Dict[str, Any]:
    if trigger_type not in {"scheduled", "manual"}:
        raise ValueError("日报触发类型无效")
    rule = get_digest_rule(db, rule_id)
    config, _ = effective_digest_config(settings)
    planned, start, end = digest_window(
        scheduled_date_value, rule["send_time"], rule["window_mode"],
        int(rule["rolling_hours"]), config["timezone"],
    )
    if trigger_type == "manual" and rule.get("enabled_at"):
        enabled_local_date = _parse_datetime(rule["enabled_at"]).astimezone(
            ZoneInfo(config["timezone"])
        ).date()
        if scheduled_date_value < enabled_local_date:
            raise ValueError("不能补跑规则启用之前的业务日期")
    existing = db.fetch_one(
        "SELECT * FROM daily_digest_runs WHERE rule_id=? AND scheduled_date=?",
        (rule_id, scheduled_date_value.isoformat()),
    )
    if existing:
        return existing
    candidates = digest_candidates(
        db, rule["person_ids"], rule["event_types"], start, end,
        source_ids=rule.get("source_ids"),
    )
    now = utc_now()
    max_events = int(
        ((settings.get("notifications") or {}).get("email") or {}).get(
            "max_events_per_message", 25
        )
    )
    max_events = max(1, min(100, max_events))
    with db.transaction() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO daily_digest_runs(rule_id,scheduled_date,scheduled_at,"
            "window_start,window_end,trigger_type,status,candidate_count,missed_count,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?, ?,?,?,?)",
            (
                rule_id, scheduled_date_value.isoformat(), _utc_iso(planned),
                _utc_iso(start), _utc_iso(end), trigger_type,
                "pending" if candidates or rule["send_when_empty"] else "empty",
                len(candidates), max(0, int(missed_count)), now, now,
            ),
        )
        run_row = connection.execute(
            "SELECT * FROM daily_digest_runs WHERE rule_id=? AND scheduled_date=?",
            (rule_id, scheduled_date_value.isoformat()),
        ).fetchone()
        run_id = int(run_row["id"])
        if not cursor.rowcount:
            return dict(run_row)
        batch_count = 0
        chunks = list(_chunks(candidates, max_events))
        if not chunks and rule["send_when_empty"]:
            chunks = [[]]
        for recipient in rule["recipients"]:
            for part_number, chunk in enumerate(chunks, 1):
                batch_cursor = connection.execute(
                    "INSERT INTO daily_digest_batches(run_id,recipient,part_number,status,"
                    "next_attempt_at,last_error,message_id,created_at,updated_at) "
                    "VALUES(?,?,?,'pending',?,'','',?,?)",
                    (run_id, recipient, part_number, now, now, now),
                )
                batch_id = int(batch_cursor.lastrowid)
                connection.execute(
                    "UPDATE daily_digest_batches SET message_id=? WHERE id=?",
                    ("<pfts-digest-{}@local>".format(batch_id), batch_id),
                )
                for event in chunk:
                    connection.execute(
                        "INSERT OR IGNORE INTO daily_digest_items("
                        "batch_id,run_id,event_id,recipient,created_at) VALUES(?,?,?,?,?)",
                        (batch_id, run_id, event["id"], recipient, now),
                    )
                batch_count += 1
        connection.execute(
            "UPDATE daily_digest_runs SET batch_count=?,updated_at=?,"
            "finished_at=CASE WHEN status='empty' THEN ? ELSE finished_at END WHERE id=?",
            (batch_count, now, now, run_id),
        )
    return db.fetch_one("SELECT * FROM daily_digest_runs WHERE id=?", (run_id,))


def digest_batch_rows(
    db: Database, batch_id: int
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    batch = db.fetch_one(
        "SELECT b.*,r.rule_id,r.scheduled_date,r.window_start,r.window_end,"
        "dr.name AS rule_name,dr.send_when_empty "
        "FROM daily_digest_batches b JOIN daily_digest_runs r ON r.id=b.run_id "
        "JOIN daily_digest_rules dr ON dr.id=r.rule_id WHERE b.id=?",
        (batch_id,),
    )
    rows = db.fetch_all(
        "SELECT i.id AS item_id,i.event_id,i.status AS item_status,e.event_type,e.title,"
        "e.summary,e.start_at,e.location_name,e.confirmation_status,e.review_status,"
        "p.name AS person_name,"
        "COALESCE((SELECT GROUP_CONCAT(DISTINCT s.name) FROM event_evidence ev "
        "JOIN raw_documents d ON d.id=ev.document_id "
        "JOIN information_sources s ON s.id=d.source_id "
        "WHERE ev.event_id=e.id),'') AS source_names "
        "FROM daily_digest_items i LEFT JOIN timeline_events e ON e.id=i.event_id "
        "LEFT JOIN public_figures p ON p.id=e.person_id WHERE i.batch_id=? "
        "ORDER BY e.start_at IS NULL,e.start_at,p.id,e.event_type,e.id",
        (batch_id,),
    )
    return batch, rows


def _beijing(value: Optional[str]) -> str:
    if not value:
        return "时间未知"
    try:
        return _parse_datetime(value).astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value)


def build_digest_message(
    db: Database,
    settings: Settings,
    batch_id: int,
    email_config: Dict[str, Any],
) -> Tuple[Optional[EmailMessage], List[int], List[int]]:
    batch, rows = digest_batch_rows(db, batch_id)
    if not batch:
        raise ValueError("日报投递批次不存在")
    deliverable = [
        row for row in rows
        if row.get("event_id") and row.get("review_status") != "rejected"
    ]
    skipped = [int(row["item_id"]) for row in rows if row not in deliverable]
    if not deliverable and rows and not bool(batch["send_when_empty"]):
        return None, [], skipped
    labels = {"itinerary": "行程", "statement": "言论", "other": "其他"}
    base_url = str(settings.get("server", "base_url", "") or "").rstrip("/") + "/"
    window_label = "{} 至 {}".format(
        _beijing(batch["window_start"]), _beijing(batch["window_end"])
    )
    plain_parts: List[str] = ["汇总窗口：{}（北京时间，左闭右开）".format(window_label)]
    html_parts: List[str] = [
        "<p>汇总窗口：{}（北京时间，左闭右开）</p>".format(html.escape(window_label))
    ]
    if not deliverable:
        plain_parts.append("本期没有符合规则的公开动态。")
        html_parts.append("<p>本期没有符合规则的公开动态。</p>")
    for index, event in enumerate(deliverable, 1):
        link = urljoin(base_url, "?event_id={}".format(event["event_id"])) if base_url != "/" else ""
        facts = [
            "{}. [{}] {} · {}".format(
                index, labels.get(event["event_type"], event["event_type"]),
                event["person_name"], event["title"],
            ),
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
                html.escape(str(event["person_name"] or "")),
                html.escape(str(event["title"] or "")),
                html.escape(str(event["summary"] or "")),
                html.escape(_beijing(event["start_at"])),
                html.escape(str(event["location_name"] or "未披露")),
                html.escape(str(event["confirmation_status"])),
                html.escape(str(event["review_status"])),
                html.escape(str(event["source_names"] or "来源未知")),
                '<br><a href="{}">查看详情</a>'.format(html.escape(link, quote=True)) if link else "",
            )
        )
    message = EmailMessage()
    prefix = str(email_config.get("subject_prefix") or "[PFTS]").strip()
    part_suffix = "（第 {} 部分）".format(batch["part_number"]) if int(batch["part_number"]) > 1 else ""
    message["Subject"] = "{} {}日报 {}：{} 条事件{}".format(
        prefix, batch["rule_name"], batch["scheduled_date"], len(deliverable), part_suffix
    )
    message["From"] = formataddr((
        str(email_config.get("from_name") or ""), str(email_config["from_address"])
    ))
    message["To"] = str(batch["recipient"])
    message["Message-ID"] = str(batch["message_id"])
    message.set_content("\n\n".join(plain_parts))
    message.add_alternative(
        "<html><body>{}</body></html>".format("".join(html_parts)), subtype="html"
    )
    return message, [int(row["item_id"]) for row in deliverable], skipped


def refresh_digest_run_status(db: Database, run_id: int) -> Dict[str, Any]:
    counts = db.fetch_one(
        "SELECT COUNT(*) AS total,"
        "SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,"
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,"
        "SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,"
        "SUM(CASE WHEN status IN ('pending','sending','retrying') THEN 1 ELSE 0 END) AS active "
        "FROM daily_digest_batches WHERE run_id=?",
        (run_id,),
    ) or {}
    total = int(counts.get("total") or 0)
    sent = int(counts.get("sent") or 0)
    failed = int(counts.get("failed") or 0)
    active = int(counts.get("active") or 0)
    if total == 0:
        status = "empty"
    elif active:
        status = "sending" if sent else "pending"
    elif failed and sent:
        status = "partial"
    elif failed:
        status = "failed"
    elif sent:
        status = "sent"
    else:
        status = "skipped"
    now = utc_now()
    db.execute(
        "UPDATE daily_digest_runs SET status=?,sent_count=?,failed_count=?,updated_at=?,"
        "finished_at=CASE WHEN ? IN ('sent','partial','failed','skipped','empty') THEN ? "
        "ELSE finished_at END WHERE id=?",
        (status, sent, failed, now, status, now, run_id),
    )
    return db.fetch_one("SELECT * FROM daily_digest_runs WHERE id=?", (run_id,))


def most_recent_due_date(
    send_time: str, timezone_name: str, now: Optional[datetime] = None
) -> date:
    local = _parse_datetime(now or datetime.now(timezone.utc)).astimezone(
        ZoneInfo(timezone_name)
    )
    today_schedule = scheduled_datetime(local.date(), send_time, timezone_name)
    return local.date() if local >= today_schedule else local.date() - timedelta(days=1)


class DailyDigestScheduler:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._loop, name="pfts-daily-digest-scheduler", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        config, _ = effective_digest_config(self.settings)
        poll = max(5, int(config["scheduler_poll_seconds"]))
        while not self.stop_event.is_set():
            try:
                self.process_due_once()
            except Exception:
                LOGGER.exception("daily digest scheduler iteration failed")
            self.stop_event.wait(poll)

    def process_due_once(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        moment = _parse_datetime(now or datetime.now(timezone.utc))
        config, _ = effective_digest_config(self.settings)
        due_rules = self.db.fetch_all(
            "SELECT * FROM daily_digest_rules WHERE enabled=1 AND deleted_at IS NULL "
            "AND next_run_at IS NOT NULL AND next_run_at<=? ORDER BY id",
            (_utc_iso(moment),),
        )
        results = []
        for raw_rule in due_rules:
            rule = _hydrate_rule(self.db, raw_rule)
            due_date = most_recent_due_date(
                rule["send_time"], config["timezone"], moment
            )
            first_due = _parse_datetime(rule["next_run_at"]).astimezone(
                ZoneInfo(config["timezone"])
            ).date()
            missed = max(0, (due_date - first_due).days)
            try:
                results.append(create_digest_run(
                    self.db, self.settings, int(rule["id"]), due_date,
                    trigger_type="scheduled", missed_count=missed,
                ))
            finally:
                next_run = _utc_iso(next_scheduled_at(
                    rule["send_time"], config["timezone"], moment
                ))
                self.db.execute(
                    "UPDATE daily_digest_rules SET next_run_at=?,updated_at=? WHERE id=?",
                    (next_run, utc_now(), rule["id"]),
                )
        return results
