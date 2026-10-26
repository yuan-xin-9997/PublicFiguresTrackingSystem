import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
from examples.ai_client import MemoryTokenStorage


MANAGE = {'X-PFTS-Request': '1'}


def credential(client, scopes=None):
    r = client.post('/api/v1/ai/credentials', headers=MANAGE, json={
        'name': 'automated-test', 'expires_in_days': 1, 'scopes': scopes or ['persons:read', 'persons:write']})
    assert r.status_code == 201, r.text
    assert r.headers['cache-control'] == 'no-store'
    return r.json()


def token(client, key, resource='http://testserver/api/v1'):
    return client.post('/oauth/token', auth=(key['id'], key['client_secret']),
                       data={'grant_type': 'client_credentials', 'resource': resource})


def headers(client, key, resource='http://testserver/api/v1'):
    r = token(client, key, resource)
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['access_token']}


def test_credentials_lifecycle_and_auth(admin_client):
    c = admin_client
    key = credential(c)
    h = headers(c, key)
    assert c.get('/api/v1/persons', headers=h).status_code == 200
    c.cookies.clear()
    for bad in [None, '', 'Bearer ', 'Bearer wrong', 'Basic bad']:
        assert c.get('/api/v1/persons', headers={} if bad is None else {'Authorization': bad}).status_code == 401
    assert c.get('/api/v1/users', headers=h).status_code == 403
    assert c.get('/api/v1/ai/credentials', headers=h).status_code == 403
    assert token(c, {**key, 'client_secret': ''}).status_code == 401
    assert token(c, {**key, 'client_secret': 'wrong'}).status_code == 401
    db = c.app.state.db
    stored = db.fetch_one('SELECT * FROM ai_credentials WHERE id=?', (key['id'],))
    assert key['client_secret'] not in json.dumps(stored)
    c.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'admin123'})
    listed = c.get('/api/v1/ai/credentials').text
    assert key['client_secret'] not in listed and 'secret_hash' not in listed
    assert c.delete('/api/v1/ai/credentials/' + key['id'], headers=MANAGE).status_code == 200
    assert c.get('/api/v1/persons', headers=h).status_code == 401
    assert token(c, key).status_code == 401
    expired = credential(c)
    expired_h = headers(c, expired)
    db.execute("UPDATE ai_credentials SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (expired['id'],))
    assert token(c, expired).status_code == 401
    assert c.get('/api/v1/persons', headers=expired_h).status_code == 401
    assert key['client_secret'] not in json.dumps(db.fetch_all('SELECT * FROM audit_logs'))


def test_openapi_and_token_expiry(admin_client):
    c = admin_client
    doc = c.get('/openapi.json')
    assert doc.status_code == 200, doc.text
    spec = doc.json()
    assert spec['paths']['/oauth/token']['post']['requestBody']['required']
    assert spec['paths']['/api/v1/persons']['post']['responses']['201']['content']['application/json']['example']['id']
    key = credential(c)
    h = headers(c, key)
    c.app.state.db.execute("UPDATE ai_tokens SET expires_at='2000-01-01T00:00:00+00:00'")
    assert c.get('/api/v1/persons', headers=h).status_code == 401
    h = headers(c, key)
    c.app.state.db.execute("INSERT OR IGNORE INTO page_permissions VALUES(1,'ai_access',1)")
    c.app.state.db.execute("INSERT OR IGNORE INTO page_permissions VALUES(1,'persons',1)")
    c.app.state.db.execute("UPDATE users SET role='user' WHERE id=1")
    assert c.get('/api/v1/persons', headers=h).status_code == 200
    assert c.post('/api/v1/persons', headers={**h, 'Idempotency-Key': 'demoted-user-001'}, json={'name': 'forbidden'}).status_code == 403


def test_user_scope_owner_and_live_permissions(admin_client):
    c = admin_client
    admin_key = credential(c)
    c.post('/api/v1/auth/logout')
    c.post('/api/v1/auth/login', json={'username': 'analyst', 'password': 'reader123'})
    assert c.post('/api/v1/ai/credentials', headers=MANAGE, json={'name': 'bad', 'scopes': ['persons:write']}).status_code == 403
    assert c.post('/api/v1/ai/credentials', headers=MANAGE, json={'name': 'bad', 'scopes': ['persons:read'], 'user_id': 1}).status_code == 422
    assert c.delete('/api/v1/ai/credentials/' + admin_key['id'], headers=MANAGE).status_code == 404
    assert admin_key['id'] not in c.get('/api/v1/ai/credentials').text
    key = credential(c, ['persons:read'])
    h = headers(c, key)
    assert c.get('/api/v1/persons', headers=h).status_code == 200
    uid = c.get('/api/v1/auth/me').json()['id']
    c.app.state.db.execute("DELETE FROM page_permissions WHERE user_id=? AND page_key='persons'", (uid,))
    assert c.get('/api/v1/persons', headers=h).status_code == 403
    assert token(c, key).status_code == 403
    c.app.state.db.execute('UPDATE users SET enabled=0 WHERE id=?', (uid,))
    assert c.get('/api/v1/persons', headers=h).status_code == 401
    assert token(c, key).status_code == 401


def test_idempotency_concurrent_validation_and_csrf(admin_client):
    c = admin_client
    assert c.post('/api/v1/ai/credentials', json={'name': 'bad', 'scopes': ['persons:read']}).status_code == 403
    assert c.post('/api/v1/ai/credentials', headers={**MANAGE, 'Origin': 'https://evil.invalid'}, json={'name': 'bad', 'scopes': ['persons:read']}).status_code == 403
    key = credential(c)
    h = {**headers(c, key), 'Idempotency-Key': 'concurrent-001'}
    def write(_):
        return c.post('/api/v1/persons', headers=h, json={'name': 'AI test'})
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write, range(6)))
    assert all(r.status_code in {201, 409} for r in results)
    assert len(c.app.state.db.fetch_all("SELECT id FROM public_figures WHERE name='AI test'")) == 1
    first = next(r.json()['id'] for r in results if r.status_code == 201)
    again = write(0)
    assert again.status_code == 201 and again.json()['id'] == first
    assert c.post('/api/v1/persons', headers=h, json={'name': 'different'}).status_code == 409
    assert c.post('/api/v1/persons', headers=headers(c, key), json={'name': 'missing-key'}).status_code == 422
    assert c.get('/api/v1/persons?page=0', headers=h).json()['error']['code'] == 'VALIDATION_ERROR'
    app = c.app
    app.state.settings.values['ai_access']['requests_per_minute'] = 1
    app.state.db.execute('DELETE FROM ai_rate_limits')
    assert c.get('/api/v1/persons', headers=h).status_code == 200
    assert c.get('/api/v1/persons', headers=h).status_code == 429


