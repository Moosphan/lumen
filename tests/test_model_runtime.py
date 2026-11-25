import hashlib
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from engine.lumen import model_runtime
from engine.lumen.model_inventory import summarize
from engine.lumen.scan import scan


def _ollama(model_id, name='gemma4', version='latest', digest='a' * 64):
    return dict(id=model_id, tool='Ollama', name=name, version=version,
                digest='sha256:' + digest if digest else '')


def _lmstudio(model_id, name='org/model-GGUF', version='model-Q4_K_M.gguf', quantization='Q4_K_M'):
    return dict(id=model_id, tool='LM Studio', name=name, version=version,
                quantization=quantization)


def _lmstudio_row(instances, **changes):
    return dict(dict(key='model', publisher='org', format='gguf',
                     quantization={'name': 'Q4_K_M'}, loaded_instances=instances), **changes)


class ModelRuntimeTests(unittest.TestCase):
    def check(self, models, tool, rows=None, *, response=None, connection_error=None):
        if response is None:
            body = json.dumps({'models': rows}).encode('utf-8')
            response = (b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: %d\r\n\r\n'
                        % len(body)) + body
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(response)
        with patch('socket.create_connection', return_value=connection, side_effect=connection_error) as connect:
            result = model_runtime.check(models, tool)
        return result, connect, connection

    def assert_states(self, result, expected, *, status='ok'):
        self.assertEqual(result['status'], status)
        self.assertEqual({key: row['active_state'] for key, row in result['models'].items()}, expected)

    def test_ollama_matches_full_manifest_identity_and_preserves_alias_uncertainty(self):
        models = [_ollama('current'), _ollama('alias', version='copy'),
                  _ollama('changed', digest='b' * 64), _ollama('unverified', digest=''),
                  _ollama('unloaded', name='other', digest='c' * 64), _lmstudio('lm')]
        response = [{'name': 'registry.ollama.ai/library/Gemma4', 'model': 'gemma4:latest', 'digest': 'a' * 64}]
        result, connect, connection = self.check(models, 'ollama', response)
        self.assert_states(result, {'current': 'loaded', 'alias': 'unknown', 'changed': 'unknown',
                                    'unverified': 'unknown', 'unloaded': 'not_loaded'})
        self.assertEqual(result['models']['current']['active_source'], 'ollama:/api/ps')
        self.assertEqual(connect.call_args.args[0], ('127.0.0.1', 11434))
        self.assertTrue(connection.sendall.call_args.args[0].startswith(b'GET /api/ps HTTP/1.1\r\n'))

    def test_ollama_duplicate_inventory_matches_cannot_identify_the_store(self):
        models = [_ollama('first'), _ollama('second')]
        result, _, _ = self.check(models, 'ollama', [{'model': 'gemma4', 'digest': 'sha256:' + 'a' * 64}])
        self.assert_states(result, {'first': 'unknown', 'second': 'unknown'})

    def test_ollama_empty_running_list_reports_no_loaded_manifest(self):
        result, _, _ = self.check([_ollama('present'), _ollama('unverified', digest='')], 'ollama', [])
        self.assert_states(result, {'present': 'not_loaded', 'unverified': 'unknown'})

    def test_ollama_matches_a_verified_runnable_child_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            root = home / '.ollama/models'
            blobs = root / 'blobs'
            blobs.mkdir(parents=True)
            config = b'{"file_type":"Q4_K_M"}'
            config_digest = hashlib.sha256(config).hexdigest()
            (blobs / ('sha256-' + config_digest)).write_bytes(config)
            leaf = json.dumps({'config': {'digest': 'sha256:' + config_digest}, 'layers': []}).encode()
            child = hashlib.sha256(leaf).hexdigest()
            blob = blobs / ('sha256-' + child)
            blob.write_bytes(leaf)
            manifest = root / 'manifests-v2/ollama.com/library/gemma4/latest'
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({'manifests': [{'digest': 'sha256:' + child}]}))
            fallback = root / 'manifests/registry.ollama.ai/library/gemma4/latest'
            fallback.parent.mkdir(parents=True)
            fallback.write_bytes(leaf + b' ')
            fallback_digest = hashlib.sha256(fallback.read_bytes()).hexdigest()

            models = summarize(scan([root], deep=False), home)['models']
            model_id = models[0]['id']
            result, _, _ = self.check(models, 'ollama', [{'model': 'gemma4', 'digest': child}])
            self.assert_states(result, {model_id: 'loaded'})

            result, _, _ = self.check(models, 'ollama', [{'model': 'gemma4', 'digest': fallback_digest}])
            self.assert_states(result, {model_id: 'unknown'})

            blob.write_bytes(leaf + b' ')
            models = summarize(scan([root], deep=False), home)['models']
            result, _, _ = self.check(models, 'ollama', [{'model': 'gemma4', 'digest': child}])
            self.assert_states(result, {model_id: 'unknown'})

    def test_ollama_does_not_match_other_registry(self):
        models = [_ollama('public'), _ollama('private', name='registry.example:5000/library/gemma4')]
        rows = [{'model': 'registry.example:5000/library/gemma4:latest', 'digest': 'a' * 64}]
        result, _, _ = self.check(models, 'ollama', rows)
        self.assert_states(result, {'public': 'unknown', 'private': 'loaded'})

    def test_lmstudio_matches_publisher_model_format_quantization_and_loaded_instances(self):
        models = [_lmstudio('studio'), _lmstudio('foreign', name='elsewhere/model-GGUF'),
                  _lmstudio('other-quant', version='model-Q8_0.gguf', quantization='Q8_0')]
        row = _lmstudio_row([{'id': 'custom-instance'}])
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://proxy.invalid', 'LM_API_TOKEN': 'unused'}):
            result, connect, connection = self.check(models, 'lmstudio', [row])
        self.assert_states(result, {'studio': 'loaded', 'foreign': 'unknown', 'other-quant': 'unknown'})
        self.assertEqual(result['models']['studio']['active_source'], 'lmstudio:/api/v1/models')
        self.assertEqual(connect.call_args.args[0], ('127.0.0.1', 1234))
        request = connection.sendall.call_args.args[0]
        self.assertTrue(request.startswith(b'GET /api/v1/models HTTP/1.1\r\n'))
        self.assertNotIn(b'Authorization:', request)
        self.assertNotIn(b'unused', request)

    def test_lmstudio_matching_empty_instances_confirms_not_loaded(self):
        result, _, _ = self.check([_lmstudio('studio')], 'lmstudio', [_lmstudio_row([], key='org/model')])
        self.assert_states(result, {'studio': 'not_loaded'})

    def test_lmstudio_loaded_multiple_variants_are_ambiguous(self):
        row = _lmstudio_row([{'id': 'org/model'}], variants=['org/model@q4_k_m', 'org/model@q8_0'],
                            selected_variant='org/model@q4_k_m')
        result, _, _ = self.check([_lmstudio('studio')], 'lmstudio', [row])
        self.assert_states(result, {'studio': 'unknown'})

    def test_lmstudio_duplicate_matches_on_either_side_are_ambiguous(self):
        row = _lmstudio_row([{'id': 'org/model'}])
        for models, rows in (([_lmstudio('one'), _lmstudio('two')], [row]),
                             ([_lmstudio('one')], [row, row])):
            with self.subTest(model_count=len(models), row_count=len(rows)):
                result, _, _ = self.check(models, 'lmstudio', rows)
                self.assert_states(result, {model['id']: 'unknown' for model in models})

    def test_invalid_provider_schema_remains_unknown(self):
        cases = ([{'error': 'bad'}], [{'model': 'gemma4', 'digest': 'a' * 10}],
                 [{'model': 'gemma4', 'name': 'other', 'digest': 'a' * 64}], None)
        for rows in cases:
            with self.subTest(rows=rows):
                result, _, _ = self.check([_ollama('present')], 'ollama', rows)
                self.assert_states(result, {'present': 'unknown'}, status='invalid_response')
        row = _lmstudio_row(['custom-instance'])
        result, _, _ = self.check([_lmstudio('studio')], 'lmstudio', [row])
        self.assert_states(result, {'studio': 'unknown'}, status='invalid_response')

    def test_unsupported_tool_skips_connection(self):
        for tool in ('https://localhost:11434', ['ollama'], None, '\ud800'):
            with self.subTest(tool=tool):
                result, connect, _ = self.check([_ollama('present')], tool)
                self.assert_states(result, {}, status='unsupported_tool')
                self.assertIsNone(result['tool'])
                connect.assert_not_called()

    def test_missing_service_becomes_unavailable(self):
        result, _, _ = self.check([_ollama('present')], 'ollama', connection_error=ConnectionRefusedError())
        self.assert_states(result, {'present': 'unknown'}, status='unavailable')
        self.assertIsNone(result['models']['present']['active_source'])

    def test_redirect_is_not_followed(self):
        response = b'HTTP/1.1 302 Found\r\nLocation: http://example.invalid/\r\nContent-Length: 0\r\n\r\n'
        result, connect, _ = self.check([_ollama('present')], 'ollama', response=response)
        self.assert_states(result, {'present': 'unknown'}, status='unavailable')
        connect.assert_called_once()

    def test_oversized_response_is_rejected(self):
        response = b'HTTP/1.1 200 OK\r\nContent-Length: 8388608\r\n\r\n' + b' ' * 2048 + b'{"models":[]}'
        result, _, _ = self.check([_ollama('present')], 'ollama', response=response)
        self.assert_states(result, {'present': 'unknown'}, status='invalid_response')

    def test_total_deadline_interrupts_the_active_socket(self):
        with patch.object(model_runtime, 'Timer') as timer:
            watchdog = timer.return_value
            watchdog.start.side_effect = lambda: timer.call_args.args[1]()
            result, _, connection = self.check([_ollama('present')], 'ollama', [])
        self.assert_states(result, {'present': 'unknown'}, status='unavailable')
        connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        watchdog.cancel.assert_called_once_with()
        self.assertLess(timer.call_args.args[0], 3)
