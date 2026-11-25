"""Request-only client for Orchestration's local-runtime authority."""
from __future__ import annotations
import json
from uuid import uuid4
from urllib.parse import urlsplit
import httpx

SCHEMA = 'orchestration-control/1'

class ManagerError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.extra = status, extra

class RigUnavailable(ManagerError):
    pass

class Profiles:
    """Display projection replaced only by a validated owner response."""
    def __init__(self):
        self.rows, self.chat_offers, self.shared, self.route_prefixes, self.summary = {}, {}, {}, {}, {}
    def all(self):
        return self.rows
    def replace(self, value):
        if not isinstance(value, dict) or any(not isinstance(value.get(k), dict) for k in
                ('profiles', 'chat_offers', 'shared_profiles', 'route_prefixes', 'summary')):
            raise ManagerError(502, 'Invalid Orchestration snapshot')
        self.rows = value['profiles']
        self.chat_offers = value['chat_offers']
        self.shared.clear(); self.shared.update(value['shared_profiles'])
        self.route_prefixes.clear(); self.route_prefixes.update(value['route_prefixes'])
        self.summary = value['summary']

class RuntimeView:
    def __init__(self, owner):
        self.owner = owner
    async def status(self):
        return await self.owner.call('rig.status')
    async def ensure_ready(self, profile_id, start=None):
        return (await self.owner.call('route.prepare', {'profile': profile_id}))['port']
    async def shared_kv(self, action, **kwargs):
        if action != 'turn':
            raise ManagerError(400, 'Local lifecycle changes require an Orchestration operation')
        return await self.owner.call('shared.turn', kwargs)
    def loading(self):
        return []

class OrchestrationClient:
    def __init__(self, endpoint, token, *, timeout=900, transport=None, model_prefix="gpu-rig"):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('ORCHESTRATION_URL must be an explicit HTTP(S) service URL')
        if not token:
            raise ValueError('ORCHESTRATION_CONTROL_TOKEN is required for local model access')
        self.endpoint, self.token, self.timeout, self.transport = endpoint.rstrip('/'), token, timeout, transport
        self.model_prefix = model_prefix
        self.profiles = Profiles()
        self.rig = RuntimeView(self)

    async def call(self, operation, arguments=None, *, request_id=None, timeout=None):
        rid = request_id or uuid4().hex
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout, transport=self.transport) as client:
                response = await client.post(self.endpoint + '/v1/control', headers={'Authorization': 'Bearer ' + self.token},
                    json={'schema': SCHEMA, 'request_id': rid, 'operation': operation, 'arguments': arguments or {}})
            if response.status_code in (401, 403):
                raise ManagerError(503, 'Orchestration connection authentication failed', request_id=rid)
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RigUnavailable(502, 'Orchestration request failed; reconcile the original request ID before retrying', request_id=rid) from exc
        if not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('request_id') != rid or type(value.get('ok')) is not bool:
            raise RigUnavailable(502, 'Orchestration response version or identity mismatch', request_id=rid)
        if not value['ok']:
            if not isinstance(value.get('error'), dict):
                raise RigUnavailable(502, 'Malformed Orchestration error response', request_id=rid)
            error = dict(value['error'])
            status = error.pop('status', 502)
            if type(status) is not int or not 400 <= status <= 599:
                status = 502
            raise ManagerError(status, error.pop('message', 'Runtime operation refused'), request_id=rid, **error)
        if 'result' not in value:
            raise RigUnavailable(502, 'Orchestration response has no result', request_id=rid)
        return value['result']

    async def refresh(self):
        snapshot = await self.call('snapshot', timeout=min(self.timeout, 3))
        if snapshot.get('model_prefix') != self.model_prefix:
            raise ManagerError(502, 'Orchestration model prefix differs from Gateway configuration')
        self.profiles.replace(snapshot)

    async def http(self, method, path, body=None, query=None, request_id=None):
        return await self.call('manager.http', {'method': method, 'path': path, 'body': body, 'query': query}, request_id=request_id)

    async def prepare_chat_profile(self, profile, spec=None, *, owner=None):
        return await self.call('chat.prepare', {'profile': profile, 'owner': owner or profile})

    async def release_chat(self, owner, expected_instance=None, expected_chat=None, *, park=False):
        return await self.call('chat.release', {'owner': owner, 'expected_instance': expected_instance, 'expected_chat': expected_chat, 'park': park})

    async def switch_chat(self, target, owner, reset_loaded=False):
        return await self.call('chat.switch', {'target': target, 'owner': owner, 'reset_loaded': reset_loaded})

    async def reset_chat(self, target, expected, *, unload=False, expected_instance=None):
        return await self.call('chat.reset', {'target': target, 'expected': expected, 'unload': unload, 'expected_instance': expected_instance})

    async def status(self, *args, **kwargs):
        return await self.call('manager.status', {'args': list(args), 'kwargs': kwargs})

    async def chat_availability(self, *args, **kwargs):
        return await self.call('manager.chat_availability', {'args': list(args), 'kwargs': kwargs})

    async def unload_profile(self, *args, **kwargs):
        return await self.call('manager.unload_profile', {'args': list(args), 'kwargs': kwargs})

    async def load_model(self, *args, **kwargs):
        return await self.call('manager.load_model', {'args': list(args), 'kwargs': kwargs})

    async def kill(self, *args, **kwargs):
        return await self.call('manager.kill', {'args': list(args), 'kwargs': kwargs})

    async def remove_claim(self, *args, **kwargs):
        return await self.call('manager.remove_claim', {'args': list(args), 'kwargs': kwargs})

    async def recover_run(self, *args, **kwargs):
        return await self.call('manager.recover_run', {'args': list(args), 'kwargs': kwargs})

    async def dismiss_run(self, *args, **kwargs):
        return await self.call('manager.dismiss_run', {'args': list(args), 'kwargs': kwargs})

    async def stop_run(self, *args, **kwargs):
        return await self.call('manager.stop_run', {'args': list(args), 'kwargs': kwargs})

    async def cancel_item(self, *args, **kwargs):
        return await self.call('manager.cancel_item', {'args': list(args), 'kwargs': kwargs})
