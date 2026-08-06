import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

from .collectors import (
    _article_rejection_reason,
    canonicalize_url,
    clean_article_content,
    collect_source,
    is_chinadaily_url,
)
from .config import Settings
from .database import Database, json_text
from .extractor import event_core_text, extract, validate_document_evidence_subject
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


def analyze_document(
    db: Database,
    document_id: int,
    ai_config: Dict[str, Any],
    stats_out: Optional[Dict[str, Any]] = None,
    task_run_id: Optional[int] = None,
    new_event_ids: Optional[List[int]] = None,
) -> int:
    document = db.fetch_one(
        "SELECT d.*,s.trust_level FROM raw_documents d JOIN information_sources s ON s.id=d.source_id WHERE d.id=?",
        (document_id,),
    )
    if not document:
        raise ValueError("原始文档不存在")
    persons = get_persons_for_source(db, int(document["source_id"]))
    result = extract(document, persons, ai_config)
    attribution_stats = result.get("attribution_stats") or {}
    if stats_out is not None:
        stats_out.update(attribution_stats)
    now = utc_now()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_runs(document_id,provider,model,prompt_version,schema_version,status,latency_ms,usage_json,error_summary,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                document_id, result["provider"], result["model"], "pfts-extract-v2", "event-v2",
                "fallback" if result["error"] else "success", result["latency_ms"],
                json_text({"attribution": attribution_stats}), result["error"], now,
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
                        event.get("confirmation_status", "rumored"), "approved",
                        float(event.get("confidence", 0.5)), event.get("quote_text", "")[:2000],
                        event.get("translated_text", "")[:2000], event.get("original_language", "")[:30],
                        event.get("speech_context", "")[:500], event["dedup_key"], now, now,
                    ),
                )
                event_id = int(cursor.lastrowid)
                if task_run_id is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO task_run_events(run_id,event_id,created_at) VALUES(?,?,?)",
                        (task_run_id, event_id, now),
                    )
                if new_event_ids is not None:
                    new_event_ids.append(event_id)
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


def run_collection_task(db: Database, task_id: int, settings: Settings) -> Dict[str, Any]:
    config = settings.values
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
    counters = {
        "discovered": 0, "created": 0, "duplicate": 0, "events": 0, "failed": 0,
        "attribution_candidates": 0, "attribution_accepted": 0, "attribution_rejected": 0,
    }
    attribution_reasons: Dict[str, int] = {}
    new_event_ids: List[int] = []
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
                    analysis_stats: Dict[str, Any] = {}
                    counters["events"] += analyze_document(
                        db, document_id, config["ai"], analysis_stats,
                        task_run_id=run_id, new_event_ids=new_event_ids,
                    )
                    counters["attribution_candidates"] += int(analysis_stats.get("candidates", 0))
                    counters["attribution_accepted"] += int(analysis_stats.get("accepted", 0))
                    counters["attribution_rejected"] += int(analysis_stats.get("rejected", 0))
                    for reason, count in (analysis_stats.get("rejection_reasons") or {}).items():
                        attribution_reasons[reason] = attribution_reasons.get(reason, 0) + int(count)
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
    add_task_log(
        db, run_id, "INFO", "人物主体归属统计",
        {
            "candidates": counters["attribution_candidates"],
            "accepted": counters["attribution_accepted"],
            "rejected": counters["attribution_rejected"],
            "rejection_reasons": attribution_reasons,
        },
    )
    return {"run_id": run_id, "status": status, **counters, "error_summary": error_summary,
            "discovery_stats": task.get("_discovery_stats")}


def _person_with_aliases(db: Database, person_id: int) -> Optional[Dict[str, Any]]:
    person = db.fetch_one("SELECT * FROM public_figures WHERE id=?", (person_id,))
    if not person:
        return None
    person["aliases"] = [
        row["alias"] for row in db.fetch_all(
            "SELECT alias FROM person_aliases WHERE person_id=? AND enabled=1 ORDER BY id", (person_id,),
        )
    ]
    return person


