import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.lumen.agent_adapters import discover
from engine.lumen.agent_ide import ADAPTERS


class AgentIDEAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.project = self.home / 'Projects/电影 Project'
        environment = patch.dict(os.environ, {'COPILOT_HOME': '', 'KIRO_HOME': ''})
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, path, payload):
        path = self.home / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        return path

    def scan(self, **kwargs):
        return discover(self.home, True, adapters=ADAPTERS, **kwargs)

    def copilot_event(self, session, cwd=None):
        return json.dumps({'type': 'session.start', 'data': {
            'sessionId': session, 'version': 1, 'producer': 'copilot-agent',
            'context': {'cwd': str(self.project) if cwd is None else cwd},
        }}).encode() + b'\n'

    def test_copilot_cli_reads_only_the_initial_event_and_validates_session_identity(self):
        path = self.write('.copilot/session-state/valid/events.jsonl',
                          self.copilot_event('valid') + b'private transcript ' * (1024 * 64))
        self.write('.copilot/session-state/wrong/events.jsonl',
                   self.copilot_event('valid', str(self.home / 'bad')))
        self.write('.copilot/session-state/too-large/events.jsonl',
                   self.copilot_event('too-large', '/' + 'x' * 32768))
        self.write('.copilot/session-state/mixed/events.jsonl',
                   b'{"type":"user.message"}\n' + self.copilot_event('mixed', str(self.home / 'bad')))
        log = self.write('.copilot/logs/process.log', b'diagnostic output')
        cached = self.write('Library/Caches/copilot/package.tar.gz', b'package cache')
        problems, cache = set(), {}
        stores, projects = self.scan(issues=problems, cache=cache)
        self.assertEqual(projects, {self.project: {'GitHub Copilot CLI'}})
        self.assertEqual({(name, directory) for name, directory, _ in stores}, {
            ('GitHub Copilot CLI', path.parents[1]), ('GitHub Copilot CLI', log.parent),
            ('GitHub Copilot CLI', cached.parent),
        })
        self.assertNotIn('private transcript', json.dumps(cache))
        self.assertLess(cache['readers']['_copilot_cli']['spent'], 64 * 1024)
        self.assertTrue(any('/too-large/events.jsonl' in message for message in problems))

    def test_copilot_cli_custom_home_requires_the_scope_and_ignores_symlinks(self):
        external = self.base / 'Copilot Data'
        self.write(external / 'session-state/local/events.jsonl', self.copilot_event('local'))
        target = self.write(self.base / 'unrelated.jsonl',
                            self.copilot_event('symlink', str(self.home / 'unrelated')))
        symlink_parent = external / 'session-state/symlink'
        symlink_parent.mkdir()
        (symlink_parent / 'events.jsonl').symlink_to(target)
        with patch.dict(os.environ, {'COPILOT_HOME': str(external)}):
            self.assertEqual(self.scan(), ([], {}))
            stores, projects = self.scan(authorized=(external,))
            self.assertEqual(projects, {self.project: {'GitHub Copilot CLI'}})
            self.assertEqual([directory for _, directory, _ in stores], [external / 'session-state'])

    def test_vscode_association_requires_copilot_specific_storage_and_local_folder_uri(self):
        root = 'Library/Application Support/Code - Insiders/User'
        transcript = 'GitHub.copilot-chat/transcripts/session.jsonl'
        global_file = self.write(f'{root}/globalStorage/github.copilot-chat/state.json', {})
        folder = self.write(f'{root}/workspaceStorage/local/workspace.json', {'folder': self.project.as_uri()})
        owned = self.write(f'{root}/workspaceStorage/local/{transcript}', b'private transcript')
        self.write(f'{root}/workspaceStorage/unrelated/workspace.json', {'folder': (self.home / 'other').as_uri()})
        self.write(f'{root}/workspaceStorage/unrelated/chatSessions/session.jsonl', b'generic transcript')
        self.write(f'{root}/workspaceStorage/remote/workspace.json', {'folder': 'vscode-remote://ssh-remote+host/repo'})
        remote = self.write(f'{root}/workspaceStorage/remote/{transcript}', b'private transcript')
        stores, projects = self.scan()
        self.assertEqual(projects, {self.project: {'GitHub Copilot VS Code'}})
        self.assertEqual({(name, directory) for name, directory, _ in stores}, {
            ('GitHub Copilot VS Code', global_file.parent),
            ('GitHub Copilot VS Code', owned.parent),
            ('GitHub Copilot VS Code', remote.parent),
        })
        replacement = self.home / 'Replacement'
        self.write(folder, {'folder': replacement.as_uri()})
        self.assertEqual(self.scan()[1], {replacement: {'GitHub Copilot VS Code'}})

    def test_windsurf_reports_cascade_without_parsing_opaque_data(self):
        file = self.write('.codeium/windsurf/cascade/session.pb', os.fsencode(self.project))
        self.write('.codeium/unrelated/session.pb', b'other Codeium product')
        stores, projects = self.scan()
        self.assertEqual(projects, {})
        self.assertEqual([(name, directory) for name, directory, _ in stores], [('Windsurf', file.parent)])

    def test_windsurf_associates_local_editor_workspaces_and_revalidates_cached_metadata(self):
        root = 'Library/Application Support/Windsurf/User/workspaceStorage'
        replacement = self.home / 'Replacement'
        next_project = self.home / 'Next/Local'
        local = self.write(f'{root}/local/workspace.json', {'folder': self.project.as_uri()})
        state = self.write(f'{root}/local/state.vscdb', b'private editor state')
        self.write(f'{root}/remote/workspace.json', {'folder': 'vscode-remote://ssh-remote+host/project'})
        self.write(f'{root}/network/workspace.json', {'folder': 'file://server/Users/project'})
        self.write(f'{root}/multi-root/workspace.json', {'workspace': (self.home / 'file.code-workspace').as_uri()})
        self.write(f'{root}/malformed/workspace.json', {'folder': f'{self.project.as_uri()}%QQ'})
        other = self.write('Library/Application Support/Code/User/workspaceStorage/other/workspace.json',
                           {'folder': (self.home / 'other').as_uri()})
        (local.parents[1] / 'symlink').mkdir()
        (local.parents[1] / 'symlink/workspace.json').symlink_to(other)
        self.write('Library/Application Support/Windsurf - Next/User/workspaceStorage/next/workspace.json',
                   {'folder': next_project.as_uri()})
        cache = {}
        stores, projects = self.scan(cache=cache)
        self.assertEqual(projects, {self.project: {'Windsurf'}, next_project: {'Windsurf'}})
        self.assertTrue(any(agent == 'Windsurf' and directory == local.parents[1] for agent, directory, _ in stores))
        watched = {path for reader in cache['readers'].values() for path in reader['stamps']}
        self.assertNotIn(str(state), watched)
        self.assertNotIn('private editor state', json.dumps(cache))

        self.write(local, {'folder': replacement.as_uri()})
        self.assertEqual(self.scan(cache=cache)[1], {replacement: {'Windsurf'}, next_project: {'Windsurf'}})
        self.assertEqual(self.scan(cache=cache, excluded=(local.parent,))[1], {next_project: {'Windsurf'}})

    def test_windsurf_reads_saved_jsonc_workspace_folders_and_refreshes_edits(self):
        records = 'Library/Application Support/Devin/User/workspaceStorage'
        local = self.home / '另一个项目'
        manifest = self.write('Workspaces/电影.code-workspace', (
            '\ufeff{ // saved workspace\n'
            '  "folders": [\n'
            '    /* production */ { "path": "../Projects/电影 Project" },\n'
            '    {"path": ' + json.dumps(str(local)) + '},\n'
            '    {"uri": ' + json.dumps(local.as_uri()) + ', "name": "notes /* text */ // ,}"},\n'
            '    {"uri": "vscode-remote://ssh-remote+host/repo"},\n'
            '    {"path": "//server/project"},\n'
            '    {"path": "C:/remote/project"},\n'
            '  ],\n'
            '  "settings": { "private": "sample settings are not indexed" },\n'
            '}\n').encode())
        self.write(f'{records}/local/workspace.json', {'workspace': manifest.as_uri()})
        remote = self.write('remote.code-workspace', {'remoteAuthority': 'ssh-remote+host',
                            'folders': [{'path': str(self.home / 'Remote')}]})
        self.write(f'{records}/remote/workspace.json', {'workspace': remote.as_uri()})
        malformed = self.write('invalid.code-workspace', b'{"folders":[,]}')
        self.write(f'{records}/invalid/workspace.json', {'workspace': malformed.as_uri()})
        cache, issues = {}, set()
        self.assertEqual(self.scan(cache=cache, issues=issues)[1], {self.project: {'Windsurf'}, local: {'Windsurf'}})
        self.assertNotIn('sample settings are not indexed', json.dumps(cache))
        self.assertTrue(any(str(malformed) in issue for issue in issues))
        self.write(manifest, {'folders': [{'path': str(local)}]})
        self.assertEqual(self.scan(cache=cache)[1], {local: {'Windsurf'}})
        self.assertEqual(self.scan(cache=cache, excluded=(manifest,))[1], {})

    def test_windsurf_workspace_file_must_be_authorized_and_cannot_be_a_link(self):
        root = 'Library/Application Support/Windsurf/User/workspaceStorage'
        external = self.write(self.base / 'outside/shared.code-workspace', {'folders': [{'path': str(self.project)}]})
        self.write(f'{root}/external/workspace.json', {'workspace': external.as_uri()})
        target = self.write('Workspaces/control.code-workspace', {'folders': [{'path': './Ignored'}]})
        link = target.with_name('link.code-workspace')
        link.symlink_to(target)
        self.write(f'{root}/link/workspace.json', {'workspace': link.as_uri()})
        self.assertEqual(self.scan()[1], {})
        self.assertEqual(self.scan(authorized=(external.parent,))[1], {self.project: {'Windsurf'}})

    def test_windsurf_discovers_untitled_workspaces_without_window_index_and_forgets_deleted_config(self):
        root = 'Library/Application Support/Devin/Workspaces'
        second = self.home / 'Local 🎬'
        primary = self.write(f'{root}/1795610510123/workspace.json', {
            'folders': [{'path': str(self.project)}, {'uri': second.as_uri()}],
        })
        legacy = self.write('Library/Application Support/Windsurf - Next/Workspaces/1795610510444/workspace.json', {
            'folders': [{'path': str(self.project)}],
        })
        cache = {}
        stores, projects = self.scan(cache=cache)
        self.assertEqual(projects, {self.project: {'Windsurf'}, second: {'Windsurf'}})
        self.assertEqual({path for agent, path, _ in stores if agent == 'Windsurf'},
                         {primary.parents[1], legacy.parents[1]})
        primary.unlink()
        self.assertEqual(self.scan(cache=cache)[1], {self.project: {'Windsurf'}})

    def test_windsurf_untitled_reads_only_local_editor_created_workspace_files(self):
        root = 'Library/Application Support/Windsurf/Workspaces'
        valid = self.write(f'{root}/1795610510123/workspace.json', {'folders': [{'path': str(self.project)}]})
        other = {'folders': [{'path': str(self.home / 'Unexpected')}]}
        self.write(f'{root}/manual/workspace.json', other)
        self.write(f'{root}/1795610510222/backup.json', other)
        self.write(f'{root}/1795610510333/workspace.json', {**other, 'remoteAuthority': 'ssh-remote+host'})
        remote = self.write('Library/Application Support/Code/Workspaces/1795610510444/workspace.json', other)
        (valid.parents[1] / '1795610510444').symlink_to(remote.parent, target_is_directory=True)
        linked = valid.parents[1] / '1795610510555'
        linked.mkdir()
        (linked / 'workspace.json').symlink_to(remote)
        self.assertEqual(self.scan()[1], {self.project: {'Windsurf'}})
        self.assertEqual(self.scan(excluded=(valid,))[1], {})

    def test_trae_discovers_local_projects_from_both_default_editor_profiles(self):
        main = 'Library/Application Support/Trae'
        china = 'Library/Application Support/Trae CN'
        workspaces = f'{main}/User/workspaceStorage'
        single = self.write(f'{workspaces}/a9c6/workspace.json', {'folder': self.project.as_uri()})
        saved = self.home / 'Documents/Edit.code-workspace'
        first = self.home / 'Projects/Edit'
        second = self.home / 'Projects/Color'
        self.write(saved, b'{ // shared edit\n "folders": [{"path": "../Projects/Edit"}, {"path": "../Projects/Color"},],\n}')
        self.write(f'{china}/User/workspaceStorage/8bc1/workspace.json', {'workspace': saved.as_uri()})
        self.write(f'{workspaces}/remote/workspace.json', {'folder': 'vscode-remote://ssh-remote+lab/code'})
        self.write(f'{workspaces}/conflict/workspace.json', {'folder': first.as_uri(), 'workspace': saved.as_uri()})
        untitled = self.home / 'Temporary Cut'
        self.write(f'{china}/Workspaces/1795610510123/workspace.json', {'folders': [{'path': str(untitled)}]})
        self.write(f'{china}/Workspaces/1795610510130/workspace.json', {
            'remoteAuthority': 'ssh-remote+host', 'folders': [{'path': str(self.home / 'Remote')}],
        })
        external = self.write(self.base / 'linked.json', {'folder': (self.home / 'Linked').as_uri()})
        (single.parents[1] / 'link').mkdir()
        (single.parents[1] / 'link/workspace.json').symlink_to(external)
        snapshot = self.write(f'{china}/ModularData/ai-agent/snapshot/retained.json', b'private snapshot body')
        self.write('.trae-cn/extensions/fixture/fixture.txt', b'extension source')
        cache = {}
        stores, projects = self.scan(cache=cache)
        self.assertEqual(projects, {path: {'Trae'} for path in (self.project, first, second, untitled)})
        self.assertIn(('Trae', snapshot.parents[2]), {(agent, path) for agent, path, _ in stores})
        self.assertNotIn('private snapshot body', json.dumps(cache))
        self.write(saved, {'folders': [{'path': '../Projects/Color'}]})
        self.assertEqual(self.scan(cache=cache)[1], {path: {'Trae'} for path in (self.project, second, untitled)})
        self.assertEqual(self.scan(excluded=(self.home / china,))[1], {self.project: {'Trae'}})

    def test_trae_saved_workspace_outside_home_requires_folder_authorization(self):
        workspace = self.write(self.base / 'manual/Layout.code-workspace', {
            'folders': [{'uri': self.project.as_uri()}],
        })
        self.write('Library/Application Support/Trae/User/workspaceStorage/one/workspace.json',
                   {'workspace': workspace.as_uri()})
        self.assertEqual(self.scan()[1], {})
        self.assertEqual(self.scan(authorized=(workspace.parent,))[1], {self.project: {'Trae'}})

    def test_traework_detects_the_standalone_profile_and_local_projects(self):
        root = self.home / 'Library/Application Support/TRAE SOLO CN'
        projects = root / 'ModularData/ai-agent/work-mode-projects'
        project = projects / '6a4db2ef0f25b4742bf9cdc2'
        self.write(project / 'report.html', b'private local deliverable')
        self.write(projects / 'unverified' / 'text.txt', b'no project identity')
        copy = self.write('Library/Application Support/Trae CN/ModularData/ai-agent/work-mode-projects/'
                          '6a4db2ef0f25b4742bf9cdc3/copy.txt', b'merged app data')
        link = projects / '6a4db2ef0f25b4742bf9cdc4'
        link.symlink_to(copy.parent, target_is_directory=True)
        disk = self.write(root / 'ModularData/ai-agent/vm/vms/example.img', b'private runtime state')
        self.write(root / 'User/workspaceStorage/fixture/workspace.json', {'folder': self.project.as_uri()})
        self.write(root / 'User/workspaceStorage/remote/workspace.json',
                   {'folder': 'vscode-remote://ssh-remote+example/home/remote'})
        cache = {}
        stores, found = self.scan(cache=cache)
        self.assertEqual(found, {project: {'TraeWork'}, self.project: {'TraeWork'}})
        locations = {path for agent, path, _ in stores if agent == 'TraeWork'}
        self.assertTrue({root, disk.parents[1], projects}.issubset(locations))
        self.assertNotIn('private local deliverable', json.dumps(cache))
        self.assertNotIn('private runtime state', json.dumps(cache))
        self.assertEqual(self.scan(excluded=(project,))[1], {self.project: {'TraeWork'}})
        project.rename(projects / '6a4db2ef0f25b4742bf9cdc5')
        self.assertNotIn(project, self.scan(cache=cache)[1])

    def test_roo_uses_per_task_workspace_and_ignores_stale_index_and_messages(self):
        root = 'Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline'
        item = self.write(f'{root}/tasks/owned/history_item.json', {
            'id': 'owned', 'workspace': str(self.project), 'task': 'private task text',
        })
        self.write(f'{root}/tasks/other/history_item.json', {'id': 'owned', 'workspace': str(self.home / 'other')})
        self.write(f'{root}/tasks/_index.json', {'version': 1, 'entries': [
            {'id': 'missing', 'workspace': str(self.home / 'missing')},
        ]})
        self.write(f'{root}/tasks/plain/api_conversation_history.json', [
            {'id': 'plain', 'workspace': str(self.home / 'plain')},
        ])
        self.write(f'{root}/tasks/broken/history_item.json', b'not JSON')
        issues, cache = set(), {}
        stores, projects = self.scan(issues=issues, cache=cache)
        self.assertEqual(projects, {self.project: {'Roo Code'}})
        self.assertEqual([(name, directory) for name, directory, _ in stores], [('Roo Code', item.parents[2])])
        self.assertTrue(any('/broken/history_item.json' in issue for issue in issues))
        self.assertNotIn('private task text', json.dumps(cache))
        self.write(item, {'id': 'owned', 'workspace': str(self.home / 'new')})
        self.assertEqual(self.scan(cache=cache)[1], {self.home / 'new': {'Roo Code'}})

    def test_kiro_reads_local_multi_folder_session_metadata_and_keeps_legacy_store(self):
        root = '.kiro/sessions'
        group = '0123456789abcdef'
        second = self.home / 'Another Repo'
        item = self.write(f'{root}/{group}/sess_one/session.json', {
            'id': 'sess_one', 'workspacePaths': [str(self.project), str(second), 'ssh://host/projects/foo'],
        })
        self.write(f'{root}/{group}/sess_one/messages.jsonl', os.fsencode(self.home / 'private'))
        self.write(f'{root}/{group}/sess_remote/session.json', {'id': 'sess_remote',
                   'executionTarget': {'kind': 'cloud-sandbox'}, 'workspacePaths': [str(self.home / 'remote')]})
        self.write(f'{root}/cli/session.json', {'id': 'cli', 'workspacePaths': [str(self.home / 'cli')]})
        legacy = self.write('Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/'
                            'workspace-sessions/legacy/session.json', b'legacy transcript')
        log = self.write('Library/Application Support/Kiro/logs/current.log', b'log')
        stores, projects = self.scan()
        self.assertEqual(projects, {self.project: {'Kiro'}, second: {'Kiro'}})
        self.assertEqual({(name, directory) for name, directory, _ in stores}, {
            ('Kiro', item.parents[2]), ('Kiro', legacy.parents[1]), ('Kiro', log.parent),
        })
        remaining, projects = self.scan(excluded=(item.parents[1],))
        self.assertEqual(projects, {})
        self.assertEqual(remaining, stores)

    def test_kiro_custom_kas_store_requires_authorization_and_has_shared_attribution(self):
        custom = self.base / 'Custom Kiro'
        session = self.write(custom / 'sessions/0123456789abcdef/sess_local/session.json', {
            'id': 'sess_local', 'workspacePaths': [str(self.project)],
        })
        cache = {}
        with patch.dict(os.environ, {'KIRO_HOME': str(custom)}):
            self.assertEqual(self.scan(cache=cache), ([], {}))
            self.assertEqual(self.scan(cache=cache, authorized=(custom,)), (
                [('Kiro', session.parents[2], 'Kiro KAS 共享会话存储')], {self.project: {'Kiro'}},
            ))
