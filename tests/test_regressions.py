"""End-to-end regressions through the same dispatch API used by the native UI."""
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.lumen.service import dispatch


class PublicWorkflowRegressions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.state = self.base / 'personal-state'
        self.work = self.base / 'work'
        self.work.mkdir()

    def call(self, command, **args):
        return dispatch(self.state, {'cmd':command, **args})

    def sample(self, state=None):
        root = Path(dispatch(state or self.state, {'cmd':'fixture'})['root'])
        self.addCleanup(shutil.rmtree, root)
        return root

    def test_separate_lab_never_changes_personal_workspace(self):
        asset = self.work / 'must-keep.txt'
        asset.write_text('private synthetic data')
        self.call('scan', roots=[str(self.work)])
        private = self.call('settings', keep=[str(asset)], budget=42)
        lab_state = self.base / 'lab-state'
        root = self.sample(lab_state)
        lab = dispatch(lab_state, {'cmd':'scan', 'roots':[str(root)]})
        self.assertNotIn(str(asset), [f['path'] for f in lab['files']])
        restored = self.call('status')
        for key in ('roots','budget','scan_id','files','keep','history'):
            self.assertEqual(restored[key], private[key])

    def test_scope_withdrawal_scrubs_external_task_inputs_and_rejects_previous_plan(self):
        assets = self.base / 'assets'
        assets.mkdir()
        private = assets / 'external.txt'
        private.write_text('A')
        self.call('scan', roots=[str(self.work), str(assets)])
        old = self.call('task', root=str(self.work), agent='Codex', inputs=[str(private)])
        current = self.call('settings', roots=[str(self.work)])
        self.assertEqual(current['tasks'][0]['inputs'], [])
        with self.assertRaises(ValueError):
            self.call('run', action='quarantine', paths=[str(private)], scan_id=old['scan_id'])
        refused = self.call('run', action='quarantine', paths=[str(private)], scan_id=current['scan_id'])
        self.assertEqual(refused['last_action']['items'][0]['status'], 'skipped')
        self.assertEqual(private.read_text(), 'A')

    def test_quick_preview_cannot_uninstall_app_with_deep_only_external_consumer(self):
        root = self.sample()
        app = root / 'Applications/Lumen Studio.app'
        source = root / 'Library/Application Support/Reader/page.html'
        source.parent.mkdir(parents=True)
        asset = app / 'Contents/Resources/theme.dat'
        source.write_text(f'<a href="{asset.as_uri()}">theme</a>')
        preview = self.call('scan', roots=[str(root)], deep=False)
        with self.assertRaises(ValueError):
            self.call('uninstall', path=str(app), scan_id=preview['scan_id'])
        self.assertTrue(asset.is_file())

    def test_package_self_references_allow_uninstall_preview(self):
        root = self.sample()
        app = root / 'Applications/Lumen Studio.app'
        page = app / 'Contents/Resources/index.html'
        page.write_text('<a href="theme.dat">theme</a>')
        preview = self.call('scan', roots=[str(root)], deep=True)
        row = next(r for r in preview['apps'] if r['path'] == str(app))
        self.assertIsNotNone(row.get('uninstall_snapshot'))

    def test_old_approval_cannot_move_same_spelling_from_replaced_root(self):
        target = self.work / 'draft.bin'
        target.write_bytes(b'OLD')
        preview = self.call('scan', roots=[str(self.work)], deep=False)
        self.work.rename(self.base / 'former-work')
        self.work.mkdir()
        target.write_bytes(b'NEW')
        refused = self.call('run', action='quarantine', paths=[str(target)], scan_id=preview['scan_id'])
        self.assertEqual(refused['last_action']['items'][0]['status'], 'skipped')
        self.assertFalse(refused['files'])
        allowed = self.call('scan', roots=[str(self.work)], renew_roots=[str(self.work)])
        self.assertEqual([r['path'] for r in allowed['files']], [str(target)])
        with self.assertRaises(ValueError):
            self.call('run', action='quarantine', paths=[str(target)], scan_id=preview['scan_id'])
        self.assertEqual(target.read_bytes(), b'NEW')

    def test_trash_purge_preserves_known_external_reference(self):
        root = self.sample()
        trash_asset = root / '.Trash/old-fixture.txt'
        page = root / 'Projects/Website/trash-reference.html'
        page.write_text(f'<a href="{trash_asset.as_uri()}">retained import</a>')
        self.call('scan', roots=[str(root)])
        listed = self.call('trash_list')
        result = self.call('empty_trash', paths=[str(trash_asset)], scan_id=listed['scan_id'])
        self.assertEqual(result['last_action']['items'][0]['status'], 'skipped')
        self.assertTrue(trash_asset.is_file())

    def test_inventory_keeps_unresolved_local_targets_without_reading_them(self):
        asset = self.base / 'outside.txt'
        asset.write_text('outside selected root')
        html = self.work / 'project.html'
        html.write_text(f'<a href="{asset.as_uri()}">outside tree</a>')
        movie = self.work / 'project.fcpxml'
        movie.write_text(f'<fcpxml><resources><asset id="r7" src="{asset.as_uri()}"/></resources></fcpxml>')
        manifest = self.work / 'dist/.vite/manifest.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"src/main.js":{"file":"missing.js"}}')
        data = self.call('scan', roots=[str(self.work)], deep=False)
        self.assertCountEqual(data['static_refs'], [
            {'source':str(html), 'target':str(asset), 'location':'1'},
            {'source':str(movie), 'target':str(asset), 'location':'r7'},
            {'source':str(manifest), 'target':str(self.work/'dist/missing.js'), 'location':'src/main.js'},
        ])
        self.assertNotIn(str(asset), [entry['path'] for entry in data['files']])