def recheck_event_attribution(
    db: Database,
    dry_run: bool = True,
    person_id: Optional[int] = None,
    source_id: Optional[int] = None,
    limit: int = 5000,
) -> Dict[str, Any]:
    where = []
    params: List[Any] = []
    if person_id is not None:
        where.append("e.person_id=?")
        params.append(person_id)
    if source_id is not None:
        where.append("d.source_id=?")
        params.append(source_id)
    clause = "WHERE " + " AND ".join(where) if where else ""
    rows = db.fetch_all(
        "SELECT ev.id AS evidence_id,ev.event_id,ev.evidence_text,e.person_id,e.event_type,e.human_locked,"
        "d.id AS document_id,d.source_id,d.title,d.content_text,d.published_at,d.language,"
        "p.name AS person_name,s.name AS source_name "
        "FROM event_evidence ev JOIN timeline_events e ON e.id=ev.event_id "
        "JOIN raw_documents d ON d.id=ev.document_id JOIN public_figures p ON p.id=e.person_id "
        "JOIN information_sources s ON s.id=d.source_id " + clause + " ORDER BY ev.id LIMIT ?",
        [*params, min(max(int(limit), 1), 10000)],
    )
    person_cache: Dict[int, Dict[str, Any]] = {}
    source_person_cache: Dict[int, List[Dict[str, Any]]] = {}
    invalid_rows: List[Dict[str, Any]] = []
    locked_skipped = 0
    accepted = 0
    reasons: Dict[str, int] = {}
    scanned_event_ids = set()
    for row in rows:
        scanned_event_ids.add(int(row["event_id"]))
        if int(row["human_locked"]):
            locked_skipped += 1
            continue
        target = person_cache.get(int(row["person_id"]))
        if target is None:
            target = _person_with_aliases(db, int(row["person_id"]))
            if not target:
                continue
            person_cache[int(row["person_id"])] = target
        persons = source_person_cache.get(int(row["source_id"]))
        if persons is None:
            persons = get_persons_for_source(db, int(row["source_id"]))
            if not any(int(person["id"]) == int(target["id"]) for person in persons):
                persons = [target, *persons]
            source_person_cache[int(row["source_id"])] = persons
        document = {
            "title": row["title"], "content_text": row["content_text"],
            "published_at": row["published_at"], "language": row["language"],
        }
        valid, reason = validate_document_evidence_subject(
            document, str(row["evidence_text"]), target, persons, str(row["event_type"]),
        )
        if valid:
            accepted += 1
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
        invalid_rows.append({
            "evidence_id": int(row["evidence_id"]), "event_id": int(row["event_id"]),
            "document_id": int(row["document_id"]), "person_name": row["person_name"],
            "source_name": row["source_name"], "title": row["title"], "reason": reason,
            "evidence_text": str(row["evidence_text"])[:300],
        })
    invalid_ids = [row["evidence_id"] for row in invalid_rows]
    invalid_by_event: Dict[int, int] = {}
    for row in invalid_rows:
        invalid_by_event[row["event_id"]] = invalid_by_event.get(row["event_id"], 0) + 1
    orphan_event_ids = []
    kept_event_ids = []
    for event_id, invalid_count in invalid_by_event.items():
        total = int(db.fetch_one("SELECT COUNT(*) n FROM event_evidence WHERE event_id=?", (event_id,))["n"])
        if total == invalid_count:
            orphan_event_ids.append(event_id)
        else:
            kept_event_ids.append(event_id)
    result = {
        "dry_run": dry_run, "scanned_evidence": len(rows), "scanned_events": len(scanned_event_ids),
        "accepted_evidence": accepted, "invalid_evidence": len(invalid_ids),
        "kept_events": len(kept_event_ids), "orphan_events": len(orphan_event_ids),
        "locked_skipped": locked_skipped, "rejection_reasons": reasons, "sample": invalid_rows[:20],
    }
    if dry_run or not invalid_ids:
        return result
    with db.transaction() as connection:
        evidence_placeholders = ",".join("?" for _ in invalid_ids)
        connection.execute(
            "DELETE FROM event_evidence WHERE id IN ({})".format(evidence_placeholders), invalid_ids,
        )
        if orphan_event_ids:
            event_placeholders = ",".join("?" for _ in orphan_event_ids)
            connection.execute(
                "DELETE FROM timeline_events WHERE id IN ({}) AND human_locked=0".format(event_placeholders),
                orphan_event_ids,
            )
    result["deleted"] = True
    return result


