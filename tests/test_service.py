import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_agent_observations_are_deduplicated_without_finishing_the_task(self):
        root = str(self.root / 'Projects/Trailer')
        self.scan()
        output = self.root / 'Projects/Trailer/preview/temporary.txt'
        output.write_text('registered temporary')
        task = dict(id='codex-session:session-1', agent='Codex', root=root, temporary=[str(output)])
        self.cmd('task', **task)
        self.cmd('settings', automatic_rules=['task.declared-temporary.quarantine.v1'])
        event = dict(hook_event_name='Stop', session_id='session-1', cwd=root, turn_id='turn-1',
                     last_assistant_message='private output', transcript_path=str(self.root / 'private'))
        self.assertFalse(self.cmd('agent_event', agent='Codex', event=event)['duplicate'])
        self.assertTrue(self.cmd('agent_event', agent='Codex', event=event)['duplicate'])
        self.assertFalse(self.cmd('agent_event', agent='Codex', event={**event, 'hook_event_name': 'SessionEnd',
                                                                      'reason': 'other'})['duplicate'])
        dashboard = self.cmd('status')
        self.assertEqual([item['stage'] for item in dashboard['agent_observations']],
                         ['session_ended', 'turn_finished'])
        self.assertFalse(dashboard['tasks'][0]['closed'])
        self.assertTrue(output.exists())
        self.assertNotIn('private output', (self.state / 'workspace.json').read_text())
        script = Path(__file__).resolve().parents[1] / 'engine/agent_event.py'
        hook = subprocess.run([sys.executable, '-I', '-B', str(script), 'Codex'],
                              input=json.dumps(event), env={**os.environ, 'LUMEN_STATE_DIR': str(self.state)},
                              capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(hook.stdout), {})
        self.assertEqual(hook.stderr, '')
        self.assertEqual(self.cmd('settings', roots=[])['agent_observations'], [])

    def test_closeout_tracks_task_batch_and_later_recovery(self):
        self.scan()
        root = self.root / 'Projects/Trailer'
        source = root / 'media/theme.wav'
        cache = root / '.cache/transcode.dat'
        cache.parent.mkdir(exist_ok=True)
        cache.write_text('temporary content')
        task = dict(id='edit-1', agent='Codex', root=str(root),
                    inputs=[str(source)], temporary=[str(cache)])
        self.cmd('task', **task)
        self.cmd('settings', automatic_rules=['task.declared-temporary.quarantine.v1'])
        dashboard = self.cmd('task', **task, closed=True)
        summary, = dashboard['task_closeouts']
        item = next(row for row in summary['items'] if row['path'] == str(cache))
        self.assertEqual(item['actions'][0]['status'], 'quarantined')
        self.assertTrue(any(row['path'] == str(source) and row['refs'] for row in summary['items']))
        batch, = summary['batch_ids']
        self.cmd('restore', batch_id=batch)
        summary, = self.cmd('status')['task_closeouts']
        item = next(row for row in summary['items'] if row['path'] == str(cache))
        self.assertEqual(item['actions'][0]['status'], 'restored')

    def test_interrupted_closeout_recovers_committed_batch_link_without_repeating_the_move(self):
        self.scan()
        root = self.root / 'Projects/Trailer'
        draft = root / 'preview/temp.txt'
        draft.write_text('temporary content')
        task = dict(id='interrupted', agent='Codex', root=str(root), temporary=[str(draft)])
        self.cmd('task', **task)
        self.cmd('settings', automatic_rules=['task.declared-temporary.quarantine.v1'])
        with patch('engine.lumen.service.measure_batch', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.cmd('task', **task, closed=True)
        summary, = self.cmd('status')['task_closeouts']
        item, = summary['items']
        self.assertEqual(item['actions'][0]['status'], 'quarantined')
        self.assertFalse(item['indexed'])
        self.assertTrue(self.cmd('task', **task, closed=True)['duplicate'])
        self.assertEqual(self.cmd('status')['task_closeouts'][0]['batch_ids'], summary['batch_ids'])

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

    def test_declared_temporary_hook_only_quarantines_registered_unprotected_files(self):
        rule = 'task.declared-temporary.quarantine.v1'
        project = self.root / 'Projects/Trailer'
        names = ('available', 'kept', 'input', 'output', 'in-progress', 'busy', 'replaced', 'modified',
                 'link-target', 'weight.safetensors', 'referenced', 'same-a', 'same-b', 'late')
        paths = {name:project/name for name in names}
        for name, path in paths.items():
            if name != 'late':
                path.write_text('temporary '+name)
        paths['same-b'].write_bytes(paths['same-a'].read_bytes())
        symlink = project/'pointer'
        symlink.symlink_to(paths['link-target'])
        outside = self.root/'Downloads/draft-a.zip'
        (project/'consumer.html').write_text('<a href="referenced">input</a>')
        self.scan()
        self.cmd('settings', automatic_rules=[rule], keep=[str(paths['kept'])])
        declared = [str(p) for p in paths.values()] + [str(symlink), str(outside)]
        self.cmd('task', id='claude-session:finish', root=str(project), agent='Claude Code',
                 temporary=declared, inputs=[str(paths['input'])], outputs=[str(paths['output'])])
        self.cmd('task', id='still-running', root=str(project), agent='Codex',temporary=[str(paths['in-progress'])])
        paths['replaced'].unlink()
        paths['replaced'].write_text('temporary replaced')
        s = paths['modified'].stat()
        paths['modified'].write_bytes(b'x'*s.st_size)
        os.utime(paths['modified'], ns=(s.st_atime_ns,s.st_mtime_ns))
        paths['late'].write_text('new since registration')
        event = dict(hook_event_name='SessionEnd',session_id='finish',cwd=str(project))
        with paths['busy'].open('rb'):
            result = self.cmd('session_end',event=event)
        batch = result['last_actions'][0]
        self.assertEqual(batch['action'], 'quarantine')
        self.assertEqual([i['path'] for i in batch['items'] if i['status']=='quarantined'], [str(paths['available'])])
        self.assertTrue(all(p.exists() for name,p in paths.items() if name!='available'))
        self.assertTrue(outside.is_file() and symlink.is_symlink())
        self.cmd('restore',batch_id=batch['id'])
        self.assertEqual(paths['available'].read_text(),'temporary available')
        self.assertTrue(self.cmd('session_end',event=event)['duplicate'])
        self.assertTrue(paths['available'].exists())

    def test_two_automatic_rules_never_delete_declared_assets_or_whole_duplicate_groups(self):
        rules = ['fixture.rotated-log.v1','task.declared-temporary.quarantine.v1']
        self.scan()
        self.cmd('settings',automatic_rules=rules)
        log = self.root/'Library/Logs/com.lumen.fixture/expired-1.log'
        duplicate = self.root/'copy.bin'
        duplicate.write_bytes(log.read_bytes())
        self.cmd('task',root=str(self.root),id='both',agent='Codex',temporary=[str(duplicate)])
        result = self.cmd('task',root=str(self.root),id='both',agent='Codex',closed=True)
        self.assertTrue(log.exists() and duplicate.exists())
        self.assertEqual({i['status'] for b in result['last_actions'] for i in b['items']},{'skipped'})
        self.cmd('task',root=str(self.root),id='log-is-temporary',agent='Codex',temporary=[str(log)])
        result = self.cmd('task',root=str(self.root),id='log-is-temporary',agent='Codex',closed=True)
        self.assertEqual([b['action'] for b in result['last_actions']],['quarantine'])
        self.assertEqual(result['last_action']['items'][0]['status'],'quarantined')

    def test_each_automatic_batch_keeps_its_own_volume_sample(self):
        from unittest.mock import patch
        path = self.root/'temp-unique'
        path.write_text('chosen once')
        self.scan()
        self.cmd('settings',automatic_rules=['fixture.rotated-log.v1','task.declared-temporary.quarantine.v1'])
        self.cmd('task',root=str(self.root),id='metrics',agent='Codex',temporary=[str(path)])
        measurements = [{9:10000}, {9:9800}, {9:9800}, {}]
        with patch('engine.lumen.service.volume_free',side_effect=measurements):
            result = self.cmd('task',root=str(self.root),id='metrics',agent='Codex',closed=True)
        quarantined, cleaned = result['last_actions']
        self.assertEqual((quarantined['action'],quarantined['measured_free_delta']),('quarantine',-200))
        self.assertEqual((cleaned['action'],cleaned['measured_free_delta']),('clean',None))
        history = {batch['id']:batch for batch in self.cmd('status')['history']}
        self.assertEqual(history[quarantined['id']]['measured_free_delta'],-200)
        self.assertIsNone(history[cleaned['id']]['measured_free_delta'])

    def test_automatic_quarantine_rechecks_ctime_after_busy_probe(self):
        from unittest.mock import patch
        from engine.lumen import actions
        project = self.root/'Projects/Trailer'
        target = project/'working.temp'
        target.write_text('same size')
        self.scan()
        self.cmd('settings',automatic_rules=['task.declared-temporary.quarantine.v1'])
        self.cmd('task',id='race',root=str(project),agent='Codex',temporary=[str(target)])
        original = actions._busy
        def writer(path):
            original(path)
            self.assertEqual(path,target)
            before = target.stat()
            target.write_text('rewritten')
            os.utime(target, ns=(before.st_atime_ns,before.st_mtime_ns))
        with patch.object(actions,'_busy',side_effect=writer):
            result = self.cmd('task',id='race',root=str(project),agent='Codex',closed=True)
        self.assertEqual(result['last_action']['items'][0]['status'],'skipped')
        self.assertEqual(target.read_text(),'rewritten')

    def test_manual_finish_old_snapshots_renewal_and_default(self):
        project = self.root/'Projects/Trailer'
        item = project/'temp-artifact'
        item.write_text('recoverable')
        self.scan()
        def register(identifier):
            return self.cmd('task',id=identifier,root=str(project),agent='Codex',temporary=[str(item)])
        def close(identifier):
            return self.cmd('task',id=identifier,root=str(project),agent='Codex',closed=True)
        register('off')
        close('off')
        self.assertTrue(item.is_file())
        self.cmd('settings',automatic_rules=['task.declared-temporary.quarantine.v1'])
        register('renewed')
        self.cmd('settings',roots=[str(self.root)],renew_roots=[str(self.root)])
        register('renewed')
        close('renewed')
        self.assertTrue(item.is_file())
        register('legacy')
        data = json.loads((self.state/'workspace.json').read_text())
        next(t for t in data['tasks'] if t['id']=='legacy').pop('temporary_snapshots')
        (self.state/'workspace.json').write_text(json.dumps(data))
        close('legacy')
        self.assertTrue(item.is_file())
        register('manual')
        result = close('manual')
        self.assertEqual(result['last_action']['items'][0]['status'],'quarantined')
        self.assertTrue(close('manual')['duplicate'])
        self.assertFalse(item.exists())

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
