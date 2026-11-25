import hashlib
import http.client
import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from engine.lumen import model_inventory, model_recovery
from engine.lumen.scan import scan


REVISION = 'a' * 40
CONTENTS = b'synthetic weights' * 512
DIGEST = hashlib.sha256(CONTENTS).hexdigest()
FILENAME = 'shards/weights.safetensors'


class Response(io.BytesIO):
    def __init__(self, body=b'', *, status=200, **headers):
        super().__init__(body)
        self.status = status
        self.headers = headers

    def getheader(self, name, default=None):
        return self.headers.get(name.lower().replace('-', '_'), default)


class ModelRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.cache = self.root / 'hub'
        self.repo = self.cache / 'models--org--repo'
        self.blob = self.repo / 'blobs' / DIGEST
        self.blob.parent.mkdir(parents=True)
        self.blob.write_bytes(CONTENTS)
        self.snapshot = self.repo / 'snapshots' / REVISION / FILENAME
        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.symlink_to(os.path.relpath(self.blob, self.snapshot.parent))
        self.destination = self.root / 'exports'
        self.destination.mkdir()

    def describe(self, roots=None):
        report = scan(roots or [self.cache], deep=False)
        models = model_inventory.summarize(report, self.root)['models']
        model = next(model for model in models if model['tool'] == model_inventory.HUB
                     and model['version'] == REVISION)
        return model_recovery.describe(model, report)

    def response(self, body=CONTENTS, **kwargs):
        headers = {'x_repo_commit': REVISION, 'x_linked_etag': f'"{DIGEST}"',
                   'x_linked_size': str(len(CONTENTS)), 'content_length': str(len(body))}
        headers.update(kwargs)
        return Response(body, **headers)

    def test_publish_checks_final_file_identity_without_removing_an_unrelated_replacement(self):
        original = os.link

        def replace(source, name, **kwargs):
            original(source, name, **kwargs)
            parent = kwargs['dst_dir_fd']
            os.unlink(name, dir_fd=parent)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(b'other process replacement')

        with patch('engine.lumen.model_recovery.os.link', side_effect=replace):
            with self.assertRaisesRegex(model_recovery.RecoveryError, '发布时发生变化'):
                self.recover(iter([self.response()]))
        result = self.destination / Path(FILENAME).name
        self.assertEqual(result.read_bytes(), b'other process replacement')
        self.assertEqual(list(self.destination.iterdir()), [result])

    def recover(self, responses, destination=None, *, proof=None, **kwargs):
        if proof is None:
            proof, = self.describe()
        hosts = []
        requests = []

        class Connection:
            def __init__(self, host, **options):
                hosts.append(host)

            def request(self, method, path, headers):
                requests.append((method, path, headers))

            def getresponse(self):
                response = next(responses)
                if isinstance(response, Exception):
                    raise response
                return response

            def close(self):
                pass

        with patch.object(http.client, 'HTTPSConnection', Connection):
            result = model_recovery.recover(proof, destination or self.destination, **kwargs)
        return result, hosts, requests

    def test_indexed_snapshot_proves_exact_revision_filename_digest_and_size(self):
        proof, = self.describe()
        self.assertEqual(proof.details(), {'repository': 'org/repo', 'revision': REVISION,
                                            'filename': FILENAME, 'digest': 'sha256:' + DIGEST,
                                            'bytes': len(CONTENTS)})

    def test_missing_blob_scan_coverage_cannot_prove_source(self):
        self.assertEqual(self.describe([self.snapshot.parent]), [])

    def test_readable_snapshot_copy_without_content_address_is_unavailable(self):
        self.snapshot.unlink()
        self.snapshot.write_bytes(CONTENTS)
        self.assertEqual(self.describe(), [])

    def test_legacy_git_sha1_blob_has_no_sha256_recovery_evidence(self):
        legacy = self.blob.with_name(DIGEST[:40])
        self.blob.rename(legacy)
        self.snapshot.unlink()
        self.snapshot.symlink_to(os.path.relpath(legacy, self.snapshot.parent))
        self.assertEqual(self.describe(), [])

    def test_shared_xet_blob_requires_cache_marker_and_verifies_repo_sha256(self):
        xet_id = 'd' * 64
        store = self.cache / 'blobs'
        payload = store / xet_id[:2] / xet_id
        payload.parent.mkdir(parents=True)
        self.blob.rename(payload)
        self.blob.symlink_to(os.path.relpath(payload, self.blob.parent))
        self.assertEqual(self.describe(), [])
        (store / '.huggingface-shared-blobs').write_bytes(b'1\n')
        proof, = self.describe()
        self.assertEqual(proof.details()['digest'], 'sha256:' + DIGEST)
        with self.assertRaises(model_recovery.RecoveryError):
            self.recover(iter([self.response(x_xet_hash='e' * 64)]))
        result, _, _ = self.recover(iter([self.response(x_xet_hash=xet_id)]))
        self.assertEqual(Path(result['path']).read_bytes(), CONTENTS)

    def test_response_at_pinned_source_materializes_verified_bytes(self):
        result, hosts, requests = self.recover(iter([self.response()]))
        target = self.destination / self.snapshot.name
        self.assertEqual(list(self.destination.iterdir()), [target])
        self.assertEqual(target.read_bytes(), CONTENTS)
        self.assertEqual(result['path'], str(target))
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['provider_visibility'], 'unverified')
        self.assertEqual(hosts, ['huggingface.co'])
        self.assertEqual(requests[0][:2], ('GET', f'/org/repo/resolve/{REVISION}/{FILENAME}'))
        self.assertNotIn('authorization', {name.lower() for name in requests[0][2]})

    def test_matching_vendor_redirect_can_supply_bytes(self):
        redirect = self.response(b'', status=302, location='https://cas-bridge.xethub.hf.co/object?token=remote')
        result, hosts, requests = self.recover(iter([redirect, Response(CONTENTS)]))
        self.assertEqual(hosts, ['huggingface.co', 'cas-bridge.xethub.hf.co'])
        self.assertEqual(requests[-1][1], '/object?token=remote')
        self.assertEqual(Path(result['path']).read_bytes(), CONTENTS)

    def test_disallowed_redirect_is_rejected_before_any_followup(self):
        for location in ('https://huggingface.co.evil.example/file', 'http://huggingface.co/file',
                         'https://user@huggingface.co/file', 'https://huggingface.co:444/file',
                         'https://huggingface.co/some\tfile'):
            with self.subTest(location=location), self.assertRaises(model_recovery.RecoveryError):
                self.recover(iter([self.response(b'', status=302, location=location)]))
            self.assertEqual(list(self.destination.iterdir()), [])

    def test_redirect_chain_is_bounded(self):
        redirect = lambda: self.response(b'', status=302, location='https://huggingface.co/next')
        with self.assertRaises(model_recovery.RecoveryError):
            self.recover(redirect() for _ in range(8))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_size_or_digest_mismatch_leaves_no_output(self):
        for body in (b'x' * len(CONTENTS), CONTENTS[:-1], CONTENTS + b'x'):
            with self.subTest(size=len(body)), self.assertRaises(model_recovery.RecoveryError):
                self.recover(iter([self.response(body)]))
            self.assertEqual(list(self.destination.iterdir()), [])

    def test_stream_truncation_and_read_error_leave_no_partial_file(self):
        class InterruptedResponse(Response):
            def read(self, size):
                if self.tell():
                    raise http.client.IncompleteRead(b'')
                return super().read(64)

        truncated = self.response(CONTENTS[:-1], content_length=str(len(CONTENTS)))
        interrupted = InterruptedResponse(CONTENTS, **self.response().headers)
        for response in (truncated, interrupted):
            with self.subTest(response=response), self.assertRaises(model_recovery.RecoveryError):
                self.recover(iter([response]))
            self.assertEqual(list(self.destination.iterdir()), [])

    def test_stale_provider_metadata_cannot_prove_pinned_source(self):
        for key, value in [('x_repo_commit', 'b' * 40), ('x_linked_size', '1'),
                           ('x_linked_etag', '"' + 'f' * 64 + '"')]:
            with self.subTest(header=key), self.assertRaises(model_recovery.RecoveryError):
                self.recover(iter([self.response(**{key: value})]))
            self.assertEqual(list(self.destination.iterdir()), [])

    def test_missing_private_and_offline_source_leave_no_output(self):
        for outcome in (Response(status=404), Response(status=401), OSError('offline')):
            with self.subTest(outcome=outcome), self.assertRaises(model_recovery.RecoveryError):
                self.recover(iter([outcome]))
            self.assertEqual(list(self.destination.iterdir()), [])

    def test_cancel_during_transfer_removes_staging(self):
        class CancelledResponse(Response):
            def read(self, size):
                cancelled.append(True)
                return super().read(size)

        cancelled = []
        response = CancelledResponse(CONTENTS, **self.response().headers)
        with self.assertRaises(model_recovery.RecoveryError):
            self.recover(iter([response]), cancel=lambda: bool(cancelled))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_existing_destination_is_never_overwritten(self):
        target = self.destination / self.snapshot.name
        target.write_bytes(b'original')
        with self.assertRaises(model_recovery.RecoveryError):
            self.recover(iter(()))
        self.assertEqual(list(self.destination.iterdir()), [target])
        self.assertEqual(target.read_bytes(), b'original')

    def test_destination_created_by_another_writer_is_never_overwritten(self):
        target = self.destination / self.snapshot.name

        class ConcurrentResponse(Response):
            def read(self, size):
                target.write_bytes(b'concurrent file')
                return super().read(size)

        with self.assertRaises(model_recovery.RecoveryError):
            self.recover(iter([ConcurrentResponse(CONTENTS, **self.response().headers)]))
        self.assertEqual(list(self.destination.iterdir()), [target])
        self.assertEqual(target.read_bytes(), b'concurrent file')

    def test_symlink_vendor_and_excluded_destinations_are_unavailable(self):
        link = self.root / 'elsewhere'
        link.symlink_to(self.destination, target_is_directory=True)
        for destination, excluded in ((link, ()), (self.cache, ()),
                                      (self.destination, (self.destination,))):
            with self.subTest(destination=destination), self.assertRaises(model_recovery.RecoveryError):
                self.recover(iter(()), destination=destination, excluded_roots=excluded)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_stale_local_source_evidence_blocks_recovery_before_network(self):
        proof, = self.describe()
        self.blob.write_bytes(CONTENTS + b'x')
        with patch.object(http.client, 'HTTPSConnection', side_effect=AssertionError('unexpected network')):
            with self.assertRaises(model_recovery.RecoveryError):
                model_recovery.recover(proof, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_saved_recipe_rematerializes_file_after_cache_bytes_disappear(self):
        proof, = self.describe()
        record = json.loads(json.dumps(model_recovery.save_recipe(proof)))
        self.snapshot.unlink()
        self.blob.unlink()
        saved = model_recovery.read_recipe(record)
        self.assertEqual(saved.details(), proof.details())
        self.assertEqual(saved.cache_root, self.cache)
        result, hosts, requests = self.recover(iter([self.response()]), proof=saved)
        self.assertEqual(hosts, ['huggingface.co'])
        self.assertEqual(requests[0][:2], ('GET', f'/org/repo/resolve/{REVISION}/{FILENAME}'))
        self.assertEqual(Path(result['path']).read_bytes(), CONTENTS)

    def test_saving_requires_fresh_local_evidence(self):
        proof, = self.describe()
        saved = model_recovery.read_recipe(model_recovery.save_recipe(proof))
        self.blob.write_bytes(CONTENTS + b'x')
        for candidate in (proof, saved):
            with self.subTest(candidate=candidate), self.assertRaises(model_recovery.RecoveryError):
                model_recovery.save_recipe(candidate)

    def test_reading_malformed_or_overridden_recipe_is_rejected(self):
        proof, = self.describe()
        record = model_recovery.save_recipe(proof)
        substitutions = {
            'schema_version': (0, True, '1'),
            'repository': ('https://huggingface.co/org/repo', 'org/../repo', 'org/repo/extra', 'org--other/repo'),
            'revision': ('main', 'a' * 39, 'A' * 40, None),
            'filename': ('../weights.gguf', '/weights.gguf', 'a//weights.gguf', 'a/./weights.gguf',
                         'a/../weights.gguf', 'a\\weights.gguf', 'a\x00.gguf', 'README.md'),
            'digest': (DIGEST, 'sha1:' + DIGEST[:40], 'sha256:xx', ['sha256:' + DIGEST]),
            'bytes': (0, -1, 2**63, True, '42', 1.0),
            'cache_root': ('/', 'relative/hub', '/tmp/../cache', '/tmp//cache'),
            'xet_id': ('bad', DIGEST.upper(), []),
        }
        for field, values in substitutions.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(model_recovery.RecoveryError):
                    model_recovery.read_recipe(dict(record, **{field: value}))
        for malformed in (dict(record, url='https://example.com'), {}, None):
            with self.subTest(record=malformed), self.assertRaises(model_recovery.RecoveryError):
                model_recovery.read_recipe(malformed)

    def test_saved_recipe_rechecks_remote_metadata_when_cache_is_missing(self):
        proof, = self.describe()
        saved = model_recovery.read_recipe(model_recovery.save_recipe(proof))
        self.blob.unlink()
        with self.assertRaises(model_recovery.RecoveryError):
            self.recover(iter([self.response(x_repo_commit='b' * 40)]), proof=saved)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_precommit_authorization_can_reject_verified_transfer(self):
        def revoked():
            raise ValueError('authorization revoked')

        with self.assertRaisesRegex(ValueError, 'authorization revoked'):
            self.recover(iter([self.response()]), precommit=revoked)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_precommit_guard_stays_held_through_publication(self):
        target = self.destination / self.snapshot.name

        @contextmanager
        def guard():
            self.assertFalse(target.exists())
            yield
            self.assertEqual(target.read_bytes(), CONTENTS)

        self.recover(iter([self.response()]), precommit=guard)

    def test_moved_destination_loses_permission_to_publish(self):
        original = self.root / 'moved'

        def move():
            self.destination.rename(original)
            self.destination.mkdir()

        with self.assertRaises(model_recovery.RecoveryError):
            self.recover(iter([self.response()]), precommit=move)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(list(original.iterdir()), [])
