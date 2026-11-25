"""Optional Harness run transport. No Harness state, implementation imports or provider credentials."""
import httpx

SCHEMA = 'harness-control/1'

class HarnessClient:
    def __init__(self, endpoint, token):
        if not token:
            raise ValueError('Harness service token is required')
        self.endpoint = endpoint.rstrip('/')
        self.token = token

    async def request(self, method, path, body=None, *, transport=None):
        async with httpx.AsyncClient(transport=transport, timeout=30) as client:
            response = await client.request(method, self.endpoint + path,
                json=body, headers={'Authorization': 'Bearer ' + self.token})
        value = response.json()
        if not isinstance(value, dict) or value.get('schema') != SCHEMA:
            raise ValueError('unsupported Harness control schema')
        return response.status_code, value

    @staticmethod
    def validate_events(packet, expected_sequence):
        if packet.get('schema') != SCHEMA or not isinstance(packet.get('events'), list):
            raise ValueError('unsupported Harness event envelope')
        accepted = []
        for event in packet['events']:
            if event.get('schema_version') != 'harness-event/v1':
                raise ValueError('unsupported Harness event schema')
            seq = event.get('sequence')
            if type(seq) is not int:
                raise ValueError('invalid event sequence')
            if seq < expected_sequence:
                continue  # reconnect may replay a previously consumed event
            if seq != expected_sequence:
                raise ValueError('Harness event gap; reconnect from last durable cursor')
            accepted.append(event)
            expected_sequence += 1
        return accepted, expected_sequence
