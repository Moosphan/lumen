import concurrent.futures
import os
import plistlib
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine.lumen import actions


class ActionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / 'work'
        self.root.mkdir()
        self.state = self.base / 'state'
        self.trash = self.base / 'fake-trash'
        self.trash.mkdir(mode=0o700)
        fixture = patch.object(actions, '_trash_directory', return_value=self.trash)
        fixture.start()
        self.addCleanup(fixture.stop)

    def file(self, name='clip.mov', decision='review'):
        p = self.root / name
        p.parent.mkdir(exist_ok=True, parents=True)
        p.write_bytes(b'original')
        s = p.stat()
        return p, dict(path=str(p), size=s.st_size, dev=s.st_dev, ino=s.st_ino,
                       mtime_ns=s.st_mtime_ns, decision=decision)

    def run_action(self, rec, action='quarantine'):
        return actions.execute(self.state, [self.root], [rec], action)

    def test_restore_collision_and_retry(self):
        p, rec = self.file()
        result = self.run_action(rec)
        self.assertEqual(result['items'][0]['status'], 'quarantined')
        p.write_text('new')
        actions.restore(self.state, [self.root], result['batch_id'])
        self.assertEqual(p.read_text(), 'new')
        p.unlink()
        restored = actions.restore(self.state, [self.root], result['batch_id'])
        self.assertEqual(restored['items'][0]['status'], 'restored')
        self.assertEqual(p.read_bytes(), b'original')

    def test_explicit_restore_to_adjacent_name(self):
        p, record = self.file()
        batch = self.run_action(record)
        p.write_text('other')
        item = actions.restore(self.state, [self.root], batch['id'], beside=True)['items'][0]
        self.assertEqual(item['status'], 'restored')
        self.assertEqual(Path(item['restored_to']).name, 'clip (Lumen restored).mov')
        self.assertEqual(Path(item['restored_to']).read_bytes(), b'original')
        self.assertEqual(p.read_text(), 'other')

    def test_trash_purge_authorization(self):
        p, rec = self.file()
        batch = self.run_action(rec, 'trash')
        self.assertEqual(batch['items'][0]['status'], 'trashed')
        unrelated = self.trash / 'other'
        unrelated.touch()
        actions.purge(self.state, [], batch['batch_id'])
        self.assertEqual(len(list(self.trash.iterdir())), 2)
        self.assertEqual(actions.purge(self.state, [self.root], batch['batch_id'])['items'][0]['status'], 'purged')
        self.assertTrue(unrelated.exists())

    def test_clean_revalidates(self):
        p, rec = self.file()
        self.assertEqual(self.run_action(rec, 'clean')['items'][0]['status'], 'skipped')
        rec['decision'] = 'safe'
        p.write_bytes(b'changed value')
        self.assertEqual(self.run_action(rec, 'clean')['items'][0]['status'], 'skipped')
        _, fresh = self.file(decision='safe')
        self.assertEqual(self.run_action(fresh, 'clean')['items'][0]['status'], 'cleaned')
        self.assertFalse(p.exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS disk image registry')
    def test_mounted_image_and_disconnected_volume(self):
        image, record = self.file('installer.dmg')
        mounts = plistlib.dumps({'images': [{'image-path': str(image)}]})
        result = subprocess.CompletedProcess([], 0, stdout=mounts)
        with patch.object(actions.subprocess, 'run', return_value=result):
            self.assertEqual(self.run_action(record, 'trash')['items'][0]['status'], 'skipped')
        self.assertTrue(image.exists())
        p, record = self.file()
        batch = self.run_action(record)
        offline = self.root.with_name('detached')
        self.root.rename(offline)
        pending = actions.restore(self.state, [self.root], batch['id'])
        self.assertEqual(pending['items'][0]['status'], 'quarantined')
        offline.rename(self.root)
        for _ in range(2):
            self.assertEqual(actions.restore(self.state, [self.root], batch['id'])['items'][0]['status'], 'restored')
        self.assertTrue(p.exists())

    def test_protect_symlinks_bundles_and_open_handles(self):
        p, rec = self.file('private/data')
        alias = self.root / 'alias'
        alias.symlink_to(p.parent)
        with p.open('rb'):
            self.assertEqual(self.run_action(rec)['items'][0]['status'], 'skipped')
        for path in (alias / 'data', self.file('Editor.app/Contents/x')[0]):
            stat = path.stat()
            spoof = {**rec, 'path':str(path), 'ino':stat.st_ino, 'mtime_ns':stat.st_mtime_ns}
            self.assertEqual(self.run_action(spoof)['items'][0]['status'], 'skipped')
        self.assertTrue(p.exists())

    def test_concurrent_batches_and_interrupted_journal(self):
        p, rec = self.file()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.run_action(rec), range(2)))
        self.assertCountEqual([r['items'][0]['status'] for r in results], ['skipped', 'quarantined'])
        _, rec = self.file('other')
        original = actions._rename
        def interrupt(*args):
            original(*args)
            raise KeyboardInterrupt()
        with patch.object(actions, '_rename', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.run_action(rec)
        latest = actions.history(self.state)[0]
        self.assertEqual(latest['items'][0]['status'], 'quarantined')
        actions.restore(self.state, [self.root], latest['batch_id'])
        self.assertEqual(Path(rec['path']).read_bytes(), b'original')
