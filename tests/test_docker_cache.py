import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.lumen import service


class DockerCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.home = self.root / 'home'
        self.state = self.root / 'state'

    def call(self, command, **arguments):
        return service.dispatch(self.state, {'cmd': command, **arguments}, local_home=self.home)

    def test_usage_is_explicit_snapshot_bound_and_does_not_change_index(self):
        disk = self.home / 'Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw'
        disk.parent.mkdir(parents=True)
        disk.write_bytes(b'existing virtual disk')
        scan = self.call('scan_local', startup=True)
        group, = scan['developer_caches']
        config = (self.state / 'workspace.json').read_bytes()
        with patch('engine.lumen.docker_cache.check', return_value={'status': 'ok', 'entries': 1}) as check:
            with self.assertRaises(ValueError):
                self.call('docker_cache_usage', scan_id='old', group_id=group['id'])
            with self.assertRaises(ValueError):
                self.call('docker_cache_usage', scan_id=scan['scan_id'], group_id='unknown')
            check.assert_not_called()
            self.assertEqual(self.call('docker_cache_usage', scan_id=scan['scan_id'], group_id=group['id']),
                             {'status': 'ok', 'entries': 1})
            check.assert_called_once_with(self.home)
        self.assertEqual((self.state / 'workspace.json').read_bytes(), config)
        self.assertEqual(disk.read_bytes(), b'existing virtual disk')

    def test_cache_records_distinguish_shared_and_unused_bytes(self):
        from engine.lumen import docker_cache

        records = {'BuildCache': [
            {'ID': 'shared', 'Size': 150, 'InUse': False, 'Shared': True, 'Description': '/private/project'},
            {'ID': 'active', 'Size': 40, 'InUse': True, 'Shared': False},
            {'ID': 'unused', 'Size': 200, 'InUse': False, 'Shared': False},
        ]}
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://outside.invalid:2375'}), \
                patch.object(docker_cache, '_fetch', return_value=records) as fetch:
            summary = docker_cache.check(self.home)
        fetch.assert_called_once_with(self.home / '.docker/run/docker.sock')
        self.assertEqual(summary, {'status': 'ok', 'entries': 3, 'reported_bytes': 390,
                                   'shared_bytes': 150, 'unused_unshared_bytes': 200})

    def test_empty_cache_is_distinct_from_unavailable_or_invalid_usage(self):
        from engine.lumen import docker_cache

        with patch.object(docker_cache, '_fetch', side_effect=FileNotFoundError):
            self.assertEqual(docker_cache.check(self.home), {'status': 'unavailable'})
        record = {'ID': 'cache', 'Size': 2, 'InUse': False, 'Shared': False}
        for invalid in ({}, {'BuildCache': None}, {'BuildCache': [record, record]},
                        {'BuildCache': [dict(record, Size=True)]}, {'BuildCache': [dict(record, Size=-1)]}):
            with self.subTest(invalid=invalid), patch.object(docker_cache, '_fetch', return_value=invalid):
                self.assertEqual(docker_cache.check(self.home), {'status': 'invalid_response'})
        with patch.object(docker_cache, '_fetch', return_value={'BuildCache': []}):
            self.assertEqual(docker_cache.check(self.home)['reported_bytes'], 0)

    def test_connection_rejects_link_or_ordinary_file_without_connecting(self):
        from engine.lumen import docker_cache

        path = self.home / '.docker/run/docker.sock'
        path.parent.mkdir(parents=True)
        path.touch()
        with patch('engine.lumen.docker_cache.socket.socket') as connect:
            self.assertEqual(docker_cache.check(self.home), {'status': 'unavailable'})
            path.unlink()
            path.symlink_to(self.root / 'other.sock')
            self.assertEqual(docker_cache.check(self.home), {'status': 'unavailable'})
            connect.assert_not_called()
