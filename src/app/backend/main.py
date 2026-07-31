import json
import logging
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .collectors import _article_rejection_reason, collect_source
from .config import Settings, load_config
from .daily_digest import (
    DailyDigestScheduler,
    create_digest_run,
    delete_digest_rule,
    effective_digest_config,
    get_digest_rule,
    list_digest_rules,
    mask_digest_rule,
    normalize_digest_config,
    preview_digest,
    save_digest_rule,
)
from .database import Database, json_text
from .notifications import (
    EVENT_TYPES,
    NotificationWorker,
    delete_rule,
    effective_email_config,
    list_rules,
    masked_email_config,
    normalize_email_config,
    sanitize_error,
    save_email_overrides,
    save_rule,
    send_test_email,
)
from .scheduler import Scheduler
from .security import (
    ALL_PAGES, create_session, current_user, parse_password_file, require_admin, require_page,
    revoke_session, sync_users, user_pages, utc_now, verify_password,
)
from .services import (
    analyze_document,
    audit,
    cleanup_chinadaily_documents,
    event_detail,
    get_persons_for_source,
    insert_document,
    recheck_event_attribution,
    run_collection_task,
)


LOGGER = logging.getLogger("pfts")


def model_values(model: BaseModel) -> Dict[str, Any]:
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


class PersonBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    native_name: str = Field(default="", max_length=200)
    bio: str = Field(default="", max_length=5000)
    organization: str = Field(default="", max_length=300)
    title: str = Field(default="", max_length=300)
    country_region: str = Field(default="", max_length=100)
    language: str = Field(default="", max_length=30)
    avatar_path: str = Field(default="", max_length=500)
    enabled: bool = True
    aliases: List[str] = Field(default_factory=list)


class SourceBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str
    entry_url: str = Field(default="", max_length=2000)
    organization: str = Field(default="", max_length=300)
    language: str = Field(default="", max_length=30)
    trust_level: int = Field(default=3, ge=1, le=5)
    schedule_seconds: int = Field(default=3600, ge=60, le=2_592_000)
    enabled: bool = True
    person_ids: List[int] = Field(default_factory=list)
    discovery_enabled: bool = False
    discovery_max_pages: int = Field(default=12, ge=1, le=50)
    discovery_max_depth: int = Field(default=1, ge=0, le=2)


class ManualDocumentBody(BaseModel):
    source_id: int
    title: str = Field(min_length=1, max_length=500)
    content_text: str = Field(min_length=1, max_length=500_000)
    canonical_url: str = Field(default="", max_length=2000)
    author: str = Field(default="", max_length=200)
    published_at: Optional[str] = None


class TaskBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_id: int
    schedule_seconds: int = Field(default=3600, ge=60, le=2_592_000)
    enabled: bool = True


class ReviewBody(BaseModel):
    action: str
    reason: str = Field(default="", max_length=2000)
    title: Optional[str] = Field(default=None, max_length=500)
    summary: Optional[str] = Field(default=None, max_length=2000)
    confirmation_status: Optional[str] = None
    start_at: Optional[str] = None
    location_name: Optional[str] = Field(default=None, max_length=300)


class PermissionBody(BaseModel):
    pages: List[str]


class EmailConfigBody(BaseModel):
    enabled: Optional[bool] = None
    smtp_host: Optional[str] = Field(default=None, max_length=255)
    smtp_port: Optional[int] = None
    security: Optional[str] = None
    username: Optional[str] = Field(default=None, max_length=300)
    password_env: Optional[str] = Field(default=None, max_length=200)
    credential_key_env: Optional[str] = Field(default=None, max_length=200)
    from_address: Optional[str] = Field(default=None, max_length=320)
    from_name: Optional[str] = Field(default=None, max_length=200)
    to_addresses: Optional[List[str]] = None
    subject_prefix: Optional[str] = Field(default=None, max_length=100)
    max_events_per_message: Optional[int] = None
    worker_poll_seconds: Optional[int] = None
    max_attempts: Optional[int] = None
    retry_base_seconds: Optional[int] = None
    timeout_seconds: Optional[int] = None
    password: str = Field(default="", max_length=1000)
    clear_password: bool = False
    clear_fields: List[str] = Field(default_factory=list)


class NotificationRuleBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    task_ids: List[int]
    person_ids: List[int] = Field(default_factory=list)
    event_types: List[str]
    enabled: bool = True


class DailyDigestRuleBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    person_ids: List[int]
    event_types: List[str]
    recipients: List[str]
    send_time: Optional[str] = Field(default=None, max_length=5)
    window_mode: Optional[str] = None
    rolling_hours: Optional[int] = None
    send_when_empty: bool = False
    enabled: bool = True


class DailyDigestDateBody(BaseModel):
    scheduled_date: Optional[str] = Field(default=None, max_length=10)


class CleanupNavigationBody(BaseModel):
    dry_run: bool = True


class AttributionMaintenanceBody(BaseModel):
    dry_run: bool = True
    person_id: Optional[int] = None
    source_id: Optional[int] = None
    limit: int = Field(default=5000, ge=1, le=10000)


class ChinaDailyMaintenanceBody(BaseModel):
    dry_run: bool = True
    source_id: Optional[int] = None
    limit: int = Field(default=2000, ge=1, le=5000)