def test_async_task_and_restart(admin_client):
    c = admin_client
    source = c.post('/api/v1/sources', json={'name': 'manual', 'type': 'manual'}).json()['id']
    target = c.app.state.db.fetch_one('SELECT id FROM collection_tasks WHERE source_id=?', (source,))['id']
    key = credential(c, ['tasks:execute', 'tasks:read'])
    h = {**headers(c, key), 'Idempotency-Key': 'async-task-001'}
    r = c.post('/api/v1/ai/jobs', headers=h, json={'kind': 'collection', 'target_id': target})
    assert r.status_code == 202, r.text
    job_id = r.json()['id']
    assert c.post('/api/v1/ai/jobs', headers=h, json={'kind': 'collection', 'target_id': target}).json()['id'] == job_id
    for _ in range(100):
        job = c.get('/api/v1/ai/jobs/' + job_id, headers=h).json()
        if job['status'] in {'completed', 'failed'}:
            break
        time.sleep(.05)
    assert job['status'] == 'completed', job
    assert isinstance(job['result'], dict)
    c.app.state.ai_worker.stop()
    db = c.app.state.db
    db.execute("UPDATE ai_jobs SET status='running' WHERE id=?", (job_id,))
    from app.backend.ai_access import JobWorker
    worker = JobWorker(c.app)
    worker.start(); worker.stop()
    assert db.fetch_one('SELECT * FROM ai_jobs WHERE id=?', (job_id,))['status'] == 'failed'
    assert c.get('/api/v1/persons', headers=h).status_code == 403


def test_real_mcp_sdk_client(admin_client):
    c = admin_client
    key = credential(c)
    h = headers(c, key, 'http://testserver/mcp')
    assert c.post('/mcp/', json={}).status_code == 401
    assert c.post('/mcp/', headers=headers(c, key), json={}).status_code == 401
    assert c.get('/api/v1/persons', headers=h).status_code == 401

    async def run():
        provider = ClientCredentialsOAuthProvider(server_url='http://testserver/mcp', storage=MemoryTokenStorage(), client_id=key['id'], client_secret=key['client_secret'], issuer='http://testserver', scope='persons:read persons:write')
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=c.app), base_url='http://testserver', auth=provider, follow_redirects=True) as http:
            async with streamable_http_client('http://testserver/mcp/', http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    initialized = await session.initialize()
                    assert initialized.server_info.name == '人物足迹'
                    tools = await session.list_tools()
                    assert len(tools.tools) == 11
                    query = await session.call_tool('list_persons', {'page': 1, 'page_size': 2})
                    assert not query.is_error, query
                    write = await session.call_tool('create_person', {'name': 'SDK created', 'idempotency_key': 'sdk-create-001'})
                    assert not write.is_error, write
                    repeat = await session.call_tool('create_person', {'name': 'SDK created', 'idempotency_key': 'sdk-create-001'})
                    assert not repeat.is_error
                    bad = await session.call_tool('get_person', {'person_id': 999999})
                    assert bad.is_error
    asyncio.run(run())
    assert len(c.app.state.db.fetch_all("SELECT id FROM public_figures WHERE name='SDK created'")) == 1