def cleanup_chinadaily_documents(
    db: Database,
    ai_config: Dict[str, Any],
    dry_run: bool = True,
    source_id: Optional[int] = None,
    limit: int = 2000,
) -> Dict[str, Any]:
    where = "WHERE d.source_id=?" if source_id is not None else ""
    params: List[Any] = [source_id] if source_id is not None else []
    documents = db.fetch_all(
        "SELECT d.*,s.name AS source_name FROM raw_documents d "
        "JOIN information_sources s ON s.id=d.source_id " + where + " ORDER BY d.id LIMIT ?",
        [*params, min(max(int(limit), 1), 5000)],
    )
    rejected: List[Dict[str, Any]] = []
    cleanable: List[Dict[str, Any]] = []
    locked_skipped = 0
    chinadaily_scanned = 0
    for document in documents:
        if not is_chinadaily_url(str(document["canonical_url"])):
            continue
        chinadaily_scanned += 1
        locked = db.fetch_one(
            "SELECT 1 found FROM event_evidence ev JOIN timeline_events e ON e.id=ev.event_id "
            "WHERE ev.document_id=? AND e.human_locked=1 LIMIT 1", (document["id"],),
        )
        if locked:
            locked_skipped += 1
            continue
        normalized_original = " ".join(str(document["content_text"]).split()).strip()
        cleaned = clean_article_content(
            str(document["canonical_url"]), str(document["title"]), str(document["content_text"]),
        )
        candidate = dict(document)
        candidate["content_text"] = cleaned
        reason = _article_rejection_reason(candidate)
        item = {
            "id": int(document["id"]), "title": document["title"],
            "canonical_url": document["canonical_url"], "source_name": document["source_name"],
            "reason": reason or "正文包含可清理的页面框架",
        }
        if reason:
            rejected.append(item)
        elif cleaned != normalized_original:
            item["cleaned_content"] = cleaned
            cleanable.append(item)
    impacted_ids = [item["id"] for item in [*rejected, *cleanable]]
    evidence_count = 0
    orphan_event_ids: List[int] = []
    if impacted_ids:
        placeholders = ",".join("?" for _ in impacted_ids)
        evidence_count = int(db.fetch_one(
            "SELECT COUNT(*) n FROM event_evidence WHERE document_id IN ({})".format(placeholders),
            impacted_ids,
        )["n"])
        orphan_event_ids = [
            int(row["event_id"]) for row in db.fetch_all(
                "SELECT DISTINCT ev.event_id FROM event_evidence ev JOIN timeline_events e ON e.id=ev.event_id "
                "WHERE ev.document_id IN ({0}) AND e.human_locked=0 "
                "AND NOT EXISTS (SELECT 1 FROM event_evidence keep WHERE keep.event_id=ev.event_id "
                "AND keep.document_id NOT IN ({0}))".format(placeholders),
                impacted_ids + impacted_ids,
            )
        ]
    result = {
        "dry_run": dry_run, "scanned_documents": chinadaily_scanned,
        "rejected_documents": len(rejected), "cleanable_documents": len(cleanable),
        "affected_evidence": evidence_count, "orphan_events": len(orphan_event_ids),
        "locked_skipped": locked_skipped,
        "sample": [
            {key: value for key, value in item.items() if key != "cleaned_content"}
            for item in [*rejected, *cleanable][:20]
        ],
    }
    if dry_run or not impacted_ids:
        return result
    rejected_ids = [item["id"] for item in rejected]
    cleanable_ids = [item["id"] for item in cleanable]
    with db.transaction() as connection:
        placeholders = ",".join("?" for _ in impacted_ids)
        connection.execute("DELETE FROM model_runs WHERE document_id IN ({})".format(placeholders), impacted_ids)
        connection.execute("DELETE FROM event_evidence WHERE document_id IN ({})".format(placeholders), impacted_ids)
        if orphan_event_ids:
            event_placeholders = ",".join("?" for _ in orphan_event_ids)
            connection.execute(
                "DELETE FROM timeline_events WHERE id IN ({}) AND human_locked=0".format(event_placeholders),
                orphan_event_ids,
            )
        if rejected_ids:
            rejected_placeholders = ",".join("?" for _ in rejected_ids)
            connection.execute("DELETE FROM attachments WHERE document_id IN ({})".format(rejected_placeholders), rejected_ids)
            connection.execute("DELETE FROM raw_documents WHERE id IN ({})".format(rejected_placeholders), rejected_ids)
        for item in cleanable:
            cleaned = str(item["cleaned_content"])
            connection.execute(
                "UPDATE raw_documents SET content_text=?,content_hash=?,status='collected',version=version+1 WHERE id=?",
                (cleaned, hashlib.sha256(cleaned.encode("utf-8")).hexdigest(), item["id"]),
            )
    reanalysis_errors = []
    reanalyzed_events = 0
    for document_id in cleanable_ids:
        try:
            reanalyzed_events += analyze_document(db, document_id, ai_config)
        except Exception as exc:
            reanalysis_errors.append({"document_id": document_id, "error": str(exc)[:300]})
    result.update({
        "deleted": True, "reanalyzed_documents": len(cleanable_ids) - len(reanalysis_errors),
        "reanalyzed_events": reanalyzed_events, "reanalysis_errors": reanalysis_errors[:10],
    })
    return result


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
