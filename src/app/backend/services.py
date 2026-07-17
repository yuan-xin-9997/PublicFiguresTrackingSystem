import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

from .collectors import canonicalize_url, collect_source
from .database import Database, json_text
from .extractor import event_core_text, extract
from .security import utc_now


LOGGER = logging.getLogger("pfts.services")


def _event_similarity(left: str, right: str) -> float:
    def normalize(value: str) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", event_core_text(value)).lower()
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) >= 24 and (a in b or b in a):
        return 1.0
    return SequenceMatcher(None, a[:240], b[:240]).ratio()


def _matching_event(connection: Any, event: Dict[str, Any]) -> Optional[int]:
    start = normalize_datetime(event.get("start_at"))
    if not start:
        return None
    day = datetime.fromisoformat(start).date()
    candidates = connection.execute(
        "SELECT id,title,summary,start_at FROM timeline_events WHERE person_id=? AND event_type=? "
        "AND review_status!='rejected' AND start_at IS NOT NULL",
        (event["person_id"], event["event_type"]),
    ).fetchall()
    best_id, best_score = None, 0.0
    for candidate in candidates:
        candidate_day = datetime.fromisoformat(candidate["start_at"]).date()
        if abs((candidate_day - day).days) > 1:
            continue
        score = max(_event_similarity(event["title"], candidate["title"]), _event_similarity(event["summary"], candidate["summary"]))
        if score >= 0.72 and score > best_score:
            best_id, best_score = int(candidate["id"]), score
    return best_id


def audit(
    db: Database, action: str, object_type: str, object_id: Any = "", actor_id: Optional[int] = None,
    result: str = "success", ip_address: str = "", summary: str = "",
) -> None:
    db.execute(
        "INSERT INTO audit_logs(actor_id,action,object_type,object_id,result,ip_address,change_summary,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (actor_id, action, object_type, str(object_id or ""), result, ip_address[:100], summary[:1000], utc_now()),
    )


