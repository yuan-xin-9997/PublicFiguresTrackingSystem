"""Real network MCP/OAuth/API check. Credentials never leave process memory."""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.ai_client import MemoryTokenStorage


async def main():
    base = os.environ.get('PFTS_SMOKE_URL', 'http://127.0.0.1:28000').rstrip('/')
    person_id = None
    source_id = None
    created_name = None
    key = None
    with httpx.Client(base_url=base, trust_env=False, timeout=30) as web:
        login = web.post('/api/v1/auth/login', json={'username': os.environ['PFTS_SMOKE_USER'], 'password': os.environ['PFTS_SMOKE_PASSWORD']})
        login.raise_for_status()
        original = web.get('/api/v1/dashboard/summary').json()['counts']
        canonical = web.get('/api/v1/ai/access').json()['api'].removesuffix('/api/v1')
        try:
            created = web.post('/api/v1/ai/credentials', headers={'X-PFTS-Request': '1'}, json={'name': 'deployment-smoke-' + uuid.uuid4().hex[:8], 'expires_in_days': 1, 'scopes': ['persons:read', 'persons:write', 'tasks:execute', 'tasks:read']})
            created.raise_for_status()
            key = created.json()
            assert created.headers['cache-control'] == 'no-store'
            listed = web.get('/api/v1/ai/credentials').text
            assert key['client_secret'] not in listed and 'secret_hash' not in listed
            resource = canonical + '/api/v1'
            response = web.post('/oauth/token', auth=(key['id'], key['client_secret']), data={'grant_type': 'client_credentials', 'resource': resource})
            response.raise_for_status()
            machine_headers = {'Authorization': 'Bearer ' + response.json()['access_token']}
            sensitive_values = [key['client_secret'].encode(), response.json()['access_token'].encode()]
            with httpx.Client(base_url=base, trust_env=False, headers=machine_headers) as machine:
                assert machine.get('/api/v1/persons?page=1&page_size=2').status_code == 200
                assert machine.get('/api/v1/users').status_code == 403
                source = web.post('/api/v1/sources', json={'name': 'AI async smoke-' + uuid.uuid4().hex[:8], 'type': 'manual', 'enabled': False})
                source.raise_for_status()
                source_id = source.json()['id']
                target_id = next(t['id'] for t in web.get('/api/v1/tasks').json()['items'] if t['source_id'] == source_id)
                job_body = {'kind': 'collection', 'target_id': target_id}
                idem = {'Idempotency-Key': 'job-smoke-' + uuid.uuid4().hex}
                submitted = machine.post('/api/v1/ai/jobs', json=job_body, headers=idem)
                assert submitted.status_code == 202
                job_id = submitted.json()['id']
                repeated = machine.post('/api/v1/ai/jobs', json=job_body, headers=idem)
                assert repeated.status_code == 202 and repeated.json()['id'] == job_id
                for _ in range(25):
                    job = machine.get('/api/v1/ai/jobs/' + job_id).json()
                    if job['status'] in {'completed', 'failed'}:
                        break
                    await asyncio.sleep(.4)
                assert job['status'] == 'completed' and job['result']['status'] == 'success'
                print('Live asynchronous manual-source task, result and idempotency: passed')
            with httpx.Client(base_url=base, trust_env=False) as anonymous:
                assert anonymous.get('/api/v1/persons').status_code == 401
                assert anonymous.get('/api/v1/persons', headers={'Authorization': ''}).status_code == 401
                assert anonymous.get('/api/v1/persons', headers={'Authorization': 'Bearer invalid'}).status_code == 401
                assert anonymous.post('/mcp/', json={}).status_code == 401
            storage = MemoryTokenStorage()
            provider = ClientCredentialsOAuthProvider(server_url=canonical + '/mcp', storage=storage, client_id=key['id'], client_secret=key['client_secret'], issuer=canonical)
            async with httpx2.AsyncClient(auth=provider, trust_env=False, timeout=30) as http:
                async with streamable_http_client(canonical + '/mcp/', http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()
                        assert len(tools.tools) == 11
                        read = await session.call_tool('list_persons', {'page_size': 2})
                        assert not read.is_error
                        body = {'name': 'AI 接入部署验证-' + uuid.uuid4().hex[:8], 'idempotency_key': 'smoke-' + uuid.uuid4().hex}
                        created_name = body['name']
                        write = await session.call_tool('create_person', body)
                        assert not write.is_error
                        payload = write.structured_content or json.loads(write.content[0].text)
                        person_id = payload['id']
                        again = await session.call_tool('create_person', body)
                        repeated = again.structured_content or json.loads(again.content[0].text)
                        assert not again.is_error and repeated['id'] == person_id
                        print('MCP protocol:', initialized.protocol_version, '; tools:', len(tools.tools), '; query/write/replay: passed')
                async with streamable_http_client(canonical + '/mcp/', http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.discover()
                        assert session.protocol_version == '2026-07-28'
                        assert len((await session.list_tools()).tools) == 11
                        assert not (await session.call_tool('list_persons', {'page_size': 2})).is_error
                        assert not (await session.call_tool('create_person', body)).is_error
                        print('Current MCP protocol:', session.protocol_version, '; discover/query/write-replay: passed')
            stored_token = await storage.get_tokens()
            sensitive_values.append(stored_token.access_token.encode())
            revoked = web.delete('/api/v1/ai/credentials/' + key['id'], headers={'X-PFTS-Request': '1'})
            revoked.raise_for_status()
            with httpx.Client(base_url=base, trust_env=False) as machine:
                assert machine.get('/api/v1/persons', headers=machine_headers).status_code == 401
                assert machine.post('/oauth/token', auth=(key['id'], key['client_secret']), data={'grant_type': 'client_credentials', 'resource': resource}).status_code == 401
            print('Live credential creation, query, owner list, revocation and authentication failures: passed')
            root = Path(__file__).resolve().parents[1]
            for directory in ([root / 'logs', root / 'app' / 'frontend' / 'dist'] if os.environ.get('PFTS_SMOKE_SCAN_LOCAL') == '1' else []):
                if directory.exists():
                    for file in directory.rglob('*'):
                        if file.is_file():
                            with file.open('rb') as stream:
                                tail = b''
                                while chunk := stream.read(1024 * 1024):
                                    combined = tail + chunk
                                    assert not any(value in combined for value in sensitive_values), 'Credential leakage in logs or frontend build'
                                    tail = combined[-256:]
            if os.environ.get('PFTS_SMOKE_SCAN_LOCAL') == '1':
                print('Generated credentials absent from server log files and built frontend assets: passed')
            assert web.get('/api/v1/events?page_size=1').status_code == 200
        finally:
            if key:
                web.delete('/api/v1/ai/credentials/' + key['id'], headers={'X-PFTS-Request': '1'}).raise_for_status()
            if not person_id and created_name:
                matching = web.get('/api/v1/persons', params={'q': created_name}).json()['items']
                person_id = next((p['id'] for p in matching if p['name'] == created_name), None)
            if person_id:
                web.delete('/api/v1/persons/' + str(person_id)).raise_for_status()
            if source_id:
                web.delete('/api/v1/sources/' + str(source_id)).raise_for_status()
        current = web.get('/api/v1/dashboard/summary').json()['counts']
        assert current['persons'] == original['persons']
        print('Existing login, timeline and original enabled-person count: passed')


if __name__ == '__main__':
    asyncio.run(main())
