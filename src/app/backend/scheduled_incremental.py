import html
import json
import logging
import re
import threading
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import DEFAULT_CONFIG, Settings
from .database import Database, json_text
from .security import utc_now


LOGGER = logging.getLogger("pfts.scheduled_incremental")
EVENT_TYPES = {"itinerary", "statement", "other"}
DELIVERY_MODES = {"immediate", "scheduled_incremental"}
SEND_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
ZERO_WATERMARK = ("", 0, 0)


def _parse_datetime(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def normalize_send_times(values: Any, default: Optional[Sequence[str]] = None) -> List[str]:
    raw = list(default or ["08:30"]) if values is None else values
    if isinstance(raw, str):
        raw = [item for item in re.split(r"[,;，；\s]+", raw) if item]
    if not isinstance(raw, (list, tuple, set)):
        raise ValueError("定时增量发送时刻必须是数组")
    normalized = sorted(set(str(value).strip() for value in raw if str(value).strip()))
    if not normalized or any(not SEND_TIME_RE.match(value) for value in normalized):
        raise ValueError("定时增量规则至少需要一个有效的 HH:mm 发送时刻")
    return normalized


def normalize_incremental_config(values: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {"timezone", "default_send_times", "scheduler_poll_seconds"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError("包含未知定时增量配置字段：{}".format(",".join(sorted(unknown))))
    timezone_name = str(values.get("timezone") or "Asia/Shanghai").strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("定时增量时区必须是有效的 IANA 时区")
    send_times = normalize_send_times(values.get("default_send_times"), ["08:30"])
    try:
        poll = int(values.get("scheduler_poll_seconds", 30))
    except (TypeError, ValueError):
        raise ValueError("scheduler_poll_seconds 必须是整数")
    if poll < 5 or poll > 3600:
        raise ValueError("scheduler_poll_seconds 必须在 5 到 3600 之间")
    return {
        "timezone": timezone_name,
        "default_send_times": send_times,
        "scheduler_poll_seconds": poll,
    }


def effective_incremental_config(
    settings: Optional[Settings],
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    if settings is None:
        values = DEFAULT_CONFIG["notifications"]["scheduled_incremental"]
        normalized = normalize_incremental_config(values)
        return normalized, {field: "default" for field in normalized}
    values = (settings.get("notifications") or {}).get("scheduled_incremental") or {}
    normalized = normalize_incremental_config(values)
    return normalized, {
        field: settings.source("notifications", "scheduled_incremental", field)
        for field in normalized
    }


def next_scheduled_at(
    send_times: Sequence[str],
    timezone_name: str,
    after: Optional[datetime] = None,
) -> datetime:
    clean_times = normalize_send_times(send_times)
    moment = _parse_datetime(after or datetime.now(timezone.utc))
    zone = ZoneInfo(timezone_name)
    local = moment.astimezone(zone)
    for offset in (0, 1):
        target_date = local.date() + timedelta(days=offset)
        for send_time in clean_times:
            hour, minute = (int(part) for part in send_time.split(":"))
            candidate = datetime.combine(target_date, time(hour, minute), tzinfo=zone)
            if candidate > local:
                return candidate.astimezone(timezone.utc)
    raise RuntimeError("无法计算下一次定时增量时刻")


def most_recent_due_at(
    send_times: Sequence[str],
    timezone_name: str,
    now: Optional[datetime] = None,
) -> datetime:
    clean_times = normalize_send_times(send_times)
    moment = _parse_datetime(now or datetime.now(timezone.utc))
    zone = ZoneInfo(timezone_name)
    local = moment.astimezone(zone)
    candidates = []
    for offset in (0, -1):
        target_date = local.date() + timedelta(days=offset)
        for send_time in clean_times:
            hour, minute = (int(part) for part in send_time.split(":"))
            candidate = datetime.combine(target_date, time(hour, minute), tzinfo=zone)
            if candidate <= local:
                candidates.append(candidate)
    if not candidates:
        raise RuntimeError("无法计算最近到期时刻")
    return max(candidates).astimezone(timezone.utc)


def _watermark(connection: Any) -> Tuple[str, int, int]:
    row = connection.execute(
        "SELECT created_at,run_id,event_id FROM task_run_events "
        "ORDER BY created_at DESC,run_id DESC,event_id DESC LIMIT 1"
    ).fetchone()
    return (
        (str(row["created_at"]), int(row["run_id"]), int(row["event_id"]))
        if row else ZERO_WATERMARK
    )


def current_watermark(db: Database) -> Tuple[str, int, int]:
    with db.connect() as connection:
        return _watermark(connection)


def _rule_lists(db: Database, rule_id: int) -> Tuple[List[int], List[int]]:
    tasks = [
        int(row["task_id"]) for row in db.fetch_all(
            "SELECT task_id FROM notification_rule_tasks WHERE rule_id=? ORDER BY task_id",
            (rule_id,),
        )
    ]
    persons = [
        int(row["person_id"]) for row in db.fetch_all(
            "SELECT person_id FROM notification_rule_persons WHERE rule_id=? ORDER BY person_id",
            (rule_id,),
        )
    ]
    return tasks, persons


def _hydrate_rule(db: Database, row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    try:
        item["event_types"] = [
            value for value in json.loads(item.pop("event_types_json"))
            if value in EVENT_TYPES
        ]
    except (TypeError, ValueError):
        item["event_types"] = []
    try:
        item["send_times"] = normalize_send_times(
            json.loads(item.pop("send_times_json")), ["08:30"]
        ) if item.get("delivery_mode") == "scheduled_incremental" else []
    except (TypeError, ValueError):
        item["send_times"] = []
    item["task_ids"], item["person_ids"] = _rule_lists(db, int(item["id"]))
    item["enabled"] = bool(item["enabled"])
    item["cursor"] = {
        "created_at": item.get("cursor_created_at") or "",
        "run_id": int(item.get("cursor_run_id") or 0),
        "event_id": int(item.get("cursor_event_id") or 0),
    }
    item["cursor_reset"] = False
    return item


def list_notification_rules(db: Database) -> List[Dict[str, Any]]:
    return [
        _hydrate_rule(db, row) for row in db.fetch_all(
            "SELECT * FROM notification_rules WHERE deleted_at IS NULL ORDER BY id"
        )
    ]


def get_notification_rule(db: Database, rule_id: int) -> Dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM notification_rules WHERE id=? AND deleted_at IS NULL", (rule_id,)
    )
    if not row:
        raise ValueError("推送规则不存在")
    return _hydrate_rule(db, row)


def save_notification_rule(
    db: Database,
    name: str,
    task_ids: Iterable[int],
    event_types: Iterable[str],
    enabled: bool,
    rule_id: Optional[int] = None,
    person_ids: Optional[Iterable[int]] = None,
    delivery_mode: str = "immediate",
    send_times: Optional[Sequence[str]] = None,
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 200:
        raise ValueError("规则名称不能为空且不能超过 200 个字符")
    clean_tasks = sorted(set(int(value) for value in task_ids))
    clean_types = sorted(set(str(value) for value in event_types))
    clean_persons = sorted(set(int(value) for value in (person_ids or [])))
    clean_mode = str(delivery_mode or "immediate").strip()
    if clean_mode not in DELIVERY_MODES:
        raise ValueError("推送模式必须是 immediate 或 scheduled_incremental")
    config, _ = effective_incremental_config(settings)
    clean_times = normalize_send_times(
        send_times, config["default_send_times"]
    ) if clean_mode == "scheduled_incremental" else []
    if not clean_tasks:
        raise ValueError("至少选择一个采集任务")
    if not clean_types or any(value not in EVENT_TYPES for value in clean_types):
        raise ValueError("至少选择一个有效事件类型")
    task_rows = db.fetch_all(
        "SELECT id FROM collection_tasks WHERE id IN ({})".format(
            ",".join("?" for _ in clean_tasks)
        ), clean_tasks,
    )
    if len(task_rows) != len(clean_tasks):
        raise ValueError("包含不存在的采集任务")
    if clean_persons:
        person_rows = db.fetch_all(
            "SELECT id FROM public_figures WHERE enabled=1 AND deleted_at IS NULL "
            "AND id IN ({})".format(",".join("?" for _ in clean_persons)),
            clean_persons,
        )
        if len(person_rows) != len(clean_persons):
            raise ValueError("包含不存在或不可用的人物")
    old = get_notification_rule(db, rule_id) if rule_id is not None else None
    scopes_changed = bool(old and (
        old["task_ids"] != clean_tasks or old["person_ids"] != clean_persons
        or old["event_types"] != clean_types
    ))
    reset = clean_mode == "scheduled_incremental" and (
        old is None or old["delivery_mode"] != clean_mode or scopes_changed
        or (bool(enabled) and not old["enabled"])
    )
    now = utc_now()
    with db.transaction(immediate=True) as connection:
        watermark = _watermark(connection) if reset else ZERO_WATERMARK
        if rule_id is None:
            next_run = _utc_iso(next_scheduled_at(
                clean_times, config["timezone"]
            )) if clean_mode == "scheduled_incremental" and enabled else None
            cursor = connection.execute(
                "INSERT INTO notification_rules(name,event_types_json,delivery_mode,"
                "send_times_json,enabled,enabled_at,next_run_at,cursor_created_at,"
                "cursor_run_id,cursor_event_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    clean_name, json_text(clean_types), clean_mode, json_text(clean_times),
                    int(enabled), now if enabled else None, next_run,
                    watermark[0] if reset else "", watermark[1] if reset else 0,
                    watermark[2] if reset else 0, now, now,
                ),
            )
            rule_id = int(cursor.lastrowid)
        else:
            previous_cursor = (
                old["cursor_created_at"], int(old["cursor_run_id"]),
                int(old["cursor_event_id"]),
            )
            cursor_value = watermark if reset else previous_cursor
            next_run = None
            if clean_mode == "scheduled_incremental" and enabled:
                next_run = _utc_iso(next_scheduled_at(
                    clean_times, config["timezone"]
                ))
            changed = connection.execute(
                "UPDATE notification_rules SET name=?,event_types_json=?,delivery_mode=?,"
                "send_times_json=?,enabled=?,enabled_at=?,next_run_at=?,cursor_created_at=?,"
                "cursor_run_id=?,cursor_event_id=?,updated_at=? "
                "WHERE id=? AND deleted_at IS NULL",
                (
                    clean_name, json_text(clean_types), clean_mode, json_text(clean_times),
                    int(enabled), now if reset or (enabled and not old.get("enabled_at"))
                    else old.get("enabled_at"), next_run, cursor_value[0], cursor_value[1],
                    cursor_value[2], now, rule_id,
                ),
            )
            if not changed.rowcount:
                raise ValueError("推送规则不存在")
            connection.execute("DELETE FROM notification_rule_tasks WHERE rule_id=?", (rule_id,))
            connection.execute("DELETE FROM notification_rule_persons WHERE rule_id=?", (rule_id,))
        connection.executemany(
            "INSERT INTO notification_rule_tasks(rule_id,task_id) VALUES(?,?)",
            [(rule_id, value) for value in clean_tasks],
        )
        connection.executemany(
            "INSERT INTO notification_rule_persons(rule_id,person_id) VALUES(?,?)",
            [(rule_id, value) for value in clean_persons],
        )
    result = get_notification_rule(db, int(rule_id))
    result["cursor_reset"] = reset
    return result


def delete_notification_rule(db: Database, rule_id: int) -> bool:
    now = utc_now()
    with db.transaction() as connection:
        changed = connection.execute(
            "UPDATE notification_rules SET enabled=0,next_run_at=NULL,deleted_at=?,updated_at=? "
            "WHERE id=? AND deleted_at IS NULL",
            (now, now, rule_id),
        )
        return bool(changed.rowcount)


def _candidate_rows(
    connection: Any,
    rule_id: int,
    lower: Tuple[str, int, int],
    upper: Tuple[str, int, int],
) -> List[Dict[str, Any]]:
    rule = connection.execute(
        "SELECT event_types_json FROM notification_rules WHERE id=?", (rule_id,)
    ).fetchone()
    if not rule or upper <= lower:
        return []
    try:
        event_types = [value for value in json.loads(rule["event_types_json"]) if value in EVENT_TYPES]
    except (TypeError, ValueError):
        return []
    if not event_types:
        return []
    placeholders = ",".join("?" for _ in event_types)
    sql = (
        "SELECT e.id,e.person_id,e.event_type,e.title,e.summary,e.start_at,e.location_name,"
        "e.confirmation_status,e.review_status,p.name AS person_name,"
        "MIN(tre.created_at) AS ingest_created_at "
        "FROM task_run_events tre JOIN task_runs tr ON tr.id=tre.run_id "
        "JOIN notification_rule_tasks nrt ON nrt.task_id=tr.task_id AND nrt.rule_id=? "
        "JOIN timeline_events e ON e.id=tre.event_id "
        "JOIN public_figures p ON p.id=e.person_id "
        "WHERE p.enabled=1 AND p.deleted_at IS NULL AND e.review_status!='rejected' "
        "AND e.event_type IN ({}) "
        "AND (NOT EXISTS(SELECT 1 FROM notification_rule_persons nrp WHERE nrp.rule_id=?) "
        "OR EXISTS(SELECT 1 FROM notification_rule_persons nrp WHERE nrp.rule_id=? "
        "AND nrp.person_id=e.person_id)) "
        "AND (tre.created_at>? OR (tre.created_at=? AND tre.run_id>?) "
        "OR (tre.created_at=? AND tre.run_id=? AND tre.event_id>?)) "
        "AND (tre.created_at<? OR (tre.created_at=? AND tre.run_id<?) "
        "OR (tre.created_at=? AND tre.run_id=? AND tre.event_id<=?)) "
        "GROUP BY e.id ORDER BY e.start_at IS NULL,e.start_at,p.name,e.event_type,e.id"
    ).format(placeholders)
    params: List[Any] = [rule_id] + event_types + [rule_id, rule_id]
    params += [lower[0], lower[0], lower[1], lower[0], lower[1], lower[2]]
    params += [upper[0], upper[0], upper[1], upper[0], upper[1], upper[2]]
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def incremental_candidates(
    db: Database,
    rule_id: int,
    lower: Tuple[str, int, int],
    upper: Tuple[str, int, int],
) -> List[Dict[str, Any]]:
    with db.connect() as connection:
        return _candidate_rows(connection, rule_id, lower, upper)


def preview_incremental(
    db: Database, rule_id: int, sample_limit: int = 20
) -> Dict[str, Any]:
    rule = get_notification_rule(db, rule_id)
    if rule["delivery_mode"] != "scheduled_incremental":
        raise ValueError("只有定时增量规则可以预览")
    lower = (
        rule["cursor_created_at"], int(rule["cursor_run_id"]), int(rule["cursor_event_id"])
    )
    upper = current_watermark(db)
    rows = incremental_candidates(db, rule_id, lower, upper)
    return {
        "rule_id": rule_id,
        "lower": {"created_at": lower[0], "run_id": lower[1], "event_id": lower[2]},
        "upper": {"created_at": upper[0], "run_id": upper[1], "event_id": upper[2]},
        "candidate_count": len(rows),
        "sample": rows[:max(0, min(100, int(sample_limit)))],
    }


def _chunks(values: Sequence[Dict[str, Any]], size: int) -> Iterable[Sequence[Dict[str, Any]]]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def create_incremental_run(
    db: Database,
    settings: Settings,
    rule_id: int,
    scheduled_at: Optional[datetime] = None,
    trigger_type: str = "scheduled",
    triggered_by: Optional[int] = None,
    missed_count: int = 0,
) -> Dict[str, Any]:
    if trigger_type not in {"scheduled", "manual"}:
        raise ValueError("定时增量触发类型无效")
    from .notifications import effective_email_config

    email_config, _ = effective_email_config(settings, db)
    recipients = list(email_config.get("to_addresses") or [])
    if not recipients:
        raise ValueError("邮件配置没有有效收件人")
    planned = _parse_datetime(scheduled_at or datetime.now(timezone.utc))
    planned_iso = _utc_iso(planned)
    now = utc_now()
    config, _ = effective_incremental_config(settings)
    max_events = max(1, min(100, int(email_config.get("max_events_per_message", 25))))
    with db.transaction(immediate=True) as connection:
        raw_rule = connection.execute(
            "SELECT * FROM notification_rules WHERE id=? AND deleted_at IS NULL", (rule_id,)
        ).fetchone()
        if not raw_rule:
            raise ValueError("推送规则不存在")
        if raw_rule["delivery_mode"] != "scheduled_incremental":
            raise ValueError("只有定时增量规则可以创建增量运行")
        if not bool(raw_rule["enabled"]):
            raise ValueError("定时增量规则未启用")
        existing = connection.execute(
            "SELECT * FROM scheduled_notification_runs WHERE rule_id=? AND scheduled_at=?",
            (rule_id, planned_iso),
        ).fetchone()
        if existing:
            return dict(existing)
        lower = (
            str(raw_rule["cursor_created_at"] or ""), int(raw_rule["cursor_run_id"] or 0),
            int(raw_rule["cursor_event_id"] or 0),
        )
        upper = _watermark(connection)
        candidates = _candidate_rows(connection, rule_id, lower, upper)
        status = "pending" if candidates else "empty"
        cursor = connection.execute(
            "INSERT INTO scheduled_notification_runs(rule_id,scheduled_at,trigger_type,"
            "lower_created_at,lower_run_id,lower_event_id,upper_created_at,upper_run_id,"
            "upper_event_id,status,candidate_count,missed_count,triggered_by,created_at,updated_at,"
            "finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rule_id, planned_iso, trigger_type, lower[0], lower[1], lower[2], upper[0],
                upper[1], upper[2], status, len(candidates), max(0, int(missed_count)),
                triggered_by, now, now, now if status == "empty" else None,
            ),
        )
        run_id = int(cursor.lastrowid)
        batch_count = 0
        for recipient in recipients:
            for part_number, chunk in enumerate(_chunks(candidates, max_events), 1):
                batch_cursor = connection.execute(
                    "INSERT INTO scheduled_notification_batches(run_id,recipient,part_number,"
                    "status,next_attempt_at,message_id,created_at,updated_at) "
                    "VALUES(?,?,?,'pending',?,'',?,?)",
                    (run_id, recipient, part_number, now, now, now),
                )
                batch_id = int(batch_cursor.lastrowid)
                connection.execute(
                    "UPDATE scheduled_notification_batches SET message_id=? WHERE id=?",
                    ("<pfts-incremental-{}@local>".format(batch_id), batch_id),
                )
                for event in chunk:
                    connection.execute(
                        "INSERT OR IGNORE INTO scheduled_notification_items("
                        "batch_id,run_id,event_id,recipient,created_at) VALUES(?,?,?,?,?)",
                        (batch_id, run_id, event["id"], recipient, now),
                    )
                batch_count += 1
        send_times = normalize_send_times(json.loads(raw_rule["send_times_json"]))
        next_run = _utc_iso(next_scheduled_at(send_times, config["timezone"], planned))
        connection.execute(
            "UPDATE scheduled_notification_runs SET batch_count=? WHERE id=?",
            (batch_count, run_id),
        )
        connection.execute(
            "UPDATE notification_rules SET cursor_created_at=?,cursor_run_id=?,cursor_event_id=?,"
            "next_run_at=?,updated_at=? WHERE id=?",
            (upper[0], upper[1], upper[2], next_run, now, rule_id),
        )
    return db.fetch_one("SELECT * FROM scheduled_notification_runs WHERE id=?", (run_id,))


def incremental_batch_rows(
    db: Database, batch_id: int
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    batch = db.fetch_one(
        "SELECT b.*,r.rule_id,r.scheduled_at,r.lower_created_at,r.upper_created_at,"
        "nr.name AS rule_name FROM scheduled_notification_batches b "
        "JOIN scheduled_notification_runs r ON r.id=b.run_id "
        "JOIN notification_rules nr ON nr.id=r.rule_id WHERE b.id=?",
        (batch_id,),
    )
    rows = db.fetch_all(
        "SELECT i.id AS item_id,i.event_id,i.status AS item_status,e.event_type,e.title,"
        "e.summary,e.start_at,e.location_name,e.confirmation_status,e.review_status,"
        "p.name AS person_name,p.enabled AS person_enabled,p.deleted_at AS person_deleted_at,"
        "COALESCE((SELECT GROUP_CONCAT(DISTINCT s.name) "
        "FROM event_evidence ev JOIN raw_documents d ON d.id=ev.document_id "
        "JOIN information_sources s ON s.id=d.source_id WHERE ev.event_id=e.id),'') "
        "AS source_names FROM scheduled_notification_items i "
        "LEFT JOIN timeline_events e ON e.id=i.event_id "
        "LEFT JOIN public_figures p ON p.id=e.person_id WHERE i.batch_id=? "
        "ORDER BY e.start_at IS NULL,e.start_at,p.name,e.event_type,e.id",
        (batch_id,),
    )
    return batch, rows


def _beijing(value: Optional[str]) -> str:
    if not value:
        return "起点"
    try:
        return _parse_datetime(value).astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (TypeError, ValueError):
        return str(value)


def build_incremental_message(
    db: Database,
    settings: Settings,
    batch_id: int,
    email_config: Dict[str, Any],
) -> Tuple[Optional[EmailMessage], List[int], List[int]]:
    batch, rows = incremental_batch_rows(db, batch_id)
    if not batch:
        raise ValueError("定时增量投递批次不存在")
    deliverable = [
        row for row in rows
        if row.get("event_id") and row.get("review_status") != "rejected"
        and bool(row.get("person_enabled")) and not row.get("person_deleted_at")
    ]
    skipped = [int(row["item_id"]) for row in rows if row not in deliverable]
    if not deliverable:
        return None, [], skipped
    labels = {"itinerary": "行程", "statement": "言论", "other": "其他"}
    base_url = str(settings.get("server", "base_url", "") or "").rstrip("/") + "/"
    window = "{} 至 {}".format(
        _beijing(batch["lower_created_at"]), _beijing(batch["upper_created_at"])
    )
    plain_parts = ["入库增量窗口：{}（北京时间）".format(window)]
    html_parts = ["<p>入库增量窗口：{}（北京时间）</p>".format(html.escape(window))]
    for index, event in enumerate(deliverable, 1):
        link = urljoin(base_url, "?event_id={}".format(event["event_id"])) if base_url != "/" else ""
        facts = [
            "{}. [{}] {} · {}".format(
                index, labels.get(event["event_type"], event["event_type"]),
                event["person_name"], event["title"],
            ),
            "事件时间：{}（北京时间）".format(_beijing(event["start_at"])),
            "地点：{}".format(event["location_name"] or "未披露"),
            "来源：{}".format(event["source_names"] or "来源未知"),
            str(event["summary"] or ""),
        ]
        if link:
            facts.append("详情：{}".format(link))
        plain_parts.append("\n".join(facts))
        html_parts.append(
            "<article><h3>{}. [{}] {} · {}</h3><p>{}</p><p>事件时间：{}（北京时间）"
            "<br>地点：{}<br>来源：{}{}</p></article>".format(
                index, html.escape(labels.get(event["event_type"], event["event_type"])),
                html.escape(str(event["person_name"] or "")), html.escape(str(event["title"] or "")),
                html.escape(str(event["summary"] or "")), html.escape(_beijing(event["start_at"])),
                html.escape(str(event["location_name"] or "未披露")),
                html.escape(str(event["source_names"] or "来源未知")),
                '<br><a href="{}">查看详情</a>'.format(html.escape(link, quote=True)) if link else "",
            )
        )
    message = EmailMessage()
    prefix = str(email_config.get("subject_prefix") or "[PFTS]").strip()
    suffix = "（第 {} 部分）".format(batch["part_number"]) if int(batch["part_number"]) > 1 else ""
    message["Subject"] = "{} {}：新增 {} 条事件{}".format(
        prefix, batch["rule_name"], len(deliverable), suffix
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


def refresh_incremental_run_status(db: Database, run_id: int) -> Dict[str, Any]:
    counts = db.fetch_one(
        "SELECT COUNT(*) total,SUM(status='sent') sent,SUM(status='failed') failed,"
        "SUM(status='skipped') skipped,SUM(status IN ('pending','sending','retrying')) active "
        "FROM scheduled_notification_batches WHERE run_id=?", (run_id,)
    ) or {}
    total = int(counts.get("total") or 0)
    sent = int(counts.get("sent") or 0)
    failed = int(counts.get("failed") or 0)
    skipped = int(counts.get("skipped") or 0)
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
        "UPDATE scheduled_notification_runs SET status=?,sent_count=?,failed_count=?,"
        "skipped_count=?,updated_at=?,finished_at=CASE WHEN ? IN "
        "('sent','partial','failed','skipped','empty') THEN ? ELSE finished_at END WHERE id=?",
        (status, sent, failed, skipped, now, status, now, run_id),
    )
    return db.fetch_one("SELECT * FROM scheduled_notification_runs WHERE id=?", (run_id,))


class ScheduledIncrementalScheduler:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._loop, name="pfts-scheduled-incremental", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        config, _ = effective_incremental_config(self.settings)
        poll = max(5, int(config["scheduler_poll_seconds"]))
        while not self.stop_event.is_set():
            try:
                self.process_due_once()
            except Exception:
                LOGGER.exception("scheduled incremental iteration failed")
            self.stop_event.wait(poll)

    def process_due_once(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        moment = _parse_datetime(now or datetime.now(timezone.utc))
        config, _ = effective_incremental_config(self.settings)
        due = self.db.fetch_all(
            "SELECT * FROM notification_rules WHERE delivery_mode='scheduled_incremental' "
            "AND enabled=1 AND deleted_at IS NULL AND next_run_at IS NOT NULL "
            "AND next_run_at<=? ORDER BY next_run_at,id", (_utc_iso(moment),)
        )
        results = []
        for row in due:
            send_times = normalize_send_times(json.loads(row["send_times_json"]))
            planned = most_recent_due_at(send_times, config["timezone"], moment)
            first_due = _parse_datetime(row["next_run_at"])
            missed = max(0, int((planned - first_due).total_seconds() // 60))
            try:
                results.append(create_incremental_run(
                    self.db, self.settings, int(row["id"]), planned,
                    trigger_type="scheduled", missed_count=missed,
                ))
            except Exception:
                LOGGER.exception("scheduled incremental rule %s failed", row["id"])
        return results
