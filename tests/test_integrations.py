import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.lumen import integrations
from engine.lumen.scan import scan


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write(self, path, text):
        full = self.root / path
        full.parent.mkdir(exist_ok=True, parents=True)
        full.write_text(text)
        return full

    def test_session_end_is_stable_and_scoped(self):
        event = dict(hook_event_name='SessionEnd', session_id='abc-123', cwd=str(self.root),
                     transcript_path='/unread/data')
        get = lambda: integrations.claude_session_end(event, [self.root])
        self.assertEqual(get(), get())
        self.assertTrue(get()['closed'])
        self.assertEqual(get()['id'], 'claude-session:abc-123')
        event['cwd'] = str(self.root.parent)
        with self.assertRaises(ValueError):
            get()

    def test_fcpxml_and_vite_references(self):
        song = self.write('shared/theme.wav', 'media')
        script = self.write('site/dist/assets/app.js', 'compiled')
        self.write('edit/export.fcpxml', '<fcpxml version="1.10"><resources><asset id="r1">' +
                   f'<media-rep src="{song.as_uri()}"/></asset></resources></fcpxml>')
        self.write('site/dist/.vite/manifest.json', json.dumps({
            'src/main.js': {'file': 'assets/app.js', 'css': ['assets/missing.css']}}))
        result = scan([self.root], projects={str(self.root/'edit'): [], str(self.root/'site'): []})
        rows = {f['path']:f for f in result['files']}
        self.assertEqual(rows[str(song)]['projects'], [str(self.root/'edit')])
        self.assertEqual(rows[str(script)]['kind'], 'build')
        self.assertEqual(rows[str(script)]['decision'], 'keep')
        self.assertTrue(any('missing.css' in msg for msg in result['issues']))

    def test_untrusted_manifest_and_xml(self):
        self.write('dist/.vite/manifest.json', '{"bad":{"file":"../../escape"}}')
        self.write('edit/evil.fcpxml', '<!DOCTYPE fcpxml [<!ENTITY x "bad">]><fcpxml/>')
        result = scan([self.root])
        self.assertEqual(len(result['issues']), 2)
        self.assertEqual(integrations.automatic_paths(result, integrations.SAFE_RULE_IDS), [])

    def test_hub_refs_and_protected_blobs(self):
        revision = 'a' * 40
        repo = 'hub/models--org--tiny/'
        blob = self.write(repo+'blobs/data', 'weights')
        link = self.root/repo/'snapshots'/revision/'model.gguf'
        link.parent.mkdir(parents=True)
        link.symlink_to('../../blobs/data')
        self.write(repo+'refs/main', revision)
        row = next(r for r in scan([self.root])['files'] if r['path'] == str(blob))
        self.assertEqual(row['model_refs'], ['main'])
        self.assertEqual(row['model_versions'], [revision])
        self.assertEqual(row['decision'], 'keep')

    def test_cache_reuse_invalidates_after_same_size_edit_and_scope_change(self):
        first = self.write('a.dat', 'hello')
        self.write('b.dat', 'hello')
        cache = {}
        initial = scan([self.root], hash_cache=cache)
        self.assertEqual(initial['hash_reads'], 2)
        cache = json.loads(json.dumps(cache))
        with patch('engine.lumen.scan._digest', side_effect=AssertionError('rehash')):
            second = scan([self.root], hash_cache=cache)
            self.assertEqual(second['hash_hits'], 2)
        previous = first.stat()
        first.write_text('world')
        os.utime(first, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        third = scan([self.root], hash_cache=cache)
        self.assertEqual(third['hash_reads'], 1)
        self.assertFalse(third['duplicates'])
        fourth = scan([self.root], hash_cache=cache, exclude=[self.root / 'excluded'])
        self.assertEqual(fourth['hash_reads'], 2)

    def test_automation_requires_active_rule_and_safe_verdict(self):
        good = dict(path='/safe', rule_id=integrations.VSCODE_LOG, decision='safe')
        report = {'files':[good, {**good,'path':'/review','decision':'review'},
                           {**good,'path':'/unknown','rule_id':'fake'}]}
        self.assertEqual(integrations.automatic_paths(report, []), [])
        self.assertEqual(integrations.automatic_paths(report, [integrations.VSCODE_LOG]), ['/safe'])
