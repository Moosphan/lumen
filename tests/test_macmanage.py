import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest

from engine.lumen import macmanage as m


class MacManageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'work'
        self.app = self.root / 'Applications/Player.app'
        self.info = self.app / 'Contents/Info.plist'
        self.info.parent.mkdir(parents=True)
        self.info.write_bytes(plistlib.dumps(dict(CFBundlePackageType='APPL',
            CFBundleIdentifier='com.lumen.fixture', CFBundleShortVersionString='1')))
        self.state = self.base / 'state'
        self.snap = m.app_snapshot(self.app, [self.root])
        self.trash = self.root / '.Trash'
        self.trash.mkdir(mode=0o700)

    def take(self):
        return m.uninstall(self.state, [self.root], self.snap, [self.root])

    def test_restore_conflict_and_app_purge(self):
        b = self.take()['id']
        self.assertFalse(self.app.exists())
        self.app.mkdir()
        self.assertEqual(m.restore(self.state, [self.root], b)['items'][0]['status'], 'quarantined')
        self.app.rmdir()
        self.assertEqual(m.restore(self.state, [self.root], b)['items'][0]['status'], 'restored')
        b = self.take()['id']
        self.assertEqual(m.purge(self.state, [self.root], b)['items'][0]['status'], 'purged')
        self.assertEqual(len(m.history(self.state)), 2)

    def test_side_restore_and_verified_residuals(self):
        log = self.root / 'Library/Logs/com.lumen.fixture/expired.log'
        log.parent.mkdir(parents=True)
        log.write_text('log')
        row = dict(path=str(log), size=3, nlink=1, decision='review')
        report = dict(files=[row], apps=[])
        check = lambda: m.residuals(self.state, [self.root], report, [self.root])
        self.assertFalse(check())
        b = self.take()['id']
        self.assertEqual(check()[0]['path'], str(log))
        report['apps'] = [self.snap]
        self.assertFalse(check())
        report['apps'] = []
        self.app.mkdir()
        self.assertFalse(check())
        item = m.restore(self.state, [self.root], b, beside=True)['items'][0]
        self.assertEqual(Path(item['restored_to']).name, 'Player (Lumen restored).app')
        self.assertTrue(self.app.is_dir())

    def test_busy_stale_unknown_and_links(self):
        with self.info.open('rb'), self.assertRaises(ValueError):
            self.take()
        (self.app / 'Contents/new').touch()
        with self.assertRaises(ValueError):
            self.take()
        self.info.write_bytes(self.info.read_bytes().replace(b'com.lumen.fixture', b'com.apple.Example'))
        with self.assertRaises(ValueError):
            m.app_snapshot(self.app, [self.root])
        os.link(self.info, self.base / 'shared')
        with self.assertRaises(ValueError):
            m.app_snapshot(self.app, [self.root])

    def test_unregistered_fixture_and_unsigned_production_bundle(self):
        with self.assertRaisesRegex(ValueError, '未在此工作区登记'):
            m.app_snapshot(self.app)
        self.info.write_bytes(self.info.read_bytes().replace(b'com.lumen.fixture', b'com.microsoft.VSCode'))
        with self.assertRaisesRegex(ValueError, '签名'):
            m.app_snapshot(self.app, [self.root])

    def test_selected_volume_and_disconnected_trash(self):
        attached = self.base / 'Volume'
        external = attached / '.Trash'
        external.mkdir(mode=0o700, parents=True)
        (external / 'archive').touch()
        untouched = self.trash / 'local'
        untouched.touch()
        roots = [self.trash, external]
        preview = m.trash_inventory(roots, roots)[1]['items']
        attached.rename(self.base / 'offline')
        self.assertEqual(m.trash_inventory(roots, roots)[1]['status'], 'offline')
        self.assertEqual(m.empty_trash(roots, preview, roots)['items'][0]['status'], 'skipped')
        (self.base / 'offline').rename(attached)
        self.assertEqual(m.empty_trash(roots, preview, roots)['items'][0]['status'], 'purged')
        self.assertTrue(untouched.exists())

    def test_trash_scoped_preview_and_folder_contents(self):
        outside = self.base / 'outside'
        outside.write_text('keep')
        folder = self.trash / 'folder'
        folder.mkdir()
        (folder / 'link').symlink_to(outside)
        (folder / 'text').touch()
        roots = [self.trash]
        self.assertIn('error', m.trash_inventory(roots)[0])
        preview = m.trash_inventory(roots, roots)[0]['items']
        self.assertEqual(m.empty_trash([], preview, roots)['items'][0]['status'], 'skipped')
        (folder / 'new').touch()
        self.assertEqual(m.empty_trash(roots, preview, roots)['items'][0]['status'], 'skipped')
        fresh = m.trash_inventory(roots, roots)[0]['items']
        self.assertEqual(m.empty_trash(roots, fresh, roots)['items'][0]['status'], 'purged')
        self.assertTrue(outside.exists())
        self.assertFalse(folder.exists())


@unittest.skipUnless(os.environ.get('LUMEN_VSCODE_SIGNED_ZIP'), 'requires the official VS Code 1.95.3 macOS ZIP')
class SignedReleaseTests(unittest.TestCase):
    def test_official_bundle_uninstall_and_restore(self):
        archive = Path(os.environ['LUMEN_VSCODE_SIGNED_ZIP']).resolve(strict=True)
        digest = hashlib.sha256()
        with archive.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        self.assertEqual(digest.hexdigest(), 'fe01f564777afb75703e748ada00f1204f0495be54947d6ffb513e5a5eb16a08')
        with tempfile.TemporaryDirectory(prefix='lumen-official-app-') as private:
            root = Path(private).resolve()
            apps = root / 'Applications'
            apps.mkdir()
            subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(apps)], check=True)
            app = apps / 'Visual Studio Code.app'
            snapshot = m.app_snapshot(app)
            self.assertEqual((snapshot['bundle_id'], snapshot['version']), ('com.microsoft.VSCode', '1.95.3'))
            batch = m.uninstall(root / 'state', [apps], snapshot)
            self.assertEqual(batch['items'][0]['status'], 'quarantined')
            self.assertFalse(app.exists())
            restored = m.restore(root / 'state', [apps], batch['id'])
            self.assertEqual(restored['items'][0]['status'], 'restored')
            self.assertEqual(m.app_snapshot(app), snapshot)
