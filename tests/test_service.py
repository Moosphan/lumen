import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from engine.lumen.fixture import create_fixture
from engine.lumen.service import dispatch


class Workflow(unittest.TestCase):
    def setUp(self):
        import shutil
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name).resolve() / 'state'
        self.root = Path(dispatch(self.state, {'cmd':'fixture'})['root'])
        self.addCleanup(shutil.rmtree, self.root)

    def cmd(self, command, **kwargs):
        if command in ('run','uninstall','empty_trash') and 'scan_id' not in kwargs:
            kwargs['scan_id'] = dispatch(self.state, {'cmd':'status'}).get('scan_id')
        return dispatch(self.state, dict(cmd=command, **kwargs))

    def scan(self):
        return self.cmd('scan', roots=[str(self.root)], deep=True)

    def test_independent_process_status_and_untrusted_request(self):
        bridge = Path(__file__).resolve().parents[1] / 'engine/bridge.py'
        env = {**os.environ, 'LUMEN_STATE_DIR': str(self.state)}
        def call(req):
            p = subprocess.run([sys.executable, '-I', '-B', str(bridge)], input=json.dumps(req),
                               capture_output=True, text=True, env=env, check=True)
            return json.loads(p.stdout)
        self.assertEqual(call({'cmd':'status'})['data']['roots'], [])
        self.assertTrue(call({'cmd':'scan','roots':[str(self.root)]})['ok'])
        self.assertTrue(call({'cmd':'status'})['data']['files'])
        self.assertFalse(call({'cmd':'run','action':'exec','paths':[str(self.root)]})['ok'])

    def test_scan_clean_and_preserve_offline_workspace(self):
        dashboard = self.scan()
        self.assertEqual(len(dashboard['apps']), 1)
        trailer = str(self.root / 'Projects/Trailer')
        self.assertCountEqual(dashboard['projects'][trailer], ['Claude Code', 'Codex'])
        clip = self.root / 'Projects/Trailer/media/theme.wav'
        ref = next(r for r in dashboard['files'] if r['path'] == str(clip))
        self.assertEqual(len(ref['refs']), 2)
        log = self.root / 'Library/Logs/com.lumen.fixture/expired-1.log'
        before = {r['path']: Path(r['path']).read_bytes() for r in dashboard['files'] if r['path'] != str(log)}
        result = self.cmd('run', action='clean', paths=[str(log), str(clip)])['last_action']
        self.assertEqual({i['status'] for i in result['items']}, {'cleaned','skipped'})
        self.assertFalse(log.exists())
        self.assertTrue(all(Path(p).read_bytes() == v for p,v in before.items()))

    def test_duplicate_review_and_safe_recovery(self):
        self.scan()
        a = str(self.root / 'Downloads/draft-a.zip')
        b = str(self.root / 'Downloads/draft-b.zip')
        failed = self.cmd('run',action='quarantine',paths=[a,b])['last_action']
        self.assertTrue(all(i['status']=='skipped' for i in failed['items']))
        batch = self.cmd('run',action='quarantine',paths=[a])['last_action']
        self.assertEqual(batch['items'][0]['status'],'quarantined')
        self.cmd('restore', batch_id=batch['id'])
        self.assertEqual(Path(a).read_bytes(),Path(b).read_bytes())

    def test_stale_preview_task_keep_revoke_and_budget(self):
        self.scan()
        draft = self.root / 'Projects/Trailer/preview/rough.mp4'
        draft.write_text('updated since scan')
        skipped = self.cmd('run',action='quarantine',paths=[str(draft)])['last_action']
        self.assertEqual(skipped['items'][0]['status'],'skipped')
        self.cmd('task',root=str(self.root/'Projects/Trailer'),agent='Codex',
                 inputs=[],outputs=[],temporary=[str(draft)],closed=False)
        dashboard = self.cmd('settings',budget=100)
        self.assertTrue(dashboard['alerts'])
        row = next(r for r in dashboard['files'] if r['path']==str(draft))
        self.assertEqual(row['decision'],'keep')
        self.assertEqual(self.cmd('settings',roots=[])['files'],[])
        self.assertEqual(self.cmd('status')['roots'],[])

    def test_uninstall_subtree_hold_and_conflicting_projects(self):
        scan = self.scan()
        app = scan['apps'][0]['path']
        self.assertTrue(scan['apps'][0].get('uninstall_snapshot'))
        self.cmd('settings',exclude=[app+'/Contents/Resources'])
        with self.assertRaises(ValueError):
            self.cmd('uninstall',path=app)
        self.assertTrue(Path(app).exists())
        self.cmd('settings',exclude=[])
        removed = self.cmd('uninstall',path=app)
        self.assertFalse(Path(app).exists())
        batch = removed['last_action']['id']
        self.assertTrue(any(b['id']==batch for b in removed['history']))
        self.cmd('restore',batch_id=batch)
        self.assertTrue(Path(app).is_dir())

    def test_bound_preview_nonce_and_isolated_trash(self):
        lab = Path(self.cmd('fixture')['root'])
        import shutil
        self.addCleanup(shutil.rmtree,lab)
        old = self.cmd('scan',roots=[str(lab)])['scan_id']
        self.cmd('scan',roots=[str(lab)])
        draft = str(lab/'Downloads/draft-a.zip')
        with self.assertRaises(ValueError):
            self.cmd('run',action='trash',paths=[draft],scan_id=old)
        moved = self.cmd('run',action='trash',paths=[draft])
        self.assertEqual(moved['last_action']['items'][0]['status'],'trashed')
        volumes = self.cmd('trash_list')['trash_volumes']
        self.assertEqual(volumes[0]['root'],str(lab/'.Trash'))
        old_fixture = str(lab/'.Trash/old-fixture.txt')
        result = self.cmd('empty_trash',paths=[old_fixture])
        self.assertFalse(Path(old_fixture).exists())
        self.assertEqual(result['history'][0]['action'],'empty_trash')

    def test_session_hook_idempotence_and_incremental_stats(self):
        self.scan()
        event = dict(hook_event_name='SessionEnd',session_id='abc-123',cwd=str(self.root/'Projects/Trailer'))
        first = self.cmd('session_end',event=event)
        self.assertTrue(first['tasks'][-1]['closed'])
        self.assertTrue(self.cmd('session_end',event=event)['duplicate'])
        self.assertGreater(self.scan()['hash_hits'],0)

    def test_fast_preview_still_protects_entire_duplicate_group(self):
        self.cmd('scan',roots=[str(self.root)],deep=False)
        both = [str(self.root / 'Downloads' / n) for n in ('draft-a.zip','draft-b.zip')]
        result = self.cmd('run',action='trash',paths=both)
        self.assertTrue(all(i['status']=='skipped' for i in result['last_action']['items']))
        self.assertTrue(all(Path(p).is_file() for p in both))

    def test_changed_root_requires_explicit_reauthorization(self):
        self.scan()
        import shutil
        swapped = self.root.with_name(self.root.name+'-old')
        self.root.rename(swapped)
        self.addCleanup(shutil.rmtree,swapped)
        self.root.mkdir(mode=0o700)
        (self.root/'new-user-data').write_text('private')
        report = self.scan()
        self.assertFalse(report['files'])
        self.assertTrue(any('身份变化' in issue for issue in report['issues']))
        allowed = self.cmd('scan',roots=[str(self.root)],renew_roots=[str(self.root)])
        self.assertEqual(len(allowed['files']),1)

    def test_event_preserves_registered_inputs_and_outputs(self):
        self.scan()
        root = str(self.root/'Projects/Trailer')
        file = str(self.root/'Downloads/draft-a.zip')
        self.cmd('task', id='claude-session:saved', root=root,agent='Claude Code',
                 inputs=[file],outputs=[],temporary=[],closed=False)
        done = self.cmd('session_end',event={'hook_event_name':'SessionEnd','session_id':'saved','cwd':root})
        self.assertEqual(done['tasks'][-1]['inputs'],[file])
        row = next(r for r in done['files'] if r['path']==file)
        self.assertEqual(row['decision'],'keep')

    def test_session_dirs_can_be_suggested_without_access(self):
        outside = str(self.root / 'unselected-project')
        trace = self.root/'.codex/sessions/2026/11/24/rollout-sample.jsonl'
        trace.write_text(json.dumps({'type':'session_meta','payload':{'cwd':outside}}))
        scanned = self.cmd('scan',roots=[str(self.root/'.codex')])
        self.assertEqual(scanned['suggested_projects'],[outside])
        self.assertNotIn(outside,scanned['roots'])

    def test_manual_task_assigns_assets_outside_project(self):
        self.scan()
        file = str(self.root/'Downloads/draft-a.zip')
        project = str(self.root/'Projects/Trailer')
        report = self.cmd('task',root=project,agent='Codex',inputs=[file])
        row = next(r for r in report['files'] if r['path']==file)
        self.assertIn(project,row['projects'])
        self.assertIn('Codex',row['agents'])
