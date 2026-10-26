"""Describe machine authentication alongside the compatible browser API."""
import copy
import re

from fastapi.openapi.utils import get_openapi

from .ai_access import ROUTES


def install_openapi(app):
    def schema():
        if app.openapi_schema:
            return app.openapi_schema
        doc = get_openapi(title=app.title, version=app.version, routes=app.routes,
            description='人物足迹 API v1。网页登录使用 HttpOnly 会话；机器客户端通过 OAuth client_credentials 换取资源绑定的短期 Bearer。凭据在 AI 接入页面创建。所有机器写操作要求 Idempotency-Key；同键不同内容返回 409。')
        components = doc.setdefault('components', {})
        security = components.setdefault('securitySchemes', {})
        security['BrowserSession'] = {'type': 'apiKey', 'in': 'cookie', 'name': 'pfts_session'}
        security['ClientCredentialsBasic'] = {'type': 'http', 'scheme': 'basic', 'description': '用户名为 client_id；密码为只在创建时返回的 client_secret'}
        error = {'type': 'object', 'required': ['error'], 'properties': {'error': {'type': 'object', 'required': ['code', 'message'], 'properties': {'code': {'type': 'string'}, 'message': {'type': 'string'}}}}}
        for path, methods in doc['paths'].items():
            for method, operation in methods.items():
                if not isinstance(operation, dict) or method not in {'get', 'post', 'put', 'delete'}:
                    continue
                concrete = re.sub(r'\{[^}]+\}', '1', path.removeprefix('/api/v1'))
                scopes = [scope for verb, pattern, scope in ROUTES if verb.lower() == method and re.fullmatch(pattern, concrete)]
                if scopes:
                    operation['security'] = [{'HTTPBearer': []}, {'BrowserSession': []}]
                    operation['description'] = (operation.get('description', '') + '\n机器接入范围：' + (scopes[0] or '任务对应的执行范围') + '。所属用户当前页面权限及管理员限制同时生效。').strip()
                    if method in {'post', 'put'}:
                        operation.setdefault('parameters', []).append({'name': 'Idempotency-Key', 'in': 'header', 'required': False,
                            'description': '机器写入必填，8–100 位；网页登录兼容旧行为。并发执行中返回 409，完成后重放原响应。', 'schema': {'type': 'string', 'minLength': 8, 'maxLength': 100}, 'example': 'create-person-20261026-001'})
                    if method == 'put':
                        operation.setdefault('parameters', []).append({'name': 'X-Confirm-Replace', 'in': 'header', 'required': False, 'description': '机器替换人物资料时必须为 true；未提供字段会重置。', 'schema': {'type': 'string', 'enum': ['true']}})
                elif path.startswith('/api/v1/ai/'):
                    operation['security'] = [{'BrowserSession': []}]
                    if method in {'post', 'delete'}:
                        operation.setdefault('parameters', []).append({'name': 'X-PFTS-Request', 'in': 'header', 'required': True, 'schema': {'type': 'string', 'enum': ['1']}, 'description': '页面自动附带的同源请求标识；服务端还验证 Origin。'})
                if path.startswith('/api/') or path == '/oauth/token':
                    for status, message in [('401', '无效、过期、撤销或停用'), ('403', '用户或凭据权限不足'), ('409', '幂等冲突或执行中'), ('422', '参数校验失败'), ('429', '限流，遵循 Retry-After')]:
                        operation.setdefault('responses', {})[status] = {'description': message, 'content': {'application/json': {'schema': error, 'example': {'error': {'code': 'FORBIDDEN' if status == '403' else 'REQUEST_ERROR', 'message': message}}}}}
        token = doc['paths']['/oauth/token']['post']
        token['security'] = [{'ClientCredentialsBasic': []}]
        token['requestBody'] = {'required': True, 'content': {'application/x-www-form-urlencoded': {'schema': {'type': 'object', 'required': ['grant_type', 'resource'], 'properties': {
            'grant_type': {'type': 'string', 'enum': ['client_credentials']}, 'resource': {'type': 'string', 'description': '当前环境完整 API 或 MCP 地址；与令牌使用目标必须一致'}, 'scope': {'type': 'string', 'description': '可选，空格分隔；不得超过凭据及用户当前权限'}}},
            'example': {'grant_type': 'client_credentials', 'resource': 'https://your-system.example/mcp', 'scope': 'persons:read'}}}}
        token['responses']['200'] = {'description': '短期令牌，只在内存中使用；响应禁止缓存', 'content': {'application/json': {'schema': {'type': 'object', 'required': ['access_token', 'token_type', 'expires_in', 'scope'], 'properties': {'access_token': {'type': 'string'}, 'token_type': {'type': 'string', 'enum': ['Bearer']}, 'expires_in': {'type': 'integer'}, 'scope': {'type': 'string'}}}, 'example': {'access_token': '<short-lived-token>', 'token_type': 'Bearer', 'expires_in': 599, 'scope': 'persons:read'}}}}
        schemas = components.setdefault('schemas', {})
        person = copy.deepcopy(schemas['PersonBody'])
        person['properties']['enabled'] = {'type': 'integer', 'enum': [0, 1]}
        person['properties'].update({'id': {'type': 'integer'}, 'created_at': {'type': 'string'}, 'updated_at': {'type': 'string'}, 'deleted_at': {'type': ['string', 'null']}, 'event_count': {'type': 'integer'}})
        schemas['PersonResponse'] = person
        example_person = {'id': 12, 'name': '公开人物示例', 'aliases': ['公开别名'], 'organization': '公开机构', 'enabled': 1, 'created_at': '2026-10-26T00:00:00+00:00'}
        for path, methods in doc['paths'].items():
            for method, operation in methods.items():
                if method not in {'get', 'post', 'put', 'delete'}:
                    continue
                if path == '/api/v1/persons' and method in {'post', 'put'}:
                    operation['requestBody']['content']['application/json']['example'] = {'name': '公开人物示例', 'aliases': ['公开别名'], 'organization': '公开机构'}
                if path.startswith('/api/v1/persons') and method in {'post', 'put'} or path == '/api/v1/persons/{person_id}' and method == 'get':
                    status = '201' if method == 'post' else '200'
                    operation['responses'][status]['content'] = {'application/json': {'schema': {'$ref': '#/components/schemas/PersonResponse'}, 'example': example_person}}
                if method == 'get' and path in {'/api/v1/persons', '/api/v1/sources', '/api/v1/tasks', '/api/v1/events', '/api/v1/documents', '/api/v1/task-runs', '/api/v1/ai/credentials'}:
                    item = {'$ref': '#/components/schemas/PersonResponse'} if path.endswith('/persons') else {'type': 'object', 'additionalProperties': True}
                    operation['responses']['200']['content'] = {'application/json': {'schema': {'type': 'object', 'required': ['items', 'total'], 'properties': {'items': {'type': 'array', 'items': item}, 'total': {'type': 'integer'}, 'page': {'type': ['integer', 'null']}, 'page_size': {'type': 'integer'}}}, 'example': {'items': [example_person] if path.endswith('/persons') else [], 'total': 1 if path.endswith('/persons') else 0, 'page': 1, 'page_size': 20}}}
        doc['paths']['/api/v1/ai/credentials']['post']['responses']['201']['content'] = {'application/json': {'schema': {'type': 'object', 'required': ['id', 'client_secret', 'expires_at'], 'properties': {'id': {'type': 'string'}, 'client_secret': {'type': 'string', 'description': '仅创建成功时返回一次，不可再次读取'}, 'expires_at': {'type': 'string'}}}, 'example': {'id': 'ai_<public-id>', 'client_secret': '<one-time-secret>', 'expires_at': '2026-11-25T00:00:00+00:00'}}}
        doc['paths']['/api/v1/ai/jobs']['post']['responses']['202']['content'] = {'application/json': {'schema': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'status': {'type': 'string', 'enum': ['queued']}, 'status_url': {'type': 'string'}}}, 'example': {'id': '<job-id>', 'status': 'queued', 'status_url': '/api/v1/ai/jobs/<job-id>'}}}
        app.openapi_schema = doc
        return doc
    app.openapi = schema