def normalize_datetime(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def get_persons_for_source(db: Database, source_id: int) -> List[Dict[str, Any]]:
    persons = db.fetch_all(
        "SELECT p.* FROM public_figures p JOIN source_persons sp ON sp.person_id=p.id "
        "WHERE sp.source_id=? AND p.enabled=1 AND p.deleted_at IS NULL ORDER BY p.id", (source_id,),
    )
    if not persons:
        persons = db.fetch_all("SELECT * FROM public_figures WHERE enabled=1 AND deleted_at IS NULL ORDER BY id")
    for person in persons:
        person["aliases"] = [
            row["alias"] for row in db.fetch_all(
                "SELECT alias FROM person_aliases WHERE person_id=? AND enabled=1 ORDER BY id", (person["id"],)
            )
        ]
    return persons


def insert_document(
    db: Database, source_id: int, item: Dict[str, Any], language: str = "", created_by: Optional[int] = None,
) -> Tuple[int, bool]:
    title = str(item.get("title") or "未命名材料").strip()[:500]
    content = str(item.get("content_text") or "").strip()
    if not content:
        raise ValueError("原始文档正文不能为空")
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    url = canonicalize_url(str(item.get("canonical_url") or ""))
    if not url:
        url = "manual://{}".format(uuid.uuid4())
    existing = db.fetch_one(
        "SELECT id FROM raw_documents WHERE source_id=? AND (canonical_url=? OR content_hash=?) ORDER BY id LIMIT 1",
        (source_id, url, content_hash),
    )
    if existing:
        return int(existing["id"]), False
    document_id = db.execute(
        "INSERT INTO raw_documents(source_id,canonical_url,title,author,published_at,collected_at,language,"
        "content_text,content_hash,fetch_metadata_json,status,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,'collected',?)",
        (
            source_id, url, title, str(item.get("author") or "")[:200],
            normalize_datetime(item.get("published_at")), utc_now(), language[:30], content, content_hash,
            json_text(item.get("fetch_metadata") or {}), created_by,
        ),
    )
    return document_id, True


def analyze_document(db: Database, document_id: int, ai_config: Dict[str, Any]) -> int:
    document = db.fetch_one(
        "SELECT d.*,s.trust_level FROM raw_documents d JOIN information_sources s ON s.id=d.source_id WHERE d.id=?",
        (document_id,),
    )
    if not document:
        raise ValueError("原始文档不存在")
    persons = get_persons_for_source(db, int(document["source_id"]))
    result = extract(document, persons, ai_config)
    now = utc_now()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_runs(document_id,provider,model,prompt_version,schema_version,status,latency_ms,usage_json,error_summary,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                document_id, result["provider"], result["model"], "pfts-extract-v1", "event-v1",
                "fallback" if result["error"] else "success", result["latency_ms"], "{}", result["error"], now,
            ),
        )
        event_count = 0
        for event in result["events"]:
            existing = connection.execute(
                "SELECT id FROM timeline_events WHERE dedup_key=?", (event["dedup_key"],)
            ).fetchone()
            if existing:
                event_id = int(existing["id"])
            else:
                matched_id = _matching_event(connection, event)
                if matched_id:
                    event_id = matched_id
                    existing = True
            if existing:
                incoming_start = normalize_datetime(event.get("start_at"))
                incoming_location = event.get("location_name", "")[:300]
                connection.execute(
                    "UPDATE timeline_events SET "
                    "start_at=CASE WHEN ? IS NOT NULL AND (start_at IS NULL OR start_at>?) THEN ? ELSE start_at END,"
                    "location_name=CASE WHEN length(?)>length(location_name) THEN ? ELSE location_name END,"
                    "location_precision=CASE WHEN length(?)>length(location_name) THEN ? ELSE location_precision END,updated_at=? "
                    "WHERE id=? AND human_locked=0",
                    (
                        incoming_start, incoming_start, incoming_start,
                        incoming_location, incoming_location, incoming_location,
                        event.get("location_precision", "unknown"), now, event_id,
                    ),
                )
            if not existing:
                cursor = connection.execute(
                    "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,end_at,original_timezone,time_precision,"
                    "location_name,location_precision,confirmation_status,review_status,confidence,quote_text,translated_text,"
                    "original_language,speech_context,dedup_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event["person_id"], event["event_type"], event["title"][:500], event["summary"][:2000],
                        normalize_datetime(event.get("start_at")), normalize_datetime(event.get("end_at")),
                        event.get("original_timezone", "")[:50], event.get("time_precision", "unknown"),
                        event.get("location_name", "")[:300], event.get("location_precision", "unknown"),
                        event.get("confirmation_status", "rumored"), event.get("review_status", "needs_review"),
                        float(event.get("confidence", 0.5)), event.get("quote_text", "")[:2000],
                        event.get("translated_text", "")[:2000], event.get("original_language", "")[:30],
                        event.get("speech_context", "")[:500], event["dedup_key"], now, now,
                    ),
                )
                event_id = int(cursor.lastrowid)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO event_evidence(event_id,document_id,evidence_text,evidence_locator,supports_fields_json,source_claim_json) "
                "VALUES(?,?,?,'text',?,?)",
                (
                    event_id, document_id, event["evidence_text"][:2000],
                    json_text(["person", "event_type", "title", "time", "location"]),
                    json_text({"source_trust": document["trust_level"]}),
                ),
            )
            if cursor.rowcount:
                event_count += 1
        connection.execute("UPDATE raw_documents SET status='analyzed' WHERE id=?", (document_id,))
    return event_count


