"""Machine credentials and MCP bridge into the existing, permission-checked API."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import parse_qs

import httpx
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.extension import Extension
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .security import current_user, require_page, user_pages, utc_now
from .services import analyze_document, audit, run_collection_task


SCOPES = {
    'persons:read': ('persons', False, '查询人物及别名'),
    'persons:write': ('persons', True, '创建和修改人物（仅管理员）'),
    'events:read': ('timeline', False, '查询事件、证据和材料'),
    'search:read': ('search', False, '全文搜索事件和材料'),
    'sources:read': ('sources', False, '查询信息源'),
    'tasks:read': ('tasks', False, '查询任务与运行日志'),
    'tasks:execute': ('tasks', True, '执行采集任务（仅管理员）'),
    'documents:analyze': ('timeline', True, '重新分析已有材料（仅管理员）'),
}
ROUTES = [
    ('GET', r'/persons(?:/\d+)?', 'persons:read'),
    ('POST', r'/persons', 'persons:write'),
    ('PUT', r'/persons/\d+', 'persons:write'),
    ('GET', r'/events(?:/\d+|/locations)?', 'events:read'),
    ('GET', r'/documents(?:/\d+)?', 'events:read'),
    ('GET', r'/search', 'search:read'),
    ('GET', r'/sources', 'sources:read'),
    ('GET', r'/tasks', 'tasks:read'),
    ('GET', r'/task-runs(?:/\d+/logs)?', 'tasks:read'),
    ('GET', r'/ai/jobs/[a-f0-9]+', None),
    ('POST', r'/ai/jobs', None),
]
MCP_TOKEN = ContextVar('pfts_mcp_token', default='')


class ClientCredentialsExtension(Extension):
    identifier = 'io.modelcontextprotocol/oauth-client-credentials'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def base_url(request):
    return (request.app.state.settings.get('server', 'base_url', '') or str(request.base_url)).rstrip('/')


def allowed_scopes(db, user):
    pages = user_pages(db, user)
    return [key for key, (page, admin, _) in SCOPES.items()
            if page in pages and (not admin or user['role'] == 'admin')]


def unauthorized(request):
    return HTTPException(401, '机器凭据无效或已失效', headers={
        'WWW-Authenticate': 'Bearer resource_metadata="' + base_url(request) + '/.well-known/oauth-protected-resource/mcp"'})


def identity(request, token, resource):
    db = request.app.state.db
    if not token:
        raise unauthorized(request)
    row = db.fetch_one(
        'SELECT t.scopes token_scopes,c.* FROM ai_tokens t JOIN ai_credentials c ON c.id=t.credential_id '
        'WHERE t.token_hash=? AND t.resource=? AND t.expires_at>? AND c.expires_at>? AND c.revoked_at IS NULL',
        (digest(token), resource, utc_now(), utc_now()))
    if not row:
        raise unauthorized(request)
    user = db.fetch_one('SELECT * FROM users WHERE id=? AND enabled=1', (row['user_id'],))
    if not user or 'ai_access' not in user_pages(db, user):
        raise unauthorized(request)
    user['pages'] = user_pages(db, user)
    user['credential_id'] = row['id']
    user['scopes'] = sorted(set(json.loads(row['scopes'])) & set(json.loads(row['token_scopes'])) & set(allowed_scopes(db, user)))
    return user


def bearer(request):
    parts = request.headers.get('authorization', '').split()
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        raise unauthorized(request)
    return parts[1]


def machine_user(request):
    # MCP tokens are deliberately not valid as general API tokens.
    resource = getattr(request.state, 'machine_resource', base_url(request) + '/api/v1')
    user = identity(request, bearer(request), resource)
    path = request.url.path.removeprefix('/api/v1')
    for method, pattern, scope in ROUTES:
        if request.method == method and re.fullmatch(pattern, path):
            if scope and scope not in user['scopes']:
                raise HTTPException(403, '凭据范围或所属用户当前权限不足')
            return user
    raise HTTPException(403, '此接口不向机器凭据开放')


class CredentialBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100, pattern=r'\S')
    expires_in_days: int = Field(default=30, ge=1, le=365)
    scopes: list[str] = Field(min_length=1, max_length=8)


class JobBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['collection', 'reanalyze']
    target_id: int = Field(gt=0)


def check_job_permission(user, kind):
    scope = 'tasks:execute' if kind == 'collection' else 'documents:analyze'
    page, _, _ = SCOPES[scope]
    if user['role'] != 'admin' or page not in user['pages'] or ('scopes' in user and scope not in user['scopes']):
        raise HTTPException(403, '没有执行此任务的权限')


class JobWorker:
    def __init__(self, app):
        self.app = app
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True, name='pfts-ai-jobs')

    def start(self):
        db = self.app.state.db
        db.execute("UPDATE ai_jobs SET status='failed',error='服务中断，结果可能已部分写入；请核查后使用新幂等键重试',finished_at=? WHERE status='running'", (utc_now(),))
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()  # Drain current business operation before the process exits.

    def loop(self):
        db, settings = self.app.state.db, self.app.state.settings
        while not self.stop_event.wait(0.25):
            with db.transaction(immediate=True) as c:
                row = c.execute("SELECT * FROM ai_jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
                if not row:
                    continue
                job = dict(row)
                c.execute("UPDATE ai_jobs SET status='running' WHERE id=?", (job['id'],))
            try:
                user = db.fetch_one('SELECT * FROM users WHERE id=? AND enabled=1', (job['user_id'],))
                credential = db.fetch_one('SELECT * FROM ai_credentials WHERE id=? AND revoked_at IS NULL AND expires_at>?', (job['credential_id'], utc_now()))
                if not user or not credential or 'ai_access' not in user_pages(db, user):
                    raise HTTPException(403, '任务所属用户或凭据已失效')
                user['pages'] = user_pages(db, user)
                user['scopes'] = sorted(set(json.loads(credential['scopes'])) & set(allowed_scopes(db, user)))
                check_job_permission(user, job['kind'])
                result = (run_collection_task(db, job['target_id'], settings) if job['kind'] == 'collection'
                          else {'event_count': analyze_document(db, job['target_id'], settings.get('ai'))})
                failed = result.get('status') == 'failed'
                if result.get('error_summary'):
                    result['error_summary'] = '采集业务失败；请核查来源及服务配置'
                db.execute("UPDATE ai_jobs SET status=?,result=?,error=?,finished_at=? WHERE id=?", ('failed' if failed else 'completed', json.dumps(result), '采集业务失败；请核查来源及服务配置' if failed else None, utc_now(), job['id']))
                outcome = 'failed' if failed else 'success'
            except Exception:
                # Never persist upstream exception text: it can contain credentials or sensitive bodies.
                db.execute("UPDATE ai_jobs SET status='failed',error='业务执行失败或当前权限不足；请核查目标和任务日志',finished_at=? WHERE id=?", (utc_now(), job['id']))
                outcome = 'failed'
            audit(db, 'ai_job_execute', 'ai_job', job['id'], job['user_id'], result=outcome, summary=job['credential_id'])


def install_ai(app):
    db = app.state.db
    manager = Depends(require_page('ai_access'))

    @app.get('/api/v1/ai/access')
    def access_info(request: Request, user=manager):
        url = base_url(request)
        return {'api': url + '/api/v1', 'openapi': url + '/openapi.json', 'docs': url + '/docs',
                'mcp': url + '/mcp', 'token_endpoint': url + '/oauth/token',
                'auth': 'OAuth client_credentials + client_secret_basic; short-lived audience-bound Bearer',
                'token_seconds': app.state.settings.get('ai_access', 'token_seconds', 600),
                'scopes': [{'scope': s, 'description': SCOPES[s][2]} for s in allowed_scopes(db, user)],
                'clients': '官方 Python MCP SDK 2.3；须支持 client_credentials 或使用随附自动续取客户端。仅支持交互授权码的客户端尚不支持。'}

    columns = 'id,name,scopes,created_at,expires_at,revoked_at,last_used_at'

    @app.get('/api/v1/ai/credentials')
    def list_credentials(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=manager):
        rows = db.fetch_all('SELECT ' + columns + ' FROM ai_credentials WHERE user_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
                            (user['id'], page_size, (page - 1) * page_size))
        for row in rows:
            row['scopes'] = json.loads(row['scopes'])
            row['status'] = 'revoked' if row['revoked_at'] else ('expired' if row['expires_at'] <= utc_now() else 'active')
        return {'items': rows, 'total': db.fetch_one('SELECT COUNT(*) n FROM ai_credentials WHERE user_id=?', (user['id'],))['n'], 'page': page, 'page_size': page_size}

    @app.post('/api/v1/ai/credentials', status_code=201)
    def create_credential(body: CredentialBody, request: Request, user=manager):
        if not set(body.scopes) <= set(allowed_scopes(db, user)):
            raise HTTPException(403, '不能授予超出当前用户权限的范围')
        key_id, secret = 'ai_' + secrets.token_hex(10), secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)).replace(microsecond=0).isoformat()
        db.execute('INSERT INTO ai_credentials(id,user_id,name,secret_hash,scopes,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',
                   (key_id, user['id'], body.name.strip(), digest(secret), json.dumps(sorted(set(body.scopes))), utc_now(), expires))
        audit(db, 'create', 'ai_credential', key_id, user['id'])
        return {'id': key_id, 'client_secret': secret, 'expires_at': expires}

    @app.delete('/api/v1/ai/credentials/{credential_id}')
    def revoke_credential(credential_id: str, user=manager):
        with db.transaction(immediate=True) as c:
            row = c.execute('SELECT id FROM ai_credentials WHERE id=? AND user_id=?', (credential_id, user['id'])).fetchone()
            if not row:
                raise HTTPException(404, '凭据不存在')
            c.execute('UPDATE ai_credentials SET revoked_at=COALESCE(revoked_at,?) WHERE id=?', (utc_now(), credential_id))
            c.execute('DELETE FROM ai_tokens WHERE credential_id=?', (credential_id,))
        audit(db, 'revoke', 'ai_credential', credential_id, user['id'])
        return {'ok': True}

    @app.post('/oauth/token')
    async def token(request: Request):
        try:
            scheme, encoded = request.headers.get('authorization', '').split(' ', 1)
            key_id, secret = base64.b64decode(encoded, validate=True).decode().split(':', 1)
            if scheme.lower() != 'basic' or not secret:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise HTTPException(401, 'invalid_client', headers={'WWW-Authenticate': 'Basic'})
        row = db.fetch_one('SELECT * FROM ai_credentials WHERE id=? AND revoked_at IS NULL AND expires_at>?', (key_id, utc_now()))
        if not row or not hmac.compare_digest(row['secret_hash'], digest(secret)):
            raise HTTPException(401, 'invalid_client', headers={'WWW-Authenticate': 'Basic'})
        user = db.fetch_one('SELECT * FROM users WHERE id=? AND enabled=1', (row['user_id'],))
        if not user or 'ai_access' not in user_pages(db, user):
            raise HTTPException(401, 'invalid_client')
        form = parse_qs((await request.body()).decode(), keep_blank_values=True)
        if form.get('grant_type') != ['client_credentials']:
            raise HTTPException(400, 'unsupported_grant_type')
        resource = form.get('resource', [''])[0]
        if resource not in {base_url(request) + '/mcp', base_url(request) + '/api/v1'}:
            raise HTTPException(400, 'invalid_target: resource 必须与当前 API 或 MCP 地址一致')
        scopes = set(json.loads(row['scopes'])) & set(allowed_scopes(db, user))
        if 'scope' in form:
            requested = set(form['scope'][0].split())
            if not requested or not requested <= scopes:
                raise HTTPException(400, 'invalid_scope')
            scopes = requested
        if not scopes:
            raise HTTPException(403, '当前用户已无可用范围')
        seconds = min(max(30, int(app.state.settings.get('ai_access', 'token_seconds', 600))), 3600)
        expiry = min(datetime.fromisoformat(row['expires_at']), datetime.now(timezone.utc) + timedelta(seconds=seconds))
        value = secrets.token_urlsafe(32)
        with db.transaction(immediate=True) as c:
            c.execute('DELETE FROM ai_tokens WHERE expires_at<=?', (utc_now(),))
            c.execute('INSERT INTO ai_tokens VALUES(?,?,?,?,?)', (digest(value), key_id, resource, json.dumps(sorted(scopes)), expiry.isoformat()))
            c.execute('UPDATE ai_credentials SET last_used_at=? WHERE id=?', (utc_now(), key_id))
        audit(db, 'issue_token', 'ai_credential', key_id, user['id'])
        return {'access_token': value, 'token_type': 'Bearer', 'expires_in': max(0, int((expiry - datetime.now(timezone.utc)).total_seconds())), 'scope': ' '.join(sorted(scopes))}

    @app.get('/.well-known/oauth-protected-resource/mcp')
    def protected_metadata(request: Request):
        url = base_url(request)
        return {'resource': url + '/mcp', 'authorization_servers': [url], 'scopes_supported': [], 'bearer_methods_supported': ['header']}

    @app.get('/.well-known/oauth-authorization-server')
    def authorization_metadata(request: Request):
        url = base_url(request)
        return {'issuer': url, 'authorization_endpoint': url + '/oauth/authorize', 'token_endpoint': url + '/oauth/token', 'grant_types_supported': ['client_credentials'],
                'response_types_supported': [], 'token_endpoint_auth_methods_supported': ['client_secret_basic'], 'scopes_supported': list(SCOPES)}

    @app.get('/oauth/authorize')
    def unsupported_interactive_authorization():
        raise HTTPException(400, '仅支持预注册机器客户端 client_credentials；未开放交互授权码流程')

    @app.post('/api/v1/ai/jobs', status_code=202)
    def submit_job(body: JobBody, user=Depends(current_user)):
        check_job_permission(user, body.kind)
        if 'credential_id' not in user:
            raise HTTPException(403, '异步机器任务须使用独立凭据')
        table = 'collection_tasks' if body.kind == 'collection' else 'raw_documents'
        if not db.fetch_one('SELECT id FROM ' + table + ' WHERE id=?', (body.target_id,)):
            raise HTTPException(404, '任务目标不存在')
        job_id = secrets.token_hex(16)
        db.execute("INSERT INTO ai_jobs(id,user_id,credential_id,kind,target_id,status,created_at) VALUES(?,?,?,?,?,'queued',?)",
                   (job_id, user['id'], user['credential_id'], body.kind, body.target_id, utc_now()))
        return {'id': job_id, 'status': 'queued', 'status_url': '/api/v1/ai/jobs/' + job_id}

    @app.get('/api/v1/ai/jobs/{job_id}')
    def get_job(job_id: str, user=Depends(current_user)):
        row = db.fetch_one('SELECT * FROM ai_jobs WHERE id=? AND user_id=?', (job_id, user['id']))
        if not row:
            raise HTTPException(404, '任务不存在')
        check_job_permission(user, row['kind'])
        row['result'] = json.loads(row['result']) if row['result'] else None
        return row

    server = MCPServer('人物足迹', extensions=[ClientCredentialsExtension()], instructions='查询公开人物与可追溯证据。写操作需要显式范围和稳定幂等键；不提供删除、发布或批量维护。')

    async def call(method, path, params=None, body=None, key=None, confirm_replace=False):
        headers = {'Authorization': 'Bearer ' + MCP_TOKEN.get(), 'X-PFTS-MCP-Bridge': '1'}
        if key:
            headers['Idempotency-Key'] = key
        if confirm_replace:
            headers['X-Confirm-Replace'] = 'true'
        # ASGI loopback reuses the exact API validation and business handlers.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://mcp.internal') as client:
            response = await client.request(method, path, params=params, json=body, headers=headers)
        if response.status_code >= 400:
            raise ToolError(response.json().get('error', {}).get('message', '业务调用失败'))
        return response.json()

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=read)
    async def list_persons(q: str = '', page: int = 1, page_size: int = 20) -> dict:
        """分页查询公开人物；q 匹配姓名、别名或组织。需要 persons:read。"""
        return await call('GET', '/api/v1/persons', params={'q': q, 'page': page, 'page_size': page_size})

    @server.tool(annotations=read)
    async def get_person(person_id: int) -> dict:
        """查询人物详情与别名。需要 persons:read。"""
        return await call('GET', f'/api/v1/persons/{person_id}')

    @server.tool(annotations=read)
    async def list_events(q: str = '', person_id: int | None = None, page: int = 1, page_size: int = 20) -> dict:
        """分页查询事件，可按人物或关键词筛选。需要 events:read。"""
        params = {'q': q, 'page': page, 'page_size': page_size}
        if person_id is not None:
            params['person_id'] = person_id
        return await call('GET', '/api/v1/events', params=params)

    @server.tool(annotations=read)
    async def get_event(event_id: int) -> dict:
        """查询事件详情、原文链接和证据。需要 events:read。"""
        return await call('GET', f'/api/v1/events/{event_id}')

    @server.tool(annotations=read)
    async def search_public_information(q: str, page: int = 1, page_size: int = 30) -> dict:
        """全文搜索公开材料与事件。需要 search:read；q 不得为空。"""
        return await call('GET', '/api/v1/search', params={'q': q, 'page': page, 'page_size': page_size})

    @server.tool(annotations=read)
    async def list_sources(page: int = 1, page_size: int = 20) -> dict:
        """分页查询信息源。需要 sources:read。"""
        return await call('GET', '/api/v1/sources', params={'page': page, 'page_size': page_size})

    @server.tool(annotations=read)
    async def list_tasks(page: int = 1, page_size: int = 20) -> dict:
        """分页查询采集任务及最近状态。需要 tasks:read。"""
        return await call('GET', '/api/v1/tasks', params={'page': page, 'page_size': page_size})

    @server.tool(annotations=write)
    async def create_person(name: str, idempotency_key: str, organization: str = '', aliases: list[str] | None = None) -> dict:
        """创建人物；需要 persons:write 和当前管理员身份。相同幂等键只执行一次。"""
        return await call('POST', '/api/v1/persons', body={'name': name, 'organization': organization, 'aliases': aliases or []}, key=idempotency_key)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
    async def update_person(person_id: int, name: str, idempotency_key: str, confirm_replace: bool, organization: str = '', aliases: list[str] | None = None) -> dict:
        """替换人物资料；未提供的可选资料将重置为默认值。需要 persons:write 和管理员身份。"""
        if not confirm_replace:
            raise ToolError('须先确认完整替换人物资料；未提供字段将重置为默认值')
        return await call('PUT', f'/api/v1/persons/{person_id}', body={'name': name, 'organization': organization, 'aliases': aliases or []}, key=idempotency_key, confirm_replace=True)

    @server.tool(annotations=write)
    async def submit_task(kind: Literal['collection', 'reanalyze'], target_id: int, idempotency_key: str) -> dict:
        """异步采集或重分析，返回任务 ID；分别需要 tasks:execute / documents:analyze 和管理员身份。"""
        return await call('POST', '/api/v1/ai/jobs', body={'kind': kind, 'target_id': target_id}, key=idempotency_key)

    @server.tool(annotations=read)
    async def get_task_result(job_id: str) -> dict:
        """查询本人异步任务的状态、结果、失败原因；仍须具有执行该业务的当前权限。"""
        return await call('GET', '/api/v1/ai/jobs/' + job_id)

    # Outer guard authenticates every HTTP request, before protocol dispatch.
    # Origin is checked below; remote addresses use the existing deployment boundary.
    mcp_app = server.streamable_http_app(streamable_http_path='/', stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    app.mount('/mcp', mcp_app)
    app.state.ai_mcp_app = mcp_app
    app.state.ai_worker = JobWorker(app)
    app.state.ai_tools = [list_persons, get_person, list_events, get_event, search_public_information, list_sources, list_tasks, create_person, update_person, submit_task, get_task_result]

    @app.middleware('http')
    async def ai_guard(request: Request, call_next):
        path = request.url.path
        machine = 'authorization' in request.headers and path.startswith('/api/v1/')
        is_mcp = path.rstrip('/') == '/mcp'
        manage = path.startswith('/api/v1/ai/') and not path.startswith('/api/v1/ai/jobs')
        sensitive = manage or path == '/oauth/token'
        principal = None
        reserved = None
        marker = None
        try:
            # An internal bridge marker has authority only with a context-local token set by MCP.
            if request.headers.get('X-PFTS-MCP-Bridge'):
                if not MCP_TOKEN.get() or request.headers.get('authorization') != 'Bearer ' + MCP_TOKEN.get():
                    raise HTTPException(403, '无效的内部调用')
                request.state.machine_resource = app.state.ai_mcp_resource.get()
            if is_mcp:
                origin = request.headers.get('origin')
                if origin and origin.rstrip('/') not in {str(request.base_url).rstrip('/'), base_url(request)}:
                    raise HTTPException(403, 'MCP Origin 不允许')
                token_value = bearer(request)
                principal = identity(request, token_value, base_url(request) + '/mcp')
                if request.method == 'POST':
                    try:
                        message = json.loads(await request.body())
                    except (ValueError, UnicodeError):
                        message = {}
                    if isinstance(message, dict) and message.get('method') == 'tools/call':
                        params = message.get('params') if isinstance(message.get('params'), dict) else {}
                        tool_scope = {'list_persons': 'persons:read', 'get_person': 'persons:read',
                            'list_events': 'events:read', 'get_event': 'events:read',
                            'search_public_information': 'search:read', 'list_sources': 'sources:read',
                            'list_tasks': 'tasks:read', 'create_person': 'persons:write', 'update_person': 'persons:write'}
                        scope = tool_scope.get(params.get('name'))
                        if params.get('name') == 'submit_task':
                            arguments = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
                            scope = 'tasks:execute' if arguments.get('kind') == 'collection' else 'documents:analyze'
                        if scope and scope not in principal['scopes']:
                            raise HTTPException(403, '工具调用所需范围不足', headers={'WWW-Authenticate': 'Bearer error="insufficient_scope", scope="' + scope + '", resource_metadata="' + base_url(request) + '/.well-known/oauth-protected-resource/mcp"'})
                marker = MCP_TOKEN.set(token_value)
                app.state.ai_mcp_resource.set(base_url(request) + '/mcp')
            elif machine:
                principal = machine_user(request)
            if manage:
                if 'authorization' in request.headers:
                    raise HTTPException(403, '凭据管理仅限网页登录')
                if request.method not in {'GET', 'HEAD'}:
                    origin = request.headers.get('origin')
                    if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and origin.rstrip('/') not in {str(request.base_url).rstrip('/'), base_url(request)}):
                        raise HTTPException(403, '跨站请求被拒绝')
                    if request.headers.get('X-PFTS-Request') != '1':
                        raise HTTPException(403, '缺少同源请求标识')
            if principal or path == '/oauth/token' or manage:
                bucket = 'credential:' + principal['credential_id'] if principal else 'ip:' + (request.client.host if request.client else '')
                window = int(time.time()) // 60
                with db.transaction(immediate=True) as c:
                    c.execute('DELETE FROM ai_rate_limits WHERE window<?', (window - 1,))
                    c.execute('INSERT INTO ai_rate_limits VALUES(?,?,1) ON CONFLICT(bucket) DO UPDATE SET count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END,window=excluded.window', (bucket, window))
                    count = c.execute('SELECT count FROM ai_rate_limits WHERE bucket=?', (bucket,)).fetchone()[0]
                if count > int(app.state.settings.get('ai_access', 'requests_per_minute', 120)):
                    raise HTTPException(429, '调用频率超限', headers={'Retry-After': '60'})
                body_bytes = await request.body()
                if len(body_bytes) > int(app.state.settings.get('ai_access', 'max_body_bytes', 524288)):
                    raise HTTPException(413, '请求体过大')
            if machine and request.method not in {'GET', 'HEAD'}:
                if request.method == 'PUT' and request.headers.get('X-Confirm-Replace') != 'true':
                    raise HTTPException(422, '完整替换人物资料须 X-Confirm-Replace: true；未提供字段将重置')
                key = request.headers.get('Idempotency-Key', '')
                if not re.fullmatch(r'[A-Za-z0-9_.:-]{8,100}', key):
                    raise HTTPException(422, '写操作需要 8–100 位 Idempotency-Key')
                fingerprint = digest(request.method + path + str(request.url.query) + (await request.body()).decode())
                with db.transaction(immediate=True) as c:
                    existing = c.execute('SELECT * FROM ai_requests WHERE credential_id=? AND request_key=?', (principal['credential_id'], key)).fetchone()
                    if existing:
                        if existing['fingerprint'] != fingerprint:
                            raise HTTPException(409, '幂等键已用于其他请求')
                        if existing['status'] is None:
                            raise HTTPException(409, '请求执行中或服务中断后结果待核查；不要自动换键重试')
                        return JSONResponse(status_code=existing['status'], content=json.loads(existing['response']), headers={'Idempotency-Replayed': 'true', 'Cache-Control': 'no-store'})
                    c.execute('INSERT INTO ai_requests(credential_id,request_key,fingerprint,created_at) VALUES(?,?,?,?)', (principal['credential_id'], key, fingerprint, utc_now()))
                    reserved = (principal['credential_id'], key)
            response = await call_next(request)
            if path == '/oauth/token' and response.status_code >= 400:
                audit(db, 'issue_token_denied', 'http', path, result=str(response.status_code))
            if reserved:
                payload = b''.join([chunk async for chunk in response.body_iterator])
                db.execute('UPDATE ai_requests SET status=?,response=? WHERE credential_id=? AND request_key=?', (response.status_code, payload.decode(), *reserved))
                response = JSONResponse(status_code=response.status_code, content=json.loads(payload), headers=dict(response.headers))
            if (principal and request.method not in {'GET', 'HEAD'}) or (manage and request.method not in {'GET', 'HEAD'}):
                actor = principal or current_user(request, request.cookies.get('pfts_session'))
                audit(db, 'machine_request' if principal else 'credential_management', 'http', path, actor['id'], result=str(response.status_code), summary=principal['credential_id'] if principal else '')
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={'error': {'code': {401: 'UNAUTHORIZED', 403: 'FORBIDDEN', 409: 'CONFLICT', 429: 'RATE_LIMITED'}.get(exc.status_code, 'REQUEST_ERROR'), 'message': str(exc.detail)}}, headers=exc.headers)
            if request.method not in {'GET', 'HEAD'} and (machine or is_mcp or manage or path == '/oauth/token'):
                audit(db, 'ai_request_denied', 'http', path, principal['id'] if principal else None, result=str(exc.status_code))
        finally:
            if marker is not None:
                MCP_TOKEN.reset(marker)
        if sensitive or machine or is_mcp:
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Pragma'] = 'no-cache'
        return response

    app.state.ai_mcp_resource = ContextVar('pfts_mcp_resource', default='')
