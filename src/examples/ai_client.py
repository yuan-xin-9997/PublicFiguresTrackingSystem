"""OAuth machine client with automatic renewal; never prints credentials or tokens.

Environment: PFTS_BASE_URL, PFTS_CLIENT_ID, PFTS_CLIENT_SECRET.
Run: python src/examples/ai_client.py
"""
import asyncio
import os
import time

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider


class MemoryTokenStorage:
    """Tokens and client metadata live only in process memory."""
    tokens = None
    client_info = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info


class MachineAuth(httpx2.Auth):
    requires_response_body = True

    def __init__(self, base_url, resource):
        self.base_url = base_url.rstrip('/')
        self.resource = resource
        self.token = ''
        self.expires = 0
        self.lock = asyncio.Lock()

    async def async_auth_flow(self, request):
        async with self.lock:
            if time.monotonic() >= self.expires:
                async with httpx2.AsyncClient(trust_env=False) as client:
                    response = await client.post(self.base_url + '/oauth/token',
                        auth=(os.environ['PFTS_CLIENT_ID'], os.environ['PFTS_CLIENT_SECRET']),
                        data={'grant_type': 'client_credentials', 'resource': self.resource})
                if response.status_code != 200:
                    raise RuntimeError('OAuth client authentication failed: ' + str(response.status_code))
                value = response.json()
                self.token = value['access_token']
                self.expires = time.monotonic() + max(1, value['expires_in'] - 15)
        request.headers['Authorization'] = 'Bearer ' + self.token
        yield request


async def main():
    base = os.environ['PFTS_BASE_URL'].rstrip('/')
    provider = ClientCredentialsOAuthProvider(server_url=base + '/mcp', storage=MemoryTokenStorage(),
        client_id=os.environ['PFTS_CLIENT_ID'], client_secret=os.environ['PFTS_CLIENT_SECRET'], issuer=base,
        scope=os.environ.get('PFTS_SCOPES', 'persons:read'))
    async with httpx2.AsyncClient(auth=provider, trust_env=False, follow_redirects=True) as http:
        async with streamable_http_client(base + '/mcp/', http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                print('tools:', ', '.join(t.name for t in tools.tools))
                result = await session.call_tool('list_persons', {'page': 1, 'page_size': 5})
                if result.is_error:
                    raise RuntimeError('MCP business query failed')
                print(result.structured_content or result.content)


if __name__ == '__main__':
    asyncio.run(main())