def add_task_log(db: Database, run_id: int, level: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
    db.execute(
        "INSERT INTO task_logs(run_id,logged_at,level,message,context_json) VALUES(?,?,?,?,?)",
        (run_id, utc_now(), level, message[:2000], json_text(context or {})),
    )


def run_collection_task(db: Database, task_id: int, config: Dict[str, Any]) -> Dict[str, Any]:
    running = db.fetch_one("SELECT id FROM task_runs WHERE task_id=? AND status='running'", (task_id,))
    if running:
        raise ValueError("该任务已有运行实例")
    task = db.fetch_one(
        "SELECT t.*,s.name AS source_name,s.type,s.entry_url,s.language,s.trust_level,s.parser_config "
        "FROM collection_tasks t JOIN information_sources s ON s.id=t.source_id WHERE t.id=?", (task_id,),
    )
    if not task:
        raise ValueError("任务不存在")
    persons = get_persons_for_source(db, int(task["source_id"]))
    task["discovery_terms"] = [
        term for person in persons for term in [person["name"]] + person.get("aliases", []) if term
    ]
    correlation_id = str(uuid.uuid4())
    started = utc_now()
    run_id = db.execute(
        "INSERT INTO task_runs(task_id,status,started_at,correlation_id) VALUES(?,'running',?,?)",
        (task_id, started, correlation_id),
    )
    counters = {"discovered": 0, "created": 0, "duplicate": 0, "events": 0, "failed": 0}
    error_summary = ""
    status = "success"
    add_task_log(db, run_id, "INFO", "任务开始", {"source": task["source_name"], "correlation_id": correlation_id})
    try:
        documents = collect_source(task, config["collector"], int(config["tasks"].get("max_items_per_run", 50)))
        counters["discovered"] = len(documents)
        discovery_stats = task.get("_discovery_stats")
        if discovery_stats:
            add_task_log(db, run_id, "INFO", "网站发现统计", discovery_stats)
            if not documents:
                add_task_log(db, run_id, "WARNING", "来源可访问，但未发现匹配资讯；请检查关联人物、站内搜索或扫描范围", discovery_stats)
        for item in documents:
            try:
                document_id, created = insert_document(db, int(task["source_id"]), item, str(task["language"] or ""))
                if created:
                    counters["created"] += 1
                    counters["events"] += analyze_document(db, document_id, config["ai"])
                else:
                    counters["duplicate"] += 1
            except Exception as exc:
                counters["failed"] += 1
                add_task_log(db, run_id, "ERROR", "条目处理失败", {"error": str(exc)[:500]})
        if counters["failed"]:
            status = "partial_success" if counters["created"] or counters["duplicate"] else "failed"
    except Exception as exc:
        LOGGER.exception("collection task failed")
        status = "failed"
        counters["failed"] += 1
        error_summary = "{}: {}".format(type(exc).__name__, str(exc)[:500])
        add_task_log(db, run_id, "ERROR", "任务失败", {"error": error_summary})
    finished = utc_now()
    next_run = (datetime.now(timezone.utc) + timedelta(seconds=int(task["schedule_seconds"]))).replace(microsecond=0).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE task_runs SET status=?,finished_at=?,discovered_count=?,created_count=?,duplicate_count=?,event_count=?,failed_count=?,error_summary=? WHERE id=?",
            (status, finished, counters["discovered"], counters["created"], counters["duplicate"], counters["events"], counters["failed"], error_summary, run_id),
        )
        connection.execute("UPDATE collection_tasks SET last_run_at=?,next_run_at=?,updated_at=? WHERE id=?", (finished, next_run, finished, task_id))
        connection.execute("UPDATE information_sources SET last_checked_at=?,last_status=? WHERE id=?", (finished, status, task["source_id"]))
    add_task_log(db, run_id, "INFO", "任务结束", {"status": status, **counters})
    return {"run_id": run_id, "status": status, **counters, "error_summary": error_summary,
            "discovery_stats": task.get("_discovery_stats")}


def event_detail(db: Database, event_id: int) -> Optional[Dict[str, Any]]:
    event = db.fetch_one(
        "SELECT e.*,p.name AS person_name,p.native_name FROM timeline_events e "
        "JOIN public_figures p ON p.id=e.person_id WHERE e.id=?", (event_id,),
    )
    if not event:
        return None
    event["evidence"] = db.fetch_all(
        "SELECT ev.*,d.title AS document_title,d.canonical_url,d.published_at,d.collected_at,s.name AS source_name,s.trust_level "
        "FROM event_evidence ev JOIN raw_documents d ON d.id=ev.document_id "
        "JOIN information_sources s ON s.id=d.source_id WHERE ev.event_id=? ORDER BY ev.id", (event_id,),
    )
    event["history"] = db.fetch_all(
        "SELECT h.*,u.username AS operator_name FROM event_history h LEFT JOIN users u ON u.id=h.operator_id "
        "WHERE h.event_id=? ORDER BY h.created_at DESC", (event_id,),
    )
    return event


def safe_slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return value[:80] or "item"
