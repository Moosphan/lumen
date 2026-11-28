import unittest

from engine.lumen.task_closeout import summarize


class TaskCloseout(unittest.TestCase):
    def test_only_closed_task_registrations_are_listed(self):
        task = dict(
            id='finished', root='/work/edit', agent='Codex', closed=True,
            inputs=['/work/shared/theme.wav', '/work/edit/export.mov'],
            outputs=['/work/edit/export.mov'],
            temporary=['/work/edit/preview.mp4', '/work/edit/.cache/render.png',
                       '/work/edit/pipeline.gguf', '/work/edit/notes.txt'],
        )
        report = {'files': [
            {'path': '/work/edit/preview.mp4', 'kind': 'video', 'decision': 'review'},
            {'path': '/work/edit/.cache/render.png', 'kind': 'image', 'cleanup_kind': 'cache'},
            {'path': '/work/edit/pipeline.gguf', 'kind': 'model', 'decision': 'keep'},
            {'path': '/work/edit/notes.txt', 'kind': 'other'},
            {'path': '/work/edit/unrelated.log', 'kind': 'log'},
        ]}
        summaries = summarize([task, {**task, 'id': 'open', 'closed': False}], report, [])
        self.assertEqual([summary['id'] for summary in summaries], ['finished'])
        items = {row['path']: row for row in summaries[0]['items']}
        self.assertEqual(
            {path: row['category'] for path, row in items.items()},
            {'/work/shared/theme.wav': 'input', '/work/edit/export.mov': 'output',
             '/work/edit/preview.mp4': 'render', '/work/edit/.cache/render.png': 'cache',
             '/work/edit/pipeline.gguf': 'model', '/work/edit/notes.txt': 'temporary'},
        )
        self.assertEqual(items['/work/edit/export.mov']['roles'], ['inputs', 'outputs'])
        self.assertFalse(items['/work/edit/export.mov']['indexed'])
        self.assertEqual(items['/work/edit/export.mov']['decision'], 'unknown')
        self.assertEqual(items['/work/edit/export.mov']['projects'], ['/work/edit'])

    def test_uses_current_shared_projects_and_reference_evidence(self):
        theme = '/work/shared/theme.wav'
        alias = '/work/film/theme-copy.wav'
        edit = '/work/edit/edit.fcpxml'
        film = '/work/film/film.fcpxml'
        repo = '/work/edit'
        film_repo = '/work/film'
        report = {
            'files': [
                {'path': theme, 'dev': 2, 'ino': 4, 'nlink': 2, 'kind': 'audio',
                 'decision': 'keep', 'reason': '用户标记保留或任务正在引用',
                 'refs': [f'{edit}#track'], 'projects': [repo]},
                {'path': alias, 'dev': 2, 'ino': 4, 'nlink': 2, 'kind': 'audio',
                 'decision': 'keep', 'refs': [f'{film}#asset'], 'projects': [film_repo]},
                {'path': edit, 'projects': [repo]},
            ],
            'projects': {repo: [], film_repo: []},
            'static_refs': [
                {'source': edit, 'target': theme, 'location': 'track'},
                {'source': film, 'target': alias, 'location': 'asset'},
            ],
        }
        [summary] = summarize([dict(id='done', root=repo, agent='Codex', closed=True, inputs=[theme])], report, [])
        [item] = summary['items']
        self.assertEqual(item['decision'], 'keep')
        self.assertEqual(item['reason'], '用户标记保留或任务正在引用')
        self.assertEqual(item['projects'], [repo, film_repo])
        self.assertEqual(item['refs'], [f'{edit}#track', f'{film}#asset'])
        self.assertEqual([(r['source'], r['projects']) for r in item['references']],
                         [(edit, [repo]), (film, [film_repo])])

    def test_closed_task_uses_only_linked_journal_status_and_service_skips(self):
        temp = '/work/edit/preview.mp4'
        deferred = '/work/edit/offline.tmp'
        task = dict(
            id='done', root='/work/edit', closed=True, temporary=[temp, deferred],
            finish_batch_ids=['task-batch', 'unavailable-batch'],
            finish_skips=[dict(path=deferred, action='quarantine', status='skipped', reason='所属卷不在线')],
        )
        history = [
            dict(id='task-batch', action='quarantine', items=[
                dict(path=temp, status='restored', restored_to='/work/edit/preview restored.mp4', size=128),
            ]),
            dict(id='other-batch', action='quarantine', items=[dict(path=temp, status='purged')]),
        ]
        [summary] = summarize([task], {}, history)
        self.assertEqual(summary['batch_ids'], ['task-batch', 'unavailable-batch'])
        items = {row['path']: row for row in summary['items']}
        self.assertEqual(items[temp]['category'], 'render')
        self.assertEqual(items[temp]['size'], 128)
        [action] = items[temp]['actions']
        self.assertEqual(action['status'], 'restored')
        self.assertEqual(action['restored_to'], '/work/edit/preview restored.mp4')
        self.assertEqual(items[deferred]['actions'][0]['reason'], '所属卷不在线')

    def test_legacy_tasks_do_not_attribute_unlinked_history(self):
        task = dict(root='/work/edit', agent='Codex', closed=True, inputs=['/work/edit/missing.mov'])
        [summary] = summarize([task], {}, [
            dict(id='old-batch', items=[{'path': '/work/edit/missing.mov', 'status': 'purged'}]),
        ])
        self.assertEqual(summary['id'], '/work/edit|Codex')
        [item] = summary['items']
        self.assertEqual(item['actions'], [])
        self.assertEqual(item['decision'], 'unknown')

    def test_model_inventory_workflows_supply_related_projects_and_references(self):
        model = '/model-store/blobs/sha256-123'
        source = '/work/edit/workflows/shot.json'
        [summary] = summarize(
            [dict(id='done', root='/work/render', closed=True, temporary=[model])],
            dict(files=[dict(path=model, kind='other', decision='keep')], projects={'/work/edit': []},
                 model_inventory={'models': [
                     dict(files=[model], workflow_refs=[f'{source}#7'], refs=['latest']),
                 ]}),
            [],
        )
        [item] = summary['items']
        self.assertEqual(item['category'], 'model')
        self.assertEqual(item['projects'], ['/work/edit', '/work/render'])
        self.assertEqual(item['refs'], [f'{source}#7'])
        self.assertEqual(item['references'][0]['location'], '7')

    def test_structured_evidence_keeps_punctuation_inside_a_source_path(self):
        source, target = '/project/#draft.html', '/project/cover.png'
        report = dict(
            projects={'/project': []},
            files=[dict(path=target, refs=[f'{source}:17'])],
            static_refs=[dict(source=source, target=target, location='17')],
        )
        [summary] = summarize([dict(closed=True, inputs=[target])], report, [])
        [item] = summary['items']
        self.assertEqual(item['references'], [dict(source=source, target=target,
                                                   location='17', projects=['/project'])])


if __name__ == '__main__':
    unittest.main()
