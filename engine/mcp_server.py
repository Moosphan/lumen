#!/usr/bin/env python3
"""Local MCP stdio transport for Lumen's summary, draft, and status tools."""
import json
import os
import sys
from pathlib import Path

if __package__:
    from .lumen import mcp_review, service
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lumen import mcp_review, service


PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
MAX_MESSAGE_BYTES = 65536


def tool(name, cmd, description, properties=(), read_only=True):
    fields = dict(properties)
    return cmd, {
        'name': name, 'description': description,
        'inputSchema': {
            'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False,
        },
        'annotations': {
            'readOnlyHint': read_only, 'destructiveHint': False, 'idempotentHint': read_only,
            'openWorldHint': False,
        },
    }


_STRING = {'type': 'string'}
TOOLS = {
    'lumen_summary': tool(
        'lumen_summary', 'mcp_summary',
        'Read totals and up to 200 opaque aliases from the latest authorized Lumen cleanup scan.'),
    'lumen_create_draft': tool(
        'lumen_create_draft', 'mcp_draft',
        'Submit selected aliases for quarantine review in the Lumen app. The user approves each list in Lumen.',
        {'snapshot': _STRING, 'aliases': {
            'type': 'array', 'items': _STRING, 'minItems': 1, 'maxItems': mcp_review.MAX_DRAFT_ITEMS,
            'uniqueItems': True,
        }}, read_only=False),
    'lumen_draft_status': tool(
        'lumen_draft_status', 'mcp_status',
        'Check a draft and its quarantine outcome using the status token from its submission.',
        {'draft_id': _STRING, 'status_token': _STRING}),
}


def _reply(request_id, result):
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def _error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def _valid_id(value):
    return type(value) in (str, int)


def _tool_result(data, error=False):
    message = data if error else json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    return {'content': [{'type': 'text', 'text': message}], 'isError': error}


class Session:
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.negotiated = None
        self.ready = False

    def receive(self, request):
        if not isinstance(request, dict):
            return _error(None, -32600, 'Invalid request.')
        request_id = request.get('id')
        method = request.get('method')
        if (request.get('jsonrpc') != '2.0' or not isinstance(method, str)
                or ('id' in request and not _valid_id(request_id))):
            return _error(None, -32600, 'Invalid request.')
        if 'id' not in request:
            if method == 'notifications/initialized' and self.negotiated is not None:
                self.ready = True
            return None
        params = request.get('params', {})
        if not isinstance(params, dict):
            return _error(request_id, -32602, 'Invalid parameters.')
        if method == 'initialize':
            return self.initialize(request_id, params)
        if method == 'ping':
            return _reply(request_id, {})
        if method not in ('tools/list', 'tools/call'):
            return _error(request_id, -32601, 'Method not found.')
        if not self.ready:
            return _error(request_id, -32002, 'MCP initialization is required.')
        if method == 'tools/list':
            if params.get('cursor') is not None:
                return _error(request_id, -32602, 'Invalid cursor.')
            return _reply(request_id, {'tools': [spec for _, spec in TOOLS.values()]})
        return self.call(request_id, params)

    def initialize(self, request_id, params):
        if self.negotiated is not None:
            return _error(request_id, -32600, 'MCP is already initialized.')
        offered = params.get('protocolVersion')
        if (not isinstance(offered, str) or not isinstance(params.get('capabilities'), dict)
                or not isinstance(params.get('clientInfo'), dict)):
            return _error(request_id, -32602, 'Invalid initialization parameters.')
        self.negotiated = offered if offered in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _reply(request_id, {
            'protocolVersion': self.negotiated, 'capabilities': {'tools': {}},
            'serverInfo': {'name': 'lumen', 'version': '0.1.0'},
        })

    def call(self, request_id, params):
        name, args = params.get('name'), params.get('arguments', {})
        if not isinstance(name, str) or name not in TOOLS:
            return _error(request_id, -32602, 'Unknown tool.')
        cmd, spec = TOOLS[name]
        if not isinstance(args, dict) or set(args) != set(spec['inputSchema']['required']):
            return _error(request_id, -32602, 'Invalid tool arguments.')
        try:
            return _reply(request_id, _tool_result(self.dispatch({'cmd': cmd, **args})))
        except mcp_review.ReviewError as error:
            return _reply(request_id, _tool_result(str(error), error=True))
        except Exception:
            # Backend exception text may include a private path or journal contents.
            return _reply(request_id, _tool_result('Lumen could not complete the request.', error=True))


def _invalid_constant(_):
    raise ValueError('Invalid JSON constant.')


def serve(incoming, outgoing, dispatch):
    session = Session(dispatch)
    while line := incoming.readline(MAX_MESSAGE_BYTES + 1):
        too_large = len(line) > MAX_MESSAGE_BYTES
        if too_large:
            reply = _error(None, -32600, 'Message is too large.')
        else:
            try:
                request = json.loads(line.decode('utf-8'), parse_constant=_invalid_constant)
            except (UnicodeDecodeError, ValueError, RecursionError):
                reply = _error(None, -32700, 'Invalid JSON.')
            else:
                reply = session.receive(request)
        if reply is not None:
            outgoing.write(json.dumps(reply, separators=(',', ':')).encode() + b'\n')
            outgoing.flush()
        if too_large:
            break


def main():
    state_override = os.environ.get('LUMEN_STATE_DIR')
    state = Path(state_override) if state_override else Path.home() / 'Library/Application Support/org.lumen.cleaner'
    fixture_home = os.environ.get('LUMEN_INVENTORY_HOME') if state_override else None
    home = Path(fixture_home).resolve() if fixture_home else Path.home()

    def dispatch(request):
        return service.dispatch(state, request, local_home=home, include_system=fixture_home is None)

    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, dispatch)
    except BrokenPipeError:
        return


if __name__ == '__main__':
    main()
