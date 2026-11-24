import json
import os
import plistlib
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from engine.lumen.scan import scan


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()

    def file(self, name, data=b'x'):
        path = self.home / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(data)
        return path

    def fixture(self, version='1.0'):
        self.file('Applications/LumenFixture.app/Contents/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': 'com.lumen.fixture', 'CFBundleShortVersionString': version,
            'CFBundleName': 'Lumen Fixture',
        }))
        log = self.file('Library/Logs/com.lumen.fixture/expired-1.log')
        old = time.time() - 35 * 86400
        os.utime(log, (old, old))
        return log

    def runscan(self, **kwargs):
        result = scan([self.home], **kwargs)
        return result, {Path(f['path']): f for f in result['files']}

    def test_exact_verified_rule_only(self):
        log = self.fixture()
        other = self.file('Projects/fake/Library/Logs/com.lumen.fixture/expired-1.log')
        _, files = self.runscan()
        self.assertEqual(files[log]['decision'], 'safe')
        self.assertNotEqual(files[other]['decision'], 'safe')
        _, files = self.runscan(keep=[log])
        self.assertEqual(files[log]['decision'], 'keep')
        self.fixture('2.0')
        _, files = self.runscan()
        self.assertNotEqual(files[log]['decision'], 'safe')

    def test_sessions_cross_project_refs_and_links(self):
        media = self.file('Downloads/sound a.wav', b'abc')
        other = self.file('Downloads/other.wav', b'abc')
        for proj, index in [('site', '.codex/sessions/a.jsonl'), ('video', '.claude/projects/b.jsonl')]:
            page = self.file('Projects/' + proj + '/index.html', b'<source src="../../Downloads/sound%20a.wav?q=x">')
            record = {'cwd': str(page.parent)}
            if proj == 'site':
                record = {'type': 'session_meta', 'payload': record}
            self.file(index, json.dumps(record).encode())
        alias = self.home / 'Downloads/hard.wav'
        os.link(other, alias)
        outside = self.home / 'outside-link'
        outside.symlink_to('/does/not/exist')
        result, files = self.runscan()
        self.assertEqual(files[media]['agents'], ['Claude Code', 'Codex'])
        self.assertEqual(len(files[media]['refs']), 2)
        self.assertEqual(files[media]['decision'], 'keep')
        self.assertEqual(files[other]['duplicate_group'], files[media]['duplicate_group'])
        self.assertEqual(files[alias]['decision'], 'keep')
        self.assertEqual(files[outside]['decision'], 'keep')
        self.assertEqual(len(result['projects']), 2)

    def test_git_quick_exclusions_and_unreadable_root(self):
        source = self.file('repo/track.txt')
        subprocess.run(['git', 'init', '-q', str(source.parent)], check=True)
        subprocess.run(['git', '-C', str(source.parent), 'add', 'track.txt'], check=True)
        private = self.file('Library/Containers/com.lumen.fixture/document.txt')
        result, files = self.runscan(deep=False)
        self.assertEqual(files[source]['decision'], 'keep')
        self.assertNotIn(private, files)
        self.assertTrue(result['issues'])
        _, files = self.runscan(exclude=[source.parent])
        self.assertNotIn(source, files)
        self.assertTrue(scan([self.home / 'missing'])['issues'])

    def test_vscode_fixed_release_retention_and_open_files(self):
        from unittest.mock import patch
        plist = self.file('Applications/Visual Studio Code.app/Contents/Info.plist',
                          plistlib.dumps({'CFBundleIdentifier': 'com.microsoft.VSCode',
                                          'CFBundleShortVersionString': '1.95.3'}))
        logs = []
        old = time.time() - 35 * 86400
        for i in range(1, 12):
            log = self.file(f'Library/Application Support/Code/logs/202401{i:02}T120000/main.log')
            os.utime(log, (old, old))
            logs.append(log)
        with patch('engine.lumen.scan._code_stopped', return_value=True):
            _, files = self.runscan()
            self.assertEqual(files[logs[0]]['decision'], 'safe')
            self.assertNotEqual(files[logs[1]]['decision'], 'safe')
            with logs[0].open('rb'):
                _, files = self.runscan()
                self.assertEqual(files[logs[0]]['decision'], 'defer')
            plist.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.microsoft.VSCode',
                                              'CFBundleShortVersionString': '1.95.4'}))
            _, files = self.runscan()
            self.assertNotEqual(files[logs[0]]['decision'], 'safe')

    def test_space_model_links_and_coverage(self):
        blob = self.file('.cache/models--tiny/blobs/12345', b'x' * 8192)
        for rev in ('v1', 'v2'):
            alias = self.home / '.cache/models--tiny/snapshots' / rev / 'weights.safetensors'
            alias.parent.mkdir(parents=True)
            alias.symlink_to('../../blobs/12345')
        self.file('project/index.html', b'<script>new Audio(dynamic)</script><img src="missing.png">')
        result, files = self.runscan()
        self.assertEqual(files[blob]['model_versions'], ['v1', 'v2'])
        self.assertGreaterEqual(result['space']['shared_allocated_bytes'], files[blob]['allocated_bytes'])
        self.assertGreater(result['volumes'][0]['total_bytes'], 0)
        self.assertTrue(any('未覆盖' in i for i in result['issues']))
        self.assertTrue(any('脚本' in i for i in result['issues']))

    def test_inventory_with_ambiguous_apps_or_invalid_metadata(self):
        log = self.fixture()
        info = self.home / 'Applications/LumenFixture.app/Contents/Info.plist'
        self.file('Applications/Extra.app/Contents/Info.plist', info.read_bytes())
        self.file('Applications/Broken.app/Contents/Info.plist', b'<?xml version="1.0"?><plist>')
        result, files = self.runscan()
        self.assertTrue(any('Broken.app' in issue for issue in result['issues']))
        self.assertNotEqual(files[log]['decision'], 'safe')
        self.assertEqual(len(files[log]['shared_owners']), 2)

    def test_ref_and_symlink_block_exact_rule(self):
        log = self.fixture()
        alias = self.home / 'alias.log'
        alias.symlink_to(log)
        _, files = self.runscan()
        self.assertEqual(files[log]['decision'], 'keep')
        alias.unlink()
        ref = self.file('Projects/a/index.html', b'<a href="../../Library/Logs/com.lumen.fixture/expired-1.log"></a>')
        _, files = self.runscan()
        self.assertEqual(files[log]['decision'], 'keep')
        self.assertEqual(files[log]['refs'], [f'{ref}:1'])

    def test_scope_errors_continue(self):
        from unittest.mock import patch
        expected = self.file('Downloads/other.txt')
        denied = self.file('private/data.txt').parent
        walk = os.fwalk
        def with_denial(path, **kwargs):
            if path == denied:
                kwargs['onerror'](PermissionError(13, 'Permission denied', str(denied)))
                return iter(())
            return walk(path, **kwargs)
        with patch('engine.lumen.scan.os.fwalk', side_effect=with_denial):
            result = scan([denied, expected.parent])
        self.assertEqual([f['path'] for f in result['files']], [str(expected)])
        self.assertTrue(any(str(denied) in issue for issue in result['issues']))