def configure_logging(settings: Settings) -> None:
    log_path = settings.path("logging", "path")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, str(settings.get("logging", "level", "INFO")).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(handler, TimedRotatingFileHandler) and Path(handler.baseFilename) == log_path for handler in root.handlers):
        handler = TimedRotatingFileHandler(
            str(log_path), when="midnight", backupCount=int(settings.get("logging", "retention_days", 30)), encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(console)


def _list_response(items: List[Dict[str, Any]], total: int, page: int, page_size: int) -> Dict[str, Any]:
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def create_app(config_path: Optional[str] = None) -> FastAPI:
    settings = load_config(config_path)
    normalize_email_config((settings.get("notifications") or {}).get("email") or {})
    normalize_digest_config(
        (settings.get("notifications") or {}).get("daily_digest") or {}
    )
    configure_logging(settings)
    db = Database(settings.path("database", "path"), int(settings.get("database", "busy_timeout_ms", 5000)))
    scheduler_holder: Dict[str, Optional[Scheduler]] = {"instance": None}
    notification_holder: Dict[str, Optional[NotificationWorker]] = {"instance": None}
    digest_scheduler_holder: Dict[str, Optional[DailyDigestScheduler]] = {"instance": None}

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        db.initialize()
        sync_users(db, settings.path("security", "password_file"))
        notification_holder["instance"] = NotificationWorker(db, settings)
        notification_holder["instance"].start()
        digest_scheduler_holder["instance"] = DailyDigestScheduler(db, settings)
        digest_scheduler_holder["instance"].start()
        if settings.get("tasks", "scheduler_enabled", False):
            scheduler_holder["instance"] = Scheduler(db, settings)
            scheduler_holder["instance"].start()
        yield
        if scheduler_holder["instance"]:
            scheduler_holder["instance"].stop()
        if digest_scheduler_holder["instance"]:
            digest_scheduler_holder["instance"].stop()
        if notification_holder["instance"]:
            notification_holder["instance"].stop()

    application = FastAPI(title=settings.get("app", "name"), version="1.0.0", lifespan=lifespan)
    application.state.db = db
    application.state.settings = settings

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))[:100]
        try:
            response = await call_next(request)
        except Exception:
            LOGGER.exception("request failed request_id=%s path=%s", request_id, request.url.path)
            response = JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "服务器内部错误"}, "request_id": request_id})
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND"}.get(exc.status_code, "REQUEST_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail)}, "request_id": request.headers.get("X-Request-ID", "")},
            headers=exc.headers,
        )

    @application.get("/api/v1/health/live")
    def health_live():
        return {"status": "ok", "time": utc_now()}

    @application.get("/api/v1/health/ready")
    def health_ready():
        row = db.fetch_one("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
        if not row:
            raise HTTPException(503, "数据库未初始化")
        return {"status": "ready", "database_schema": row["version"], "time": utc_now()}

    @application.post("/api/v1/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        sync_users(db, settings.path("security", "password_file"))
        key = "{}|{}".format(body.username.lower(), _client_ip(request))
        attempt = db.fetch_one("SELECT * FROM login_attempts WHERE attempt_key=?", (key,))
        window = int(settings.get("security", "login_window_seconds", 300))
        max_attempts = int(settings.get("security", "login_max_attempts", 8))
        now = datetime.now(timezone.utc)
        if attempt:
            first = datetime.fromisoformat(attempt["first_at"])
            if (now - first).total_seconds() > window:
                db.execute("DELETE FROM login_attempts WHERE attempt_key=?", (key,))
                attempt = None
            elif int(attempt["failures"]) >= max_attempts:
                raise HTTPException(429, "登录失败次数过多，请稍后再试")
        user = db.fetch_one("SELECT * FROM users WHERE username=? AND enabled=1", (body.username,))
        if not user or not verify_password(body.password, user["password_hash"]):
            if attempt:
                db.execute("UPDATE login_attempts SET failures=failures+1,last_at=? WHERE attempt_key=?", (utc_now(), key))
            else:
                db.execute("INSERT INTO login_attempts(attempt_key,failures,first_at,last_at) VALUES(?,1,?,?)", (key, utc_now(), utc_now()))
            audit(db, "login", "session", actor_id=user["id"] if user else None, result="failed", ip_address=_client_ip(request))
            raise HTTPException(401, "用户名或密码错误")
        db.execute("DELETE FROM login_attempts WHERE attempt_key=?", (key,))
        token = create_session(db, int(user["id"]), int(settings.get("security", "session_hours", 12)))
        db.execute("UPDATE users SET last_login_at=?,updated_at=? WHERE id=?", (utc_now(), utc_now(), user["id"]))
        response.set_cookie(
            "pfts_session", token, max_age=int(settings.get("security", "session_hours", 12)) * 3600,
            httponly=True, samesite="lax", secure=bool(settings.get("security", "cookie_secure", False)), path="/",
        )
        audit(db, "login", "session", actor_id=user["id"], ip_address=_client_ip(request))
        return {"user": {"id": user["id"], "username": user["username"], "role": user["role"], "pages": user_pages(db, user)}}

    @application.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response, user: Dict[str, Any] = Depends(current_user)):
        token = request.cookies.get("pfts_session")
        if token:
            revoke_session(db, token)
        response.delete_cookie("pfts_session", path="/")
        audit(db, "logout", "session", actor_id=user["id"], ip_address=_client_ip(request))
        return {"ok": True}

    @application.get("/api/v1/auth/me")
    def me(user: Dict[str, Any] = Depends(current_user)):
        return {"id": user["id"], "username": user["username"], "role": user["role"], "pages": user["pages"]}

    @application.get("/api/v1/dashboard/summary")
    def dashboard(user: Dict[str, Any] = Depends(require_page("dashboard"))):
        def count(sql: str, params: tuple = ()) -> int:
            return int(db.fetch_one(sql, params)["n"])
        recent = db.fetch_all(
            "SELECT e.id,e.event_type,e.title,e.start_at,e.confirmation_status,e.review_status,e.confidence,p.name AS person_name "
            "FROM timeline_events e JOIN public_figures p ON p.id=e.person_id WHERE e.review_status!='rejected' "
            "ORDER BY COALESCE(e.start_at,e.created_at) DESC,e.id DESC LIMIT 8"
        )
        failed = db.fetch_all(
            "SELECT r.id,r.status,r.started_at,r.error_summary,t.name AS task_name FROM task_runs r "
            "JOIN collection_tasks t ON t.id=r.task_id WHERE r.status IN ('failed','partial_success') ORDER BY r.id DESC LIMIT 5"
        )
        return {
            "counts": {
                "persons": count("SELECT COUNT(*) n FROM public_figures WHERE enabled=1 AND deleted_at IS NULL"),
                "sources": count("SELECT COUNT(*) n FROM information_sources WHERE enabled=1"),
                "documents_today": count("SELECT COUNT(*) n FROM raw_documents WHERE substr(collected_at,1,10)=substr(?,1,10)", (utc_now(),)),
                "events_today": count("SELECT COUNT(*) n FROM timeline_events WHERE review_status!='rejected' AND substr(created_at,1,10)=substr(?,1,10)", (utc_now(),)),
                "needs_review": count("SELECT COUNT(*) n FROM timeline_events WHERE review_status IN ('pending','needs_review')"),
                "failed_tasks": count("SELECT COUNT(*) n FROM task_runs WHERE status IN ('failed','partial_success')"),
            }, "recent_events": recent, "failed_runs": failed,
        }

    def _person_row(person_id: int) -> Dict[str, Any]:
        person = db.fetch_one("SELECT * FROM public_figures WHERE id=? AND deleted_at IS NULL", (person_id,))
        if not person:
            raise HTTPException(404, "人物不存在")
        person["aliases"] = [row["alias"] for row in db.fetch_all("SELECT alias FROM person_aliases WHERE person_id=? AND enabled=1", (person_id,))]
        return person

    @application.get("/api/v1/persons")
    def list_persons(q: str = "", user: Dict[str, Any] = Depends(require_page("persons"))):
        params: List[Any] = []
        where = "WHERE p.deleted_at IS NULL"
        if q:
            where += " AND (p.name LIKE ? OR p.native_name LIKE ? OR p.organization LIKE ? OR EXISTS(SELECT 1 FROM person_aliases a WHERE a.person_id=p.id AND a.alias LIKE ?))"
            params.extend(["%" + q + "%"] * 4)
        items = db.fetch_all(
            "SELECT p.*,(SELECT COUNT(*) FROM timeline_events e WHERE e.person_id=p.id) event_count FROM public_figures p " + where + " ORDER BY p.enabled DESC,p.name", params
        )
        for item in items:
            item["aliases"] = [row["alias"] for row in db.fetch_all("SELECT alias FROM person_aliases WHERE person_id=? AND enabled=1", (item["id"],))]
        return {"items": items, "total": len(items)}

    @application.get("/api/v1/persons/{person_id}")
    def get_person(person_id: int, user: Dict[str, Any] = Depends(require_page("persons"))):
        return _person_row(person_id)

    @application.post("/api/v1/persons", status_code=201)
    def create_person(body: PersonBody, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        values = model_values(body)
        now = utc_now()
        with db.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO public_figures(name,native_name,bio,organization,title,country_region,language,avatar_path,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (values["name"], values["native_name"], values["bio"], values["organization"], values["title"], values["country_region"], values["language"], values["avatar_path"], int(values["enabled"]), now, now),
            )
            person_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT OR IGNORE INTO person_aliases(person_id,alias) VALUES(?,?)",
                [(person_id, alias.strip()) for alias in values["aliases"] if alias.strip()],
            )
        audit(db, "create", "person", person_id, user["id"], ip_address=_client_ip(request), summary=values["name"])
        return _person_row(person_id)

    @application.put("/api/v1/persons/{person_id}")
    def update_person(person_id: int, body: PersonBody, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        _person_row(person_id)
        values = model_values(body)
        with db.transaction() as connection:
            connection.execute(
                "UPDATE public_figures SET name=?,native_name=?,bio=?,organization=?,title=?,country_region=?,language=?,avatar_path=?,enabled=?,updated_at=? WHERE id=?",
                (values["name"], values["native_name"], values["bio"], values["organization"], values["title"], values["country_region"], values["language"], values["avatar_path"], int(values["enabled"]), utc_now(), person_id),
            )
            connection.execute("DELETE FROM person_aliases WHERE person_id=?", (person_id,))
            connection.executemany("INSERT OR IGNORE INTO person_aliases(person_id,alias) VALUES(?,?)", [(person_id, a.strip()) for a in values["aliases"] if a.strip()])
        audit(db, "update", "person", person_id, user["id"], ip_address=_client_ip(request), summary=values["name"])
        return _person_row(person_id)

    @application.delete("/api/v1/persons/{person_id}")
    def delete_person(person_id: int, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        person = _person_row(person_id)
        now = utc_now()
        db.execute(
            "UPDATE public_figures SET enabled=0,deleted_at=?,updated_at=? WHERE id=?",
            (now, now, person_id),
        )
        audit(
            db, "delete", "person", person_id, user["id"], ip_address=_client_ip(request),
            summary="软删除人物：{}；历史事件和证据保留".format(person["name"]),
        )
        return {"ok": True, "id": person_id, "deleted_at": now}

    @application.get("/api/v1/sources")
    def list_sources(user: Dict[str, Any] = Depends(require_page("sources"))):
        items = db.fetch_all(
            "SELECT s.*,(SELECT COUNT(*) FROM raw_documents d WHERE d.source_id=s.id) document_count "
            "FROM information_sources s WHERE s.deleted_at IS NULL ORDER BY s.id DESC"
        )
        for item in items:
            item["person_ids"] = [row["person_id"] for row in db.fetch_all("SELECT person_id FROM source_persons WHERE source_id=?", (item["id"],))]
            try:
                parser = json.loads(item.get("parser_config") or "{}")
            except ValueError:
                parser = {}
            item["discovery_enabled"] = bool(parser.get("discovery_enabled", False))
            item["discovery_max_pages"] = int(parser.get("discovery_max_pages", 12))
            item["discovery_max_depth"] = int(parser.get("discovery_max_depth", 1))
            item["display_type"] = "website" if item["discovery_enabled"] else item["type"]
        return {"items": items, "total": len(items)}

    def _validate_source(values: Dict[str, Any]) -> None:
        if values["type"] not in {"rss", "web_page", "website", "manual"}:
            raise HTTPException(422, "不支持的来源类型")
        if values["type"] != "manual" and not values["entry_url"]:
            raise HTTPException(422, "RSS/网页来源必须填写入口 URL")
        if (values["type"] == "website" or values.get("discovery_enabled")) and not values["person_ids"]:
            raise HTTPException(422, "网站自动发现来源必须至少关联一个人物")

    def _source_storage_values(values: Dict[str, Any]) -> tuple:
        discovery_enabled = bool(values.get("discovery_enabled") or values["type"] == "website")
        stored_type = "web_page" if values["type"] == "website" else values["type"]
        parser_config = json_text({
            "discovery_enabled": discovery_enabled,
            "discovery_max_pages": int(values.get("discovery_max_pages", 12)),
            "discovery_max_depth": int(values.get("discovery_max_depth", 1)),
        })
        return stored_type, parser_config

    @application.post("/api/v1/sources", status_code=201)
    def create_source(body: SourceBody, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        values = model_values(body)
        _validate_source(values)
        stored_type, parser_config = _source_storage_values(values)
        now = utc_now()
        with db.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO information_sources(name,type,entry_url,organization,language,trust_level,schedule_seconds,parser_config,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (values["name"], stored_type, values["entry_url"], values["organization"], values["language"], values["trust_level"], values["schedule_seconds"], parser_config, int(values["enabled"]), now, now),
            )
            source_id = int(cursor.lastrowid)
            connection.executemany("INSERT OR IGNORE INTO source_persons(source_id,person_id) VALUES(?,?)", [(source_id, pid) for pid in values["person_ids"]])
            connection.execute(
                "INSERT INTO collection_tasks(name,source_id,schedule_seconds,enabled,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("采集：" + values["name"], source_id, values["schedule_seconds"], int(values["enabled"]), now, now, now),
            )
        audit(db, "create", "source", source_id, user["id"], ip_address=_client_ip(request), summary=values["name"])
        return {"id": source_id, **values, "type": stored_type, "display_type": "website" if values.get("discovery_enabled") or values["type"] == "website" else stored_type}

    @application.put("/api/v1/sources/{source_id}")
    def update_source(source_id: int, body: SourceBody, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        if not db.fetch_one("SELECT id FROM information_sources WHERE id=? AND deleted_at IS NULL", (source_id,)):
            raise HTTPException(404, "来源不存在")
        values = model_values(body)
        _validate_source(values)
        stored_type, parser_config = _source_storage_values(values)
        with db.transaction() as connection:
            connection.execute(
                "UPDATE information_sources SET name=?,type=?,entry_url=?,organization=?,language=?,trust_level=?,schedule_seconds=?,parser_config=?,enabled=?,updated_at=? WHERE id=?",
                (values["name"], stored_type, values["entry_url"], values["organization"], values["language"], values["trust_level"], values["schedule_seconds"], parser_config, int(values["enabled"]), utc_now(), source_id),
            )
            connection.execute("UPDATE collection_tasks SET name=?,schedule_seconds=?,enabled=?,updated_at=? WHERE source_id=?", ("采集：" + values["name"], values["schedule_seconds"], int(values["enabled"]), utc_now(), source_id))
            connection.execute("DELETE FROM source_persons WHERE source_id=?", (source_id,))
            connection.executemany("INSERT OR IGNORE INTO source_persons(source_id,person_id) VALUES(?,?)", [(source_id, pid) for pid in values["person_ids"]])
        audit(db, "update", "source", source_id, user["id"], ip_address=_client_ip(request), summary=values["name"])
        return {"id": source_id, **values, "type": stored_type, "display_type": "website" if values.get("discovery_enabled") or values["type"] == "website" else stored_type}

    @application.delete("/api/v1/sources/{source_id}")
    def delete_source(source_id: int, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        source = db.fetch_one("SELECT * FROM information_sources WHERE id=? AND deleted_at IS NULL", (source_id,))
        if not source:
            raise HTTPException(404, "来源不存在")
        now = utc_now()
        with db.transaction() as connection:
            connection.execute("UPDATE information_sources SET enabled=0,deleted_at=?,updated_at=? WHERE id=?", (now, now, source_id))
            connection.execute("UPDATE collection_tasks SET enabled=0,updated_at=? WHERE source_id=?", (now, source_id))
        audit(db, "delete", "source", source_id, user["id"], ip_address=_client_ip(request), summary="软删除信息源：{}；历史材料保留".format(source["name"]))
        return {"ok": True, "id": source_id, "deleted_at": now}

    @application.post("/api/v1/sources/{source_id}/test")
    def test_source(source_id: int, user: Dict[str, Any] = Depends(require_admin)):
        source = db.fetch_one("SELECT * FROM information_sources WHERE id=? AND deleted_at IS NULL", (source_id,))
        if not source:
            raise HTTPException(404, "来源不存在")
        if source["type"] == "manual":
            return {"ok": True, "status": "manual", "parsed_count": 0, "message": "人工来源无需网络测试"}
        try:
            persons = get_persons_for_source(db, source_id)
            source["discovery_terms"] = [term for person in persons for term in [person["name"]] + person.get("aliases", [])]
            docs = collect_source(source, settings.get("collector"), 2)
            return {"ok": True, "status": 200, "parsed_count": len(docs), "message": "来源可用"}
        except Exception as exc:
            raise HTTPException(400, "来源测试失败：{}".format(str(exc)[:300]))

    @application.get("/api/v1/tasks")
    def list_tasks(user: Dict[str, Any] = Depends(require_page("tasks"))):
        items = db.fetch_all(
            "SELECT t.*,s.name AS source_name,(SELECT status FROM task_runs r WHERE r.task_id=t.id ORDER BY r.id DESC LIMIT 1) last_status "
            "FROM collection_tasks t JOIN information_sources s ON s.id=t.source_id ORDER BY t.id DESC"
        )
        return {"items": items, "total": len(items)}

    @application.post("/api/v1/tasks", status_code=201)
    def create_task(body: TaskBody, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        if not db.fetch_one("SELECT id FROM information_sources WHERE id=?", (body.source_id,)):
            raise HTTPException(404, "来源不存在")
        now = utc_now()
        task_id = db.execute(
            "INSERT INTO collection_tasks(name,source_id,schedule_seconds,enabled,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (body.name, body.source_id, body.schedule_seconds, int(body.enabled), now, now, now),
        )
        audit(db, "create", "task", task_id, user["id"], ip_address=_client_ip(request), summary=body.name)
        return db.fetch_one("SELECT * FROM collection_tasks WHERE id=?", (task_id,))

    @application.post("/api/v1/tasks/{task_id}/run")
    def run_task(task_id: int, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        try:
            result = run_collection_task(db, task_id, settings)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        audit(db, "run", "task", task_id, user["id"], ip_address=_client_ip(request), summary=result["status"])
        return result

    @application.get("/api/v1/task-runs")
    def list_runs(task_id: Optional[int] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200), user: Dict[str, Any] = Depends(require_page("tasks"))):
        where = "WHERE r.task_id=?" if task_id else ""
        params: List[Any] = [task_id] if task_id else []
        total = int(db.fetch_one("SELECT COUNT(*) n FROM task_runs r " + where, params)["n"])
        params.extend([page_size, (page - 1) * page_size])
        items = db.fetch_all(
            "SELECT r.*,t.name AS task_name,"
            "(SELECT COUNT(*) FROM email_delivery_batches b WHERE b.task_run_id=r.id) AS notification_batches,"
            "(SELECT COUNT(*) FROM email_delivery_items i WHERE i.task_run_id=r.id) AS notification_items,"
            "COALESCE((SELECT b.last_error FROM email_delivery_batches b WHERE b.task_run_id=r.id "
            "AND b.status='failed' ORDER BY b.id DESC LIMIT 1),'') AS notification_error "
            "FROM task_runs r JOIN collection_tasks t ON t.id=r.task_id " + where + " ORDER BY r.id DESC LIMIT ? OFFSET ?", params
        )
        return _list_response(items, total, page, page_size)

    @application.get("/api/v1/task-runs/{run_id}/logs")
    def run_logs(run_id: int, user: Dict[str, Any] = Depends(require_page("tasks"))):
        return {"items": db.fetch_all("SELECT * FROM task_logs WHERE run_id=? ORDER BY id", (run_id,))}

    @application.get("/api/v1/documents")
    def list_documents(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200), user: Dict[str, Any] = Depends(require_page("timeline"))):
        total = int(db.fetch_one("SELECT COUNT(*) n FROM raw_documents")["n"])
        items = db.fetch_all(
            "SELECT d.id,d.title,d.canonical_url,d.published_at,d.collected_at,d.status,s.name AS source_name "
            "FROM raw_documents d JOIN information_sources s ON s.id=d.source_id ORDER BY d.id DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        )
        return _list_response(items, total, page, page_size)

    @application.get("/api/v1/documents/{document_id}")
    def get_document(document_id: int, user: Dict[str, Any] = Depends(require_page("timeline"))):
        document = db.fetch_one(
            "SELECT d.*,s.name AS source_name FROM raw_documents d JOIN information_sources s ON s.id=d.source_id WHERE d.id=?", (document_id,)
        )
        if not document:
            raise HTTPException(404, "文档不存在")
        document["events"] = db.fetch_all(
            "SELECT e.id,e.title,e.event_type,p.name AS person_name FROM event_evidence ev JOIN timeline_events e ON e.id=ev.event_id "
            "JOIN public_figures p ON p.id=e.person_id WHERE ev.document_id=?", (document_id,),
        )
        return document

    @application.post("/api/v1/documents/manual", status_code=201)
    def create_manual_document(body: ManualDocumentBody, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        source = db.fetch_one("SELECT * FROM information_sources WHERE id=?", (body.source_id,))
        if not source or source["type"] != "manual":
            raise HTTPException(422, "请选择人工来源")
        document_id, created = insert_document(db, body.source_id, model_values(body), str(source["language"] or ""), user["id"])
        event_count = analyze_document(db, document_id, settings.get("ai")) if created else 0
        audit(db, "create", "document", document_id, user["id"], ip_address=_client_ip(request), summary=body.title)
        return {"id": document_id, "created": created, "event_count": event_count}

    @application.post("/api/v1/documents/{document_id}/reanalyze")
    def reanalyze_document(document_id: int, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        try:
            count = analyze_document(db, document_id, settings.get("ai"))
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        audit(db, "reanalyze", "document", document_id, user["id"], ip_address=_client_ip(request), summary="events={}".format(count))
        return {"id": document_id, "event_count": count}

    @application.get("/api/v1/events")
    def list_events(
        page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200), person_id: Optional[int] = None,
        event_type: str = "", confirmation_status: str = "", review_status: str = "", q: str = "",
        start_date: str = "", end_date: str = "", sort_order: str = "desc",
        location: List[str] = Query(default=[]),
        user: Dict[str, Any] = Depends(require_page("timeline")),
    ):
        if sort_order not in {"asc", "desc"}:
            raise HTTPException(422, "排序方向必须是 asc 或 desc")
        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if start_date and not date_pattern.match(start_date):
            raise HTTPException(422, "开始日期格式必须是 YYYY-MM-DD")
        if end_date and not date_pattern.match(end_date):
            raise HTTPException(422, "结束日期格式必须是 YYYY-MM-DD")
        if start_date and end_date and start_date > end_date:
            raise HTTPException(422, "开始日期不能晚于结束日期")
        clauses: List[str] = [
            "NOT (e.event_type='other' AND EXISTS ("
            "SELECT 1 FROM event_evidence oe JOIN event_evidence same_doc ON same_doc.document_id=oe.document_id "
            "JOIN timeline_events preferred ON preferred.id=same_doc.event_id "
            "WHERE oe.event_id=e.id AND preferred.person_id=e.person_id AND preferred.event_type='statement' "
            "AND preferred.review_status!='rejected'))"
        ]
        params: List[Any] = []
        if not review_status:
            clauses.append("e.review_status!='rejected'")
        for field, value in (("e.person_id", person_id), ("e.event_type", event_type), ("e.confirmation_status", confirmation_status), ("e.review_status", review_status)):
            if value not in (None, ""):
                clauses.append(field + "=?")
                params.append(value)
        if q:
            clauses.append("(e.title LIKE ? OR e.summary LIKE ? OR e.location_name LIKE ? OR e.quote_text LIKE ? OR p.name LIKE ?)")
            params.extend(["%" + q + "%"] * 5)
        if start_date:
            clauses.append("substr(e.start_at,1,10)>=?")
            params.append(start_date)
        if end_date:
            clauses.append("substr(e.start_at,1,10)<=?")
            params.append(end_date)
        locations = list(dict.fromkeys(item.strip() for item in location if item.strip()))
        if locations:
            clauses.append("e.location_name IN ({})".format(",".join("?" for _ in locations)))
            params.extend(locations)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        base = " FROM timeline_events e JOIN public_figures p ON p.id=e.person_id "
        total = int(db.fetch_one("SELECT COUNT(*) n" + base + where, params)["n"])
        query_params = list(params) + [page_size, (page - 1) * page_size]
        items = db.fetch_all(
            "SELECT e.*,p.name AS person_name,(SELECT COUNT(*) FROM event_evidence ev WHERE ev.event_id=e.id) evidence_count,"
            "COALESCE((SELECT GROUP_CONCAT(DISTINCT s.name) FROM event_evidence ev "
            "JOIN raw_documents d ON d.id=ev.document_id JOIN information_sources s ON s.id=d.source_id "
            "WHERE ev.event_id=e.id),'') source_names" + base + where +
            " ORDER BY COALESCE(e.start_at,e.created_at) " + sort_order.upper() + ",e.id " + sort_order.upper() + " LIMIT ? OFFSET ?", query_params,
        )
        return _list_response(items, total, page, page_size)

    @application.get("/api/v1/events/locations")
    def event_locations(user: Dict[str, Any] = Depends(require_page("timeline"))):
        return {"items": [row["location_name"] for row in db.fetch_all(
            "SELECT DISTINCT location_name FROM timeline_events "
            "WHERE location_name!='' AND review_status!='rejected' ORDER BY location_name"
        )]}

    @application.get("/api/v1/events/{event_id}")
    def get_event(event_id: int, user: Dict[str, Any] = Depends(require_page("timeline"))):
        item = event_detail(db, event_id)
        if not item:
            raise HTTPException(404, "事件不存在")
        return item

    @application.post("/api/v1/events/{event_id}/review")
    def review_event(event_id: int, body: ReviewBody, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        before = event_detail(db, event_id)
        if not before:
            raise HTTPException(404, "事件不存在")
        action_status = {"approve": "approved", "reject": "rejected", "needs_review": "needs_review"}
        if body.action not in action_status:
            raise HTTPException(422, "不支持的审核动作")
        updates = {"review_status": action_status[body.action], "human_locked": 1, "updated_at": utc_now()}
        for field in ("title", "summary", "confirmation_status", "start_at", "location_name"):
            value = getattr(body, field)
            if value is not None:
                updates[field] = value
        allowed_confirm = {"rumored", "expected", "confirmed", "ongoing", "completed", "cancelled", "disputed"}
        if "confirmation_status" in updates and updates["confirmation_status"] not in allowed_confirm:
            raise HTTPException(422, "确认状态无效")
        assignments = ",".join(key + "=?" for key in updates)
        values = list(updates.values()) + [event_id]
        with db.transaction() as connection:
            connection.execute("UPDATE timeline_events SET " + assignments + " WHERE id=?", values)
            connection.execute(
                "INSERT INTO event_history(event_id,action,before_json,after_json,operator_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                (event_id, body.action, json_text({k: before.get(k) for k in updates}), json_text(updates), user["id"], body.reason, utc_now()),
            )
        audit(db, "review:" + body.action, "event", event_id, user["id"], ip_address=_client_ip(request), summary=body.reason)
        return event_detail(db, event_id)

    @application.get("/api/v1/search")
    def search(q: str = Query(min_length=1, max_length=200), page_size: int = Query(30, ge=1, le=100), user: Dict[str, Any] = Depends(require_page("search"))):
        term = "%" + q + "%"
        events = db.fetch_all(
            "SELECT e.id,'event' result_type,e.title,e.summary,e.start_at,p.name AS person_name FROM timeline_events e "
            "JOIN public_figures p ON p.id=e.person_id WHERE e.title LIKE ? OR e.summary LIKE ? OR e.quote_text LIKE ? OR e.location_name LIKE ? OR p.name LIKE ? LIMIT ?",
            (term, term, term, term, term, page_size),
        )
        remaining = max(0, page_size - len(events))
        docs = db.fetch_all(
            "SELECT d.id,'document' result_type,d.title,substr(d.content_text,1,300) summary,d.published_at start_at,s.name person_name "
            "FROM raw_documents d JOIN information_sources s ON s.id=d.source_id WHERE d.title LIKE ? OR d.content_text LIKE ? LIMIT ?",
            (term, term, remaining),
        ) if remaining else []
        return {"items": events + docs, "total": len(events) + len(docs)}

    @application.get("/api/v1/users")
    def list_users(user: Dict[str, Any] = Depends(require_admin)):
        items = db.fetch_all("SELECT id,username,role,enabled,last_login_at,created_at,updated_at FROM users ORDER BY id")
        for item in items:
            item["pages"] = user_pages(db, item)
        return {"items": items, "total": len(items), "all_pages": ALL_PAGES}

    @application.put("/api/v1/users/{user_id}/permissions")
    def set_permissions(user_id: int, body: PermissionBody, request: Request, user: Dict[str, Any] = Depends(require_admin)):
        target = db.fetch_one("SELECT * FROM users WHERE id=?", (user_id,))
        if not target:
            raise HTTPException(404, "用户不存在")
        pages = sorted(set(body.pages))
        if any(page not in ALL_PAGES for page in pages):
            raise HTTPException(422, "包含未知页面权限")
        if target["role"] == "admin":
            return {"id": user_id, "pages": ALL_PAGES}
        with db.transaction() as connection:
            connection.execute("DELETE FROM page_permissions WHERE user_id=?", (user_id,))
            connection.executemany("INSERT INTO page_permissions(user_id,page_key,can_access) VALUES(?,?,1)", [(user_id, page) for page in pages])
        audit(db, "permissions", "user", user_id, user["id"], ip_address=_client_ip(request), summary=",".join(pages))
        return {"id": user_id, "pages": pages}

    @application.get("/api/v1/config/effective")
    def effective_config(user: Dict[str, Any] = Depends(require_page("config"))):
        return {"config": settings.masked(), "config_path": str(settings.config_path), "sources": ["defaults", "app.json", "PFTS_* environment"]}

    @application.get("/api/v1/notifications/email/config")
    def get_email_config(user: Dict[str, Any] = Depends(require_page("notifications"))):
        return masked_email_config(settings, db)

    @application.put("/api/v1/notifications/email/config")
    def update_email_config(
        body: EmailConfigBody,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        values = body.model_dump(exclude_unset=True) if hasattr(body, "model_dump") else body.dict(exclude_unset=True)
        password = str(values.pop("password", "") or "")
        clear_password = bool(values.pop("clear_password", False))
        clear_fields = values.pop("clear_fields", []) or []
        try:
            config, sources = save_email_overrides(
                db, settings, values, clear_fields, password, clear_password, int(user["id"]),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        audit(
            db, "update", "notification_email_config", 1, user["id"],
            ip_address=_client_ip(request),
            summary="更新字段：{}；清除字段：{}；密码操作：{}".format(
                ",".join(sorted(values)), ",".join(sorted(clear_fields)),
                "更新" if password else ("清除" if clear_password else "保持"),
            ),
        )
        return masked_email_config(settings, db)

    @application.post("/api/v1/notifications/email/test")
    def test_email_config(request: Request, user: Dict[str, Any] = Depends(require_admin)):
        try:
            send_test_email(settings, db)
        except Exception as exc:
            safe = sanitize_error(exc)
            audit(
                db, "test", "notification_email", 1, user["id"], result="failed",
                ip_address=_client_ip(request), summary=safe,
            )
            raise HTTPException(502, safe)
        audit(
            db, "test", "notification_email", 1, user["id"],
            ip_address=_client_ip(request), summary="测试邮件已提交给 SMTP 服务",
        )
        return {"ok": True, "message": "测试邮件已发送"}

    @application.get("/api/v1/notifications/options")
    def notification_options(user: Dict[str, Any] = Depends(require_page("notifications"))):
        return {
            "tasks": db.fetch_all(
                "SELECT t.id,t.name,t.enabled,s.name AS source_name FROM collection_tasks t "
                "JOIN information_sources s ON s.id=t.source_id ORDER BY t.id"
            ),
            "persons": db.fetch_all(
                "SELECT id,name,organization,title FROM public_figures "
                "WHERE enabled=1 AND deleted_at IS NULL ORDER BY name,id"
            ),
            "event_types": sorted(EVENT_TYPES),
        }

    @application.get("/api/v1/notifications/rules")
    def notification_rules(user: Dict[str, Any] = Depends(require_page("notifications"))):
        items = list_rules(db)
        return {"items": items, "total": len(items)}

    @application.post("/api/v1/notifications/rules", status_code=201)
    def create_notification_rule(
        body: NotificationRuleBody,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        try:
            item = save_rule(
                db, body.name, body.task_ids, body.event_types, body.enabled,
                person_ids=body.person_ids,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        audit(
            db, "create", "notification_rule", item["id"], user["id"],
            ip_address=_client_ip(request), summary=item["name"],
        )
        return item

    @application.put("/api/v1/notifications/rules/{rule_id}")
    def update_notification_rule(
        rule_id: int,
        body: NotificationRuleBody,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        try:
            item = save_rule(
                db, body.name, body.task_ids, body.event_types, body.enabled,
                rule_id=rule_id, person_ids=body.person_ids,
            )
        except ValueError as exc:
            status_code = 404 if "不存在" in str(exc) else 422
            raise HTTPException(status_code, str(exc))
        audit(
            db, "update", "notification_rule", item["id"], user["id"],
            ip_address=_client_ip(request), summary=item["name"],
        )
        return item

    @application.delete("/api/v1/notifications/rules/{rule_id}")
    def remove_notification_rule(
        rule_id: int,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        if not delete_rule(db, rule_id):
            raise HTTPException(404, "推送规则不存在")
        audit(
            db, "delete", "notification_rule", rule_id, user["id"],
            ip_address=_client_ip(request), summary="删除推送规则",
        )
        return {"ok": True, "id": rule_id}

    @application.get("/api/v1/notifications/digests/config")
    def daily_digest_config(
        user: Dict[str, Any] = Depends(require_page("notifications")),
    ):
        config, sources = effective_digest_config(settings)
        return {"config": config, "sources": sources}

    @application.get("/api/v1/notifications/digests/options")
    def daily_digest_options(
        user: Dict[str, Any] = Depends(require_page("notifications")),
    ):
        config, sources = effective_digest_config(settings)
        return {
            "persons": db.fetch_all(
                "SELECT id,name,organization,title FROM public_figures "
                "WHERE enabled=1 AND deleted_at IS NULL ORDER BY name,id"
            ),
            "event_types": sorted(EVENT_TYPES),
            "defaults": config,
            "sources": sources,
        }

    @application.get("/api/v1/notifications/digests/rules")
    def daily_digest_rules(
        user: Dict[str, Any] = Depends(require_page("notifications")),
    ):
        reveal = user.get("role") == "admin"
        items = [
            mask_digest_rule(item, reveal)
            for item in list_digest_rules(db)
        ]
        return {"items": items, "total": len(items)}

    @application.post("/api/v1/notifications/digests/rules", status_code=201)
    def create_daily_digest_rule(
        body: DailyDigestRuleBody,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        try:
            item = save_digest_rule(
                db, settings, body.name, body.person_ids, body.event_types,
                body.recipients, enabled=body.enabled, send_time=body.send_time,
                window_mode=body.window_mode, rolling_hours=body.rolling_hours,
                send_when_empty=body.send_when_empty,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        audit(
            db, "create", "daily_digest_rule", item["id"], user["id"],
            ip_address=_client_ip(request),
            summary="{}；人物 {}；收件人 {} 个；发送时间 {}".format(
                item["name"], len(item["person_ids"]), len(item["recipients"]),
                item["send_time"],
            ),
        )
        return item

    @application.put("/api/v1/notifications/digests/rules/{rule_id}")
    def update_daily_digest_rule(
        rule_id: int,
        body: DailyDigestRuleBody,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        try:
            item = save_digest_rule(
                db, settings, body.name, body.person_ids, body.event_types,
                body.recipients, enabled=body.enabled, send_time=body.send_time,
                window_mode=body.window_mode, rolling_hours=body.rolling_hours,
                send_when_empty=body.send_when_empty, rule_id=rule_id,
            )
        except ValueError as exc:
            raise HTTPException(404 if "不存在" in str(exc) else 422, str(exc))
        audit(
            db, "update", "daily_digest_rule", item["id"], user["id"],
            ip_address=_client_ip(request),
            summary="{}；人物 {}；收件人 {} 个；发送时间 {}".format(
                item["name"], len(item["person_ids"]), len(item["recipients"]),
                item["send_time"],
            ),
        )
        return item

    @application.delete("/api/v1/notifications/digests/rules/{rule_id}")
    def remove_daily_digest_rule(
        rule_id: int,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        if not delete_digest_rule(db, rule_id):
            raise HTTPException(404, "日报规则不存在")
        audit(
            db, "delete", "daily_digest_rule", rule_id, user["id"],
            ip_address=_client_ip(request), summary="停用并软删除日报规则",
        )
        return {"ok": True, "id": rule_id}

    def _digest_date(value: Optional[str]) -> Optional[date]:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise HTTPException(422, "业务日期必须是 YYYY-MM-DD")

    @application.post("/api/v1/notifications/digests/rules/{rule_id}/preview")
    def preview_daily_digest_rule(
        rule_id: int,
        body: DailyDigestDateBody,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        try:
            return preview_digest(
                db, settings, rule_id, _digest_date(body.scheduled_date)
            )
        except ValueError as exc:
            raise HTTPException(404 if "不存在" in str(exc) else 422, str(exc))

    @application.post("/api/v1/notifications/digests/rules/{rule_id}/runs")
    def run_daily_digest_rule(
        rule_id: int,
        body: DailyDigestDateBody,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        scheduled_date_value = _digest_date(body.scheduled_date)
        if scheduled_date_value is None:
            raise HTTPException(422, "补跑必须指定业务日期")
        try:
            item = create_digest_run(
                db, settings, rule_id, scheduled_date_value, trigger_type="manual"
            )
        except ValueError as exc:
            raise HTTPException(404 if "不存在" in str(exc) else 422, str(exc))
        audit(
            db, "run", "daily_digest_run", item["id"], user["id"],
            ip_address=_client_ip(request),
            summary="补跑规则 {} 的业务日期 {}".format(
                rule_id, scheduled_date_value.isoformat()
            ),
        )
        return item

    @application.get("/api/v1/notifications/digests/runs")
    def daily_digest_runs(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=200),
        rule_id: Optional[int] = None,
        run_status: str = "",
        user: Dict[str, Any] = Depends(require_page("notifications")),
    ):
        clauses: List[str] = []
        params: List[Any] = []
        if rule_id is not None:
            clauses.append("r.rule_id=?")
            params.append(rule_id)
        allowed_statuses = {
            "pending", "empty", "sending", "sent", "partial", "failed", "skipped",
        }
        if run_status:
            if run_status not in allowed_statuses:
                raise HTTPException(422, "日报运行状态无效")
            clauses.append("r.status=?")
            params.append(run_status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        base = (
            " FROM daily_digest_runs r "
            "JOIN daily_digest_rules dr ON dr.id=r.rule_id "
        )
        total = int(db.fetch_one(
            "SELECT COUNT(*) n" + base + where, params
        )["n"])
        items = db.fetch_all(
            "SELECT r.*,dr.name AS rule_name" + base + where
            + " ORDER BY r.scheduled_date DESC,r.id DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        )
        return _list_response(items, total, page, page_size)

    @application.get("/api/v1/notifications/digests/runs/{run_id}")
    def daily_digest_run_detail(
        run_id: int,
        user: Dict[str, Any] = Depends(require_page("notifications")),
    ):
        item = db.fetch_one(
            "SELECT r.*,dr.name AS rule_name FROM daily_digest_runs r "
            "JOIN daily_digest_rules dr ON dr.id=r.rule_id WHERE r.id=?",
            (run_id,),
        )
        if not item:
            raise HTTPException(404, "日报运行不存在")
        reveal = user.get("role") == "admin"
        item["batches"] = db.fetch_all(
            "SELECT b.*,(SELECT COUNT(*) FROM daily_digest_items i "
            "WHERE i.batch_id=b.id) AS item_count "
            "FROM daily_digest_batches b WHERE b.run_id=? ORDER BY b.id",
            (run_id,),
        )
        if not reveal:
            for batch in item["batches"]:
                recipient = str(batch["recipient"])
                batch["recipient"] = "{}***@{}".format(
                    recipient[:1], recipient.split("@", 1)[1]
                )
        item["items"] = db.fetch_all(
            "SELECT i.*,e.title,e.event_type,e.start_at,p.name AS person_name "
            "FROM daily_digest_items i "
            "LEFT JOIN timeline_events e ON e.id=i.event_id "
            "LEFT JOIN public_figures p ON p.id=e.person_id "
            "WHERE i.run_id=? ORDER BY e.start_at IS NULL,e.start_at,p.id,e.event_type,e.id",
            (run_id,),
        )
        return item

    @application.post("/api/v1/notifications/digests/batches/{batch_id}/retry")
    def retry_daily_digest_batch(
        batch_id: int,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        batch = db.fetch_one(
            "SELECT id,run_id,status FROM daily_digest_batches WHERE id=?",
            (batch_id,),
        )
        if not batch:
            raise HTTPException(404, "日报投递批次不存在")
        if batch["status"] != "failed":
            raise HTTPException(409, "只有失败批次可以手工重试")
        now = utc_now()
        db.execute(
            "UPDATE daily_digest_batches SET status='retrying',attempt_count=0,"
            "next_attempt_at=?,last_error='',updated_at=? WHERE id=?",
            (now, now, batch_id),
        )
        db.execute(
            "UPDATE daily_digest_runs SET status='pending',error_summary='',updated_at=? "
            "WHERE id=?",
            (now, batch["run_id"]),
        )
        audit(
            db, "retry", "daily_digest_batch", batch_id, user["id"],
            ip_address=_client_ip(request), summary="手工重试失败日报批次",
        )
        return db.fetch_one(
            "SELECT * FROM daily_digest_batches WHERE id=?", (batch_id,)
        )

    @application.get("/api/v1/notifications/deliveries")
    def notification_deliveries(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=200),
        task_id: Optional[int] = None,
        delivery_status: str = "",
        user: Dict[str, Any] = Depends(require_page("notifications")),
    ):
        clauses = []
        params: List[Any] = []
        if task_id is not None:
            clauses.append("r.task_id=?")
            params.append(task_id)
        if delivery_status:
            if delivery_status not in {"pending", "sending", "retrying", "sent", "failed", "skipped"}:
                raise HTTPException(422, "投递状态无效")
            clauses.append("b.status=?")
            params.append(delivery_status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        base = (
            " FROM email_delivery_batches b JOIN task_runs r ON r.id=b.task_run_id "
            "JOIN collection_tasks t ON t.id=r.task_id "
        )
        total = int(db.fetch_one("SELECT COUNT(*) n" + base + where, params)["n"])
        items = db.fetch_all(
            "SELECT b.*,r.task_id,t.name AS task_name,"
            "(SELECT COUNT(*) FROM email_delivery_items i WHERE i.batch_id=b.id) AS item_count"
            + base + where + " ORDER BY b.id DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        )
        return _list_response(items, total, page, page_size)

    @application.get("/api/v1/notifications/deliveries/{batch_id}")
    def notification_delivery_detail(
        batch_id: int,
        user: Dict[str, Any] = Depends(require_page("notifications")),
    ):
        batch = db.fetch_one(
            "SELECT b.*,r.task_id,t.name AS task_name FROM email_delivery_batches b "
            "JOIN task_runs r ON r.id=b.task_run_id JOIN collection_tasks t ON t.id=r.task_id WHERE b.id=?",
            (batch_id,),
        )
        if not batch:
            raise HTTPException(404, "投递批次不存在")
        batch["items"] = db.fetch_all(
            "SELECT i.*,e.title,e.event_type,p.name AS person_name FROM email_delivery_items i "
            "LEFT JOIN timeline_events e ON e.id=i.event_id LEFT JOIN public_figures p ON p.id=e.person_id "
            "WHERE i.batch_id=? ORDER BY i.id",
            (batch_id,),
        )
        return batch

    @application.post("/api/v1/notifications/deliveries/{batch_id}/retry")
    def retry_notification_delivery(
        batch_id: int,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        batch = db.fetch_one("SELECT id,status FROM email_delivery_batches WHERE id=?", (batch_id,))
        if not batch:
            raise HTTPException(404, "投递批次不存在")
        if batch["status"] != "failed":
            raise HTTPException(409, "只有失败批次可以手工重试")
        now = utc_now()
        db.execute(
            "UPDATE email_delivery_batches SET status='retrying',attempt_count=0,next_attempt_at=?,"
            "last_error='',updated_at=? WHERE id=?",
            (now, now, batch_id),
        )
        audit(
            db, "retry", "email_delivery_batch", batch_id, user["id"],
            ip_address=_client_ip(request), summary="手工重试失败邮件批次",
        )
        return db.fetch_one("SELECT * FROM email_delivery_batches WHERE id=?", (batch_id,))

    @application.get("/api/v1/map/config")
    def map_config(user: Dict[str, Any] = Depends(require_page("map"))):
        config = settings.get("map") or {}
        return {
            "provider": str(config.get("provider") or "none"),
            "tile_url": str(config.get("tile_url") or ""),
            "attribution": str(config.get("attribution") or ""),
            "default_center": config.get("default_center") or [35.0, 105.0],
            "default_zoom": int(config.get("default_zoom") or 3),
        }

    @application.get("/api/v1/map/people")
    def map_people(user: Dict[str, Any] = Depends(require_page("map"))):
        return {"items": db.fetch_all(
            "SELECT DISTINCT p.id,p.name FROM public_figures p JOIN timeline_events e ON e.person_id=p.id "
            "WHERE p.deleted_at IS NULL AND e.event_type='itinerary' AND e.review_status!='rejected' ORDER BY p.name"
        )}

    @application.get("/api/v1/audit-logs")
    def audit_logs(page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=200), user: Dict[str, Any] = Depends(require_page("audit"))):
        total = int(db.fetch_one("SELECT COUNT(*) n FROM audit_logs")["n"])
        items = db.fetch_all(
            "SELECT a.*,u.username FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        )
        return _list_response(items, total, page, page_size)

    @application.post("/api/v1/maintenance/recheck-event-attribution")
    def maintenance_recheck_event_attribution(
        body: AttributionMaintenanceBody,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        if body.person_id is not None and not db.fetch_one("SELECT id FROM public_figures WHERE id=?", (body.person_id,)):
            raise HTTPException(404, "人物不存在")
        if body.source_id is not None and not db.fetch_one("SELECT id FROM information_sources WHERE id=?", (body.source_id,)):
            raise HTTPException(404, "来源不存在")
        try:
            result = recheck_event_attribution(
                db, body.dry_run, body.person_id, body.source_id, body.limit,
            )
        except Exception as exc:
            if not body.dry_run:
                audit(
                    db, "recheck_event_attribution", "maintenance", "", user["id"], result="failed",
                    ip_address=_client_ip(request), summary=str(exc)[:500],
                )
            raise
        if not body.dry_run:
            audit(
                db, "recheck_event_attribution", "maintenance", "", user["id"],
                ip_address=_client_ip(request),
                summary="删除无效证据 {}、孤立事件 {}，跳过锁定 {}".format(
                    result["invalid_evidence"], result["orphan_events"], result["locked_skipped"],
                ),
            )
        return result

    @application.post("/api/v1/maintenance/cleanup-chinadaily-content")
    def maintenance_cleanup_chinadaily_content(
        body: ChinaDailyMaintenanceBody,
        request: Request,
        user: Dict[str, Any] = Depends(require_admin),
    ):
        if body.source_id is not None and not db.fetch_one("SELECT id FROM information_sources WHERE id=?", (body.source_id,)):
            raise HTTPException(404, "来源不存在")
        try:
            result = cleanup_chinadaily_documents(
                db, settings.get("ai"), body.dry_run, body.source_id, body.limit,
            )
        except Exception as exc:
            if not body.dry_run:
                audit(
                    db, "cleanup_chinadaily_content", "maintenance", "", user["id"], result="failed",
                    ip_address=_client_ip(request), summary=str(exc)[:500],
                )
            raise
        if not body.dry_run:
            audit(
                db, "cleanup_chinadaily_content", "maintenance", "", user["id"],
                ip_address=_client_ip(request),
                summary="拒绝材料 {}、重清洗 {}、孤立事件 {}、跳过锁定 {}".format(
                    result["rejected_documents"], result["cleanable_documents"],
                    result["orphan_events"], result["locked_skipped"],
                ),
            )
        return result

    @application.post("/api/v1/maintenance/cleanup-navigation-pages")
    def cleanup_navigation_pages(
        body: CleanupNavigationBody, request: Request, user: Dict[str, Any] = Depends(require_admin),
    ):
        documents = db.fetch_all(
            "SELECT d.id,d.canonical_url,d.title,d.published_at,d.content_text,s.name AS source_name "
            "FROM raw_documents d JOIN information_sources s ON s.id=d.source_id "
            "WHERE d.canonical_url LIKE 'http%' AND s.parser_config LIKE '%\"discovery_enabled\":true%'"
        )
        rejected = []
        for document in documents:
            reason = _article_rejection_reason(document)
            if reason:
                rejected.append({
                    "id": document["id"], "title": document["title"],
                    "canonical_url": document["canonical_url"], "source_name": document["source_name"],
                    "reason": reason,
                })
        document_ids = [item["id"] for item in rejected]
        evidence_count = 0
        orphan_event_ids: List[int] = []
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            evidence_count = int(db.fetch_one(
                "SELECT COUNT(*) n FROM event_evidence WHERE document_id IN ({})".format(placeholders), document_ids
            )["n"])
            orphan_event_ids = [row["event_id"] for row in db.fetch_all(
                "SELECT DISTINCT ev.event_id FROM event_evidence ev WHERE ev.document_id IN ({0}) "
                "AND NOT EXISTS (SELECT 1 FROM event_evidence keep WHERE keep.event_id=ev.event_id "
                "AND keep.document_id NOT IN ({0}))".format(placeholders), document_ids + document_ids,
            )]
        result = {
            "dry_run": body.dry_run, "documents": len(document_ids), "evidence": evidence_count,
            "events": len(orphan_event_ids), "sample": rejected[:20],
        }
        if body.dry_run or not document_ids:
            return result
        placeholders = ",".join("?" for _ in document_ids)
        with db.transaction() as connection:
            connection.execute("DELETE FROM model_runs WHERE document_id IN ({})".format(placeholders), document_ids)
            connection.execute("DELETE FROM attachments WHERE document_id IN ({})".format(placeholders), document_ids)
            connection.execute("DELETE FROM event_evidence WHERE document_id IN ({})".format(placeholders), document_ids)
            if orphan_event_ids:
                event_placeholders = ",".join("?" for _ in orphan_event_ids)
                connection.execute("DELETE FROM event_history WHERE event_id IN ({})".format(event_placeholders), orphan_event_ids)
                connection.execute("DELETE FROM timeline_events WHERE id IN ({})".format(event_placeholders), orphan_event_ids)
            connection.execute("DELETE FROM raw_documents WHERE id IN ({})".format(placeholders), document_ids)
        audit(
            db, "cleanup_navigation_pages", "maintenance", "", user["id"], ip_address=_client_ip(request),
            summary="删除聚合页材料 {}、证据 {}、孤立事件 {}".format(len(document_ids), evidence_count, len(orphan_event_ids)),
        )
        result["deleted"] = True
        return result

    frontend_dist = settings.src_root / "app" / "frontend" / "dist"
    if frontend_dist.exists():
        application.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
    else:
        @application.get("/")
        def root():
            return {"name": settings.get("app", "name"), "api": "/docs", "message": "前端尚未构建，请执行 npm run build"}

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn
    current_settings = load_config()
    uvicorn.run(
        "app.backend.main:app", host=str(current_settings.get("server", "host")),
        port=int(current_settings.get("server", "port")), reload=False,
    )
