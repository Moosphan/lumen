import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from engine.lumen.mcp_review import ReviewError
from engine.lumen.service import dispatch
from engine.mcp_server import serve


def message(method, request_id=None, **params):
    result = dict(jsonrpc='2.0', method=method)
    if request_id is not None:
        result['id'] = request_id
    if params:
        result['params'] = params
    return result


def initialize(protocol='2025-11-25'):
    return message('initialize', 1, protocolVersion=protocol,
                   capabilities={}, clientInfo=dict(name='synthetic-test', version='1'))


class TransportTests(unittest.TestCase):
    def exchange(self, messages, dispatch=lambda _: {}):
        incoming = io.BytesIO(b''.join(
            line if isinstance(line, bytes) else json.dumps(line).encode() + b'\n' for line in messages))
        outgoing = io.BytesIO()
        serve(incoming, outgoing, dispatch)
        return [json.loads(line) for line in outgoing.getvalue().splitlines()]

    def test_initialization_and_tools_list_over_newline_delimited_messages(self):
        hello, pong, listing = self.exchange([
            initialize('2025-06-18'), message('notifications/initialized'),
            message('ping', 2), message('tools/list', 3),
        ])
        self.assertEqual(hello['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(pong['result'], {})
        self.assertIn('tools', hello['result']['capabilities'])
        tools = {tool['name']: tool for tool in listing['result']['tools']}
        self.assertEqual(set(tools), {'lumen_summary', 'lumen_create_draft', 'lumen_draft_status'})
        self.assertTrue(tools['lumen_summary']['annotations']['readOnlyHint'])
        self.assertFalse(tools['lumen_create_draft']['annotations']['readOnlyHint'])

    def test_only_allowlisted_commands_and_arguments_reach_service(self):
        calls = []

        def dispatch(request):
            calls.append(request)
            return {'marker': request['cmd']}

        replies = self.exchange([
            initialize(), message('notifications/initialized'),
            message('tools/call', 2, name='lumen_summary'),
            message('tools/call', 3, name='lumen_create_draft', arguments={'snapshot': 's', 'aliases': ['a']}),
            message('tools/call', 4, name='lumen_draft_status', arguments={'draft_id': 'd', 'status_token': 't'}),
            message('tools/call', 5, name='mcp_approve', arguments={'paths': ['/outside']}),
            message('tools/call', 6, name='lumen_summary', arguments={'cmd': 'mcp_approve'}),
            message('tools/call', 7, name='lumen_create_draft', arguments={'snapshot': 's', 'aliases': ['/outside'],
                                                                         'paths': ['/outside']}),
        ], dispatch)
        self.assertEqual(calls, [
            {'cmd': 'mcp_summary'}, {'cmd': 'mcp_draft', 'snapshot': 's', 'aliases': ['a']},
            {'cmd': 'mcp_status', 'draft_id': 'd', 'status_token': 't'},
        ])
        self.assertEqual([item['error']['code'] for item in replies[-3:]], [-32602] * 3)
        self.assertEqual(json.loads(replies[2]['result']['content'][0]['text']), {'marker': 'mcp_draft'})
        self.assertNotIn('/outside', json.dumps(replies))

    def test_protocol_and_backend_errors_do_not_echo_private_details(self):
        def dispatch(request):
            if request['cmd'] == 'mcp_summary':
                raise OSError('file in /private/home/missing')
            if request['cmd'] == 'mcp_draft':
                raise sqlite3.OperationalError('journal in /private/home/missing')
            raise ReviewError('Draft status is unavailable.')

        replies = self.exchange([
            b'not JSON with /private/home\n', b'\xff\n', {'jsonrpc': '2.0', 'id': 10, 'method': []},
            message('tools/list', 9), initialize(), message('notifications/initialized'),
            message('this/method//private/home', 2),
            message('tools/call', 3, name='lumen_summary'),
            message('tools/call', 4, name='lumen_draft_status', arguments={'draft_id': 'd', 'status_token': 't'}),
            message('tools/call', 5, name='lumen_create_draft', arguments={'snapshot': 's', 'aliases': ['a']}),
        ], dispatch)
        self.assertEqual([reply['error']['code'] for reply in replies[:4]], [-32700, -32700, -32600, -32002])
        self.assertEqual(replies[5]['error']['code'], -32601)
        self.assertTrue(replies[6]['result']['isError'])
        self.assertTrue(replies[7]['result']['isError'])
        self.assertIn('Draft status is unavailable.', json.dumps(replies[7]))
        self.assertTrue(replies[8]['result']['isError'])
        self.assertNotIn('/private/home', json.dumps(replies))


class IsolatedWorkflowTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name).resolve()
        self.root = self.scratch / 'workspace'
        self.root.mkdir()
        self.candidate = self.root / 'private-candidate.zip'
        self.candidate.write_bytes(b'candidate that requires explicit review')
        self.spare = self.root / 'private-spare.bin'
        self.spare.write_bytes(b'spare')
        self.secret = self.root / '.ssh'
        self.secret.mkdir()
        (self.secret / 'private-key').write_bytes(b'synthetic credential')
        self.engine = Path(__file__).resolve().parents[1] / 'engine'
        self.state = self.scratch / 'state'
        self.fixture_home = self.scratch / 'unused-home'
        self.env = {**os.environ, 'LUMEN_STATE_DIR': str(self.state),
                    'LUMEN_INVENTORY_HOME': str(self.fixture_home)}

    def run_engine(self, script, payload):
        process = subprocess.run([sys.executable, '-I', '-B', str(self.engine / script)],
                                 input=payload, env=self.env, capture_output=True, text=True,
                                 timeout=30, check=True)
        self.assertEqual(process.stderr, '')
        return process.stdout

    def bridge(self, command, **args):
        return json.loads(self.run_engine('bridge.py', json.dumps(dict(cmd=command, **args))))

    def native(self, command, **args):
        return dispatch(self.state, dict(cmd=command, **args), local_home=self.fixture_home)

    def call(self, name, **arguments):
        output = self.run_engine('mcp_server.py', ''.join(json.dumps(request) + '\n' for request in [
            initialize(), message('notifications/initialized'), message('tools/call', 2, name=name, arguments=arguments),
        ]))
        self.assertNotIn(str(self.scratch), output)
        self.assertNotIn(self.candidate.name, output)
        self.assertNotIn(self.spare.name, output)
        self.assertNotIn('private-key', output)
        hello, reply = map(json.loads, output.splitlines())
        self.assertEqual(hello['result']['protocolVersion'], '2025-11-25')
        return reply

    def result(self, name, **arguments):
        result = self.call(name, **arguments)['result']
        self.assertFalse(result['isError'], result)
        return json.loads(result['content'][0]['text'])

    def allow(self):
        self.assertTrue(self.native('settings', mcp_enabled=True, roots=[str(self.root)])['mcp_enabled'])

    def draft(self, *paths):
        summary = self.result('lumen_summary')
        aliases = {row['size']: row['alias'] for row in summary['candidates']}
        return self.result('lumen_create_draft', snapshot=summary['snapshot'],
                           aliases=[aliases[path.stat().st_size] for path in paths])

    def status(self, draft):
        return self.result('lumen_draft_status', draft_id=draft['draft_id'], status_token=draft['status_token'])

    def test_default_off_and_exact_native_approval(self):
        self.assertTrue(self.call('lumen_summary')['result']['isError'])
        self.allow()
        summary = self.result('lumen_summary')
        self.assertEqual(summary['totals']['review']['count'], 2)
        draft = self.draft(self.candidate)
        queued, = self.native('mcp_pending')['drafts']
        self.assertEqual(queued['items'][0]['path'], str(self.candidate))
        self.assertEqual(self.status(draft)['status'], 'pending')
        self.assertIn('error', self.call('mcp_approve', draft_id=draft['draft_id'], paths=[str(self.candidate)]))
        with self.assertRaises(ReviewError):
            self.native('mcp_approve', draft_id=draft['draft_id'], paths=[str(self.candidate), str(self.spare)])
        self.assertTrue(self.candidate.exists())
        approved = self.native('mcp_approve', draft_id=draft['draft_id'], paths=[str(self.candidate)])
        self.assertFalse(self.candidate.exists())
        self.assertTrue(self.spare.exists())
        status = self.status(draft)
        self.assertEqual((status['status'], status['quarantined'], status['skipped']), ('completed', 1, 0))
        self.assertEqual(status['batch_id'], approved['last_action']['batch_id'])
        with self.assertRaises(ReviewError):
            self.native('mcp_approve', draft_id=draft['draft_id'], paths=[str(self.candidate)])
        self.assertTrue(self.bridge('restore', batch_id=status['batch_id'])['ok'])
        self.assertTrue(self.candidate.exists())
        self.assertFalse(self.native('settings', mcp_enabled=False)['mcp_enabled'])
        self.assertTrue(self.call('lumen_summary')['result']['isError'])
        self.allow()
        self.assertTrue(self.call('lumen_draft_status', draft_id=draft['draft_id'],
                                  status_token=draft['status_token'])['result']['isError'])

    def test_standalone_bridge_cannot_enable_mcp(self):
        denied = self.bridge('settings', mcp_enabled=True, roots=[str(self.root)])
        self.assertFalse(denied['ok'])
        self.assertTrue(self.call('lumen_summary')['result']['isError'])

    def test_standalone_bridge_denies_native_mcp_controls(self):
        self.allow()
        draft = self.draft(self.candidate)
        for command, args in (
            ('mcp_pending', {}),
            ('mcp_approve', {'draft_id': draft['draft_id'], 'paths': [str(self.candidate)]}),
            ('mcp_decline', {'draft_id': draft['draft_id']}),
            ('settings', {'mcp_enabled': False}),
        ):
            with self.subTest(command=command):
                response = self.bridge(command, **args)
                self.assertFalse(response['ok'])
                self.assertNotIn(self.candidate.name, json.dumps(response))
        self.assertTrue(self.candidate.exists())
        self.assertEqual(self.status(draft)['status'], 'pending')

    def test_changed_identity_and_new_project_reference_prevent_quarantine(self):
        self.allow()
        first = self.draft(self.candidate)
        self.candidate.write_bytes(b'replacement data')
        with self.assertRaises(ReviewError):
            self.native('mcp_approve', draft_id=first['draft_id'], paths=[str(self.candidate)])
        self.assertTrue(self.bridge('scan')['ok'])
        self.assertEqual(self.status(first)['status'], 'stale')
        second = self.draft(self.candidate, self.spare)
        (self.root / 'index.html').write_text(f'<a href="{self.candidate.name}">download</a>')
        self.native('mcp_approve', draft_id=second['draft_id'], paths=[str(self.candidate), str(self.spare)])
        self.assertTrue(self.candidate.exists())
        self.assertTrue(self.spare.exists())
        status = self.status(second)
        self.assertEqual((status['quarantined'], status['skipped']), (0, 2))
        self.assertNotIn('batch_id', status)

    def test_native_startup_keeps_the_snapshot_of_a_pending_draft(self):
        self.assertTrue(self.bridge('scan_local', startup=True)['ok'])
        self.allow()
        original_scan = self.bridge('status')['data']['scan_id']
        draft = self.draft(self.candidate)
        startup = self.bridge('scan_local', startup=True)
        self.assertTrue(startup['ok'], startup)
        self.assertEqual(startup['data']['scan_id'], original_scan)
        pending, = self.native('mcp_pending')['drafts']
        self.assertEqual(pending['draft_id'], draft['draft_id'])
        self.assertEqual(self.status(draft)['status'], 'pending')
        manual = self.bridge('scan_local')
        self.assertTrue(manual['ok'], manual)
        self.assertNotEqual(manual['data']['scan_id'], original_scan)
        self.assertEqual(self.status(draft)['status'], 'stale')


if __name__ == '__main__':
    unittest.main()
