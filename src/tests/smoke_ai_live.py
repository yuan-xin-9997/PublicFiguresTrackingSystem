"""Real network MCP/OAuth/API check. Credentials never leave process memory."""
import asyncio
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
    key = None
    with httpx.Client(base_url=base, trust_env=False, timeout=30) as web:
        login = web.post('/api/v1/auth/login', json={'username': os.environ['PFTS_SMOKE_USER'], 'password': os.environ['PFTS_SMOKE_PASSWORD']})
        login.raise_for_status()
        original = web.get('/api/v1/dashboard/summary').json()['counts']
        try:
            created = web.post('/api/v1/ai/credentials', headers={'X-PFTS-Request': '1'}, json={'name': 'deployment-smoke-' + uuid.uuid4().hex[:8], 'expires_in_days': 1, 'scopes': ['persons:read', 'persons:write']})
            created.raise_for_status()
            key = created.json()
            assert created.headers['cache-control'] == 'no-store'
            listed = web.get('/api/v1/ai/credentials').text
            assert key['client_secret'] not in listed and 'secret_hash' not in listed
            resource = base + '/api/v1'
            response = web.post('/oauth/token', auth=(key['id'], key['client_secret']), data={'grant_type': 'client_credentials', 'resource': resource})
            response.raise_for_status()
            machine_headers = {'Authorization': 'Bearer ' + response.json()['access_token']}
            with httpx.Client(base_url=base, trust_env=False, headers=machine_headers) as machine:
                assert machine.get('/api/v1/persons?page=1&page_size=2').status_code == 200
                assert machine.get('/api/v1/users').status_code == 403
            with httpx.Client(base_url=base, trust_env=False) as anonymous:
                assert anonymous.get('/api/v1/persons').status_code == 401
                assert anonymous.get('/api/v1/persons', headers={'Authorization': 'Bearer '}).status_code == 401
                assert anonymous.get('/api/v1/persons', headers={'Authorization': 'Bearer invalid'}).status_code == 401
                assert anonymous.post('/mcp/', json={}).status_code == 401
            provider = ClientCredentialsOAuthProvider(server_url=base + '/mcp', storage=MemoryTokenStorage(), client_id=key['id'], client_secret=key['client_secret'], issuer=base)
            async with httpx2.AsyncClient(auth=provider, trust_env=False, timeout=30) as http:
                async with streamable_http_client(base + '/mcp/', http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()
                        assert len(tools.tools) == 11
                        read = await session.call_tool('list_persons', {'page_size': 2})
                        assert not read.is_error
                        body = {'name': 'AI 接入部署验证-' + uuid.uuid4().hex[:8], 'idempotency_key': 'smoke-' + uuid.uuid4().hex}
                        write = await session.call_tool('create_person', body)
                        assert not write.is_error
                        person_id = write.structured_content['id']
                        again = await session.call_tool('create_person', body)
                        assert not again.is_error and again.structured_content['id'] == person_id
                        print('MCP protocol:', initialized.protocol_version, '; tools:', len(tools.tools), '; query/write/replay: passed')
            revoked = web.delete('/api/v1/ai/credentials/' + key['id'], headers={'X-PFTS-Request': '1'})
            revoked.raise_for_status()
            with httpx.Client(base_url=base, trust_env=False) as machine:
                assert machine.get('/api/v1/persons', headers=machine_headers).status_code == 401
                assert machine.post('/oauth/token', auth=(key['id'], key['client_secret']), data={'grant_type': 'client_credentials', 'resource': resource}).status_code == 401
            print('Live credential creation, query, owner list, revocation and authentication failures: passed')
            assert web.get('/api/v1/events?page_size=1').status_code == 200
        finally:
            if key:
                web.delete('/api/v1/ai/credentials/' + key['id'], headers={'X-PFTS-Request': '1'}).raise_for_status()
            if person_id:
                web.delete('/api/v1/persons/' + str(person_id)).raise_for_status()
        current = web.get('/api/v1/dashboard/summary').json()['counts']
        assert current['persons'] == original['persons']
        print('Existing login, timeline and original enabled-person count: passed')


if __name__ == '__main__':
    asyncio.run(main())
