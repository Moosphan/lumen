import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.lumen.agent_adapters import discover, extension_installations


class AgentAdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.project = self.home / 'Projects/电影 Project'
        env = patch.dict(os.environ, {'GEMINI_CLI_HOME': '', 'XDG_DATA_HOME': '', 'XDG_CACHE_HOME': ''})
        env.start()
        self.addCleanup(env.stop)

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        return path

    def scan(self, quick=True, **kwargs):
        return discover(self.home, quick, **kwargs)

    def test_cursor_decodes_local_folder_uri_and_ignores_remote_or_workspace_file(self):
        root = 'Library/Application Support/Cursor/User/workspaceStorage'
        source = self.write(f'{root}/a/workspace.json', {'folder': self.project.as_uri()})
        self.write(f'{root}/b/workspace.json', {'folder': f'file://remote{self.project}'})
        self.write(f'{root}/c/workspace.json', {'workspace': (self.project / 'foo.code-workspace').as_uri()})
        self.write(f'{root}/d/workspace.json', {'folder': 'vscode-remote://ssh-remote+host/repo'})
        stores, projects = self.scan()
        self.assertEqual(projects, {self.project: {'Cursor'}})
        self.assertEqual([(agent, path) for agent, path, _ in stores], [('Cursor', source.parents[1])])

    def test_incremental_discovery_caches_only_projections_and_revalidates_changes(self):
        from engine.lumen.agent_adapters import _Metadata

        cursor = self.write('Library/Application Support/Cursor/User/workspaceStorage/id/workspace.json',
                            {'folder': self.project.as_uri()})
        transcript = self.write('.cline/data/tasks/taskHistory.json', [
            {'id': 't', 'cwdOnTaskInitialization': str(self.project), 'task': 'private task example'},
        ])
        cache = {}
        expected = self.scan(cache=cache)
        self.assertNotIn('private task example', json.dumps(cache))
        with patch.object(_Metadata, 'read_json', side_effect=AssertionError("parsed unchanged metadata")):
            self.assertEqual(self.scan(cache=cache), expected)

        destination = self.home / 'Projects/another'
        cursor.write_text(json.dumps({'folder': destination.as_uri()}))
        _, projects = self.scan(cache=cache)
        self.assertEqual(projects, {destination: {'Cursor'}, self.project: {'Cline'}})
        _, projects = self.scan(cache=cache, excluded=(transcript.parent,))
        self.assertEqual(projects, {destination: {'Cursor'}})

    def test_incremental_discovery_detects_new_metadata_under_an_empty_store(self):
        cache = {}
        self.assertEqual(self.scan(cache=cache), ([], {}))
        self.write('.gemini/projects.json', {'projects': {str(self.project): 'film'}})
        self.assertEqual(self.scan(cache=cache)[1], {self.project: {'Gemini CLI'}})

    def test_gemini_uses_project_registry_and_honors_explicit_in_home_override(self):
        source = self.write('.gemini/projects.json', {'projects': {str(self.project): 'film'}})
        alternate = self.home / 'agent-home'
        other = self.home / 'Projects/other'
        custom = self.write('agent-home/.gemini/projects.json', {'projects': {str(other): 'other'}})
        with patch.dict(os.environ, {'GEMINI_CLI_HOME': str(alternate)}):
            stores, projects = self.scan()
        self.assertEqual(projects, {self.project: {'Gemini CLI'}, other: {'Gemini CLI'}})
        self.assertEqual({path for _, path, _ in stores}, {source.parent, custom.parent})

    def test_gemini_external_config_requires_scope_for_the_derived_store(self):
        target = self.base / "config" / ".gemini"
        target.mkdir(parents=True)
        (target / "projects.json").write_text(json.dumps({"projects": {str(self.project): "film"}}))
        with patch.dict(os.environ, {"GEMINI_CLI_HOME": str(target.parent)}):
            self.assertEqual(self.scan(), ([], {}))
            stores, projects = self.scan(authorized=[target])
        self.assertEqual({path for _, path, _ in stores}, {target})
        self.assertEqual(projects, {self.project: {"Gemini CLI"}})

    def test_gemini_prefers_project_root_marker_when_registry_conflicts(self):
        old = self.home / 'Projects/old'
        self.write('.gemini/projects.json', {'projects': {str(old): 'film'}})
        self.write('.gemini/tmp/film/.project_root', str(self.project).encode())
        _, projects = self.scan()
        self.assertEqual(projects, {self.project: {'Gemini CLI'}})

    def test_gemini_ambiguous_registry_keeps_the_store_unlinked(self):
        other = self.home / 'Projects/other'
        path = self.write('.gemini/projects.json', {'projects': {str(self.project): 'same', str(other): 'same'}})
        issues = set()
        stores, projects = self.scan(issues=issues)
        self.assertEqual(projects, {})
        self.assertEqual([row[1] for row in stores], [path.parent])
        self.assertTrue(any(str(path) in issue for issue in issues))

    def test_cline_uses_actual_task_cwd_in_legacy_and_shared_storage(self):
        legacy = self.write('Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/state/taskHistory.json', [
            {'id': 'task', 'ts': 1, 'cwdOnTaskInitialization': str(self.project),
             'shadowGitConfigWorkTree': str(self.home / 'Projects/shadow')},
        ])
        other = self.home / 'Projects/other'
        current = self.write('.cline/data/tasks/taskHistory.json', [
            {'id': 'task2', 'ts': 2, 'cwdOnTaskInitialization': str(other)},
            {'id': 'task3', 'ts': 3, 'task': str(self.home / 'Projects/mentioned')},
        ])
        stores, projects = self.scan()
        self.assertEqual(projects, {self.project: {'Cline'}, other: {'Cline'}})
        self.assertEqual({path for _, path, _ in stores}, {legacy.parents[1], current.parents[1]})

    def test_opencode_recognizes_legacy_project_and_current_sqlite_worktree(self):
        other = self.home / 'Projects/other'
        worktree = self.home / 'Worktrees/branch'
        plain = self.home / 'Projects/without-git'
        legacy = self.write('.local/share/opencode/storage/project/legacy.json', {'id': 'legacy', 'worktree': str(self.project)})
        database = legacy.parents[2] / 'opencode.db'
        with sqlite3.connect(database) as db:
            db.executescript('''
                CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, sandboxes TEXT);
                CREATE TABLE worktree (project_id TEXT, directory TEXT);
                CREATE TABLE session_v2 (project_id TEXT, directory TEXT);
            ''')
            db.execute('INSERT INTO project VALUES (?, ?, ?)', ('project', str(other), '[]'))
            db.execute('INSERT INTO project VALUES (?, ?, ?)', ('global', '/', '[]'))
            db.execute('INSERT INTO worktree VALUES (?, ?)', ('project', str(worktree)))
            db.execute('INSERT INTO session_v2 VALUES (?, ?)', ('project', str(other / 'subdir')))
            db.execute('INSERT INTO session_v2 VALUES (?, ?)', ('global', str(plain)))
        contents = database.read_bytes()
        stores, projects = self.scan()
        self.assertEqual(projects, {path: {'OpenCode'} for path in (self.project, other, worktree, plain)})
        self.assertEqual({path for _, path, _ in stores}, {database.parent})
        self.assertEqual(database.read_bytes(), contents)
        self.assertEqual(sorted(p.name for p in database.parent.iterdir()), ['opencode.db', 'storage'])

    def test_kilo_projects_and_plain_sessions_are_read_without_mutating_the_database(self):
        directory = self.home / '.local/share/kilo'
        directory.mkdir(parents=True)
        database = directory / 'kilo.db'
        alternate = self.home / 'Worktrees/alternate'
        checkout = self.home / 'Projects/a separate checkout'
        plain = self.home / 'Projects/without git'
        cloud = self.home / 'Projects/cloud lookalike'
        oversized_remote = self.home / 'Projects/oversized remote lookalike'
        with sqlite3.connect(database) as db:
            db.executescript('''
                CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, sandboxes TEXT);
                CREATE TABLE project_directory (project_id TEXT, directory TEXT, type TEXT);
                CREATE TABLE session (project_id TEXT, directory TEXT, workspace_id TEXT);
            ''')
            db.execute('INSERT INTO project VALUES (?, ?, ?)', ('project', str(self.project), json.dumps([str(alternate)])))
            db.execute('INSERT INTO project_directory VALUES (?, ?, ?)', ('project', str(checkout), 'git_worktree'))
            db.execute('INSERT INTO session VALUES (?, ?, ?)', ('global', str(plain), None))
            db.execute('INSERT INTO session VALUES (?, ?, ?)', ('global', str(cloud), 'workspace-remote'))
            db.execute('INSERT INTO session VALUES (?, ?, ?)', ('global', str(oversized_remote), 'x' * 4097))
            db.execute('INSERT INTO session VALUES (?, ?, ?)', ('global', 'vscode-remote://ssh-remote+host/repo', None))
            db.execute('INSERT INTO session VALUES (?, ?, ?)', ('project', str(self.project / 'subdir'), None))
        db.close()
        original = database.read_bytes()
        cache = {}
        with patch.dict(os.environ, {'KILO_DB': ''}):
            stores, projects = self.scan(cache=cache)
            expected = {p: {'Kilo Code CLI'} for p in (self.project, alternate, checkout, plain)}
            self.assertEqual(projects, expected)
            self.assertEqual([(name, path) for name, path, _ in stores], [('Kilo Code CLI', directory)])
            self.assertEqual(database.read_bytes(), original)
            self.assertEqual(list(directory.iterdir()), [database])
            with sqlite3.connect(database) as db:
                db.execute('UPDATE session SET directory = ? WHERE directory = ?', (str(cloud), str(plain)))
            db.close()
            expected[cloud] = expected.pop(plain)
            self.assertEqual(self.scan(cache=cache)[1], expected)
            journal = database.with_name(database.name + '-journal')
            journal.write_bytes(b'rollback in progress')
            issues = set()
            self.assertEqual(self.scan(cache=cache, issues=issues)[1], {})
            self.assertTrue(any('回滚日志' in issue for issue in issues))
            self.assertEqual(journal.read_bytes(), b'rollback in progress')
            journal.unlink()
            self.assertEqual(self.scan(cache=cache)[1], expected)

    def test_kilo_relative_database_uses_authorized_xdg_root_and_rejects_links(self):
        data = self.base / 'xdg'
        root = data / 'kilo'
        root.mkdir(parents=True)
        database = root / 'profile.db'
        fallback = self.home / '.local/share/kilo/profile.db'
        fallback.parent.mkdir(parents=True)
        for path, project in ((database, self.project), (fallback, self.home / 'Incorrect Legacy Fallback')):
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE session (project_id TEXT, directory TEXT, workspace_id TEXT)')
                db.execute('INSERT INTO session VALUES (?, ?, ?)', ('global', str(project), None))
            db.close()
        link = root / 'linked.db'
        link.symlink_to(database)
        cache = {}
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(data), 'KILO_DB': 'profile.db'}):
            self.assertEqual(self.scan(cache=cache)[1], {})
            self.assertEqual(self.scan(cache=cache, authorized=(data,))[1], {self.project: {'Kilo Code CLI'}})
            with patch.dict(os.environ, {'KILO_DB': 'linked.db'}):
                self.assertEqual(self.scan(cache=cache, authorized=(data,))[1], {})

    def test_ignored_symlinks_and_exclusions_cannot_supply_metadata(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'projects.json').write_text(json.dumps({'projects': {str(self.project): 'film'}}))
        (self.home / '.gemini').symlink_to(outside, target_is_directory=True)
        root = 'Library/Application Support/Cursor/User/workspaceStorage'
        excluded = self.write(f'{root}/a/workspace.json', {'folder': self.project.as_uri()})
        (outside / 'workspace.json').write_text(json.dumps({'folder': self.project.as_uri()}))
        (excluded.parents[1] / 'b').symlink_to(outside, target_is_directory=True)
        cline = self.write('.cline/data/tasks/taskHistory.json', [])
        cline.unlink()
        cline.symlink_to(outside / 'projects.json')
        stores, projects = self.scan(excluded=(excluded.parent,))
        self.assertEqual(projects, {})
        self.assertEqual({agent for agent, _, _ in stores}, {'Cursor', 'Cline'})

    def test_malformed_and_oversize_metadata_leave_store_visible_with_issue(self):
        broken = self.write('.gemini/projects.json', b'[[[[[[[')
        oversize = self.write('.cline/data/tasks/taskHistory.json', b' ' * (4 * 1024 * 1024 + 1))
        issues = set()
        stores, projects = self.scan(issues=issues)
        self.assertEqual(projects, {})
        self.assertEqual({agent for agent, _, _ in stores}, {'Gemini CLI', 'Cline'})
        self.assertTrue(any(str(broken) in issue for issue in issues))
        self.assertTrue(any(str(oversize) in issue for issue in issues))

    def test_projects_in_multiple_stores_share_one_path(self):
        self.write('.gemini/projects.json', {'projects': {str(self.project): 'film'}})
        self.write('.cline/data/tasks/taskHistory.json', [{'id': 't', 'ts': 1, 'cwdOnTaskInitialization': str(self.project)}])
        _, projects = self.scan()
        self.assertEqual(projects, {self.project: {'Gemini CLI', 'Cline'}})

    def test_cline_installation_requires_matching_manifest_and_is_not_obsolete(self):
        root = '.vscode/extensions/saoudrizwan.claude-dev'
        manifest = {'publisher': 'saoudrizwan', 'name': 'claude-dev', 'version': '3.45.1'}
        valid = self.write(f'{root}-3.45.1/package.json', manifest)
        self.write(f'{root}-3.40.0/package.json', {**manifest, 'version': '3.40.0'})
        self.write(f'{root}-3.46.0/package.json', {**manifest, 'publisher': 'different'})
        self.write('.vscode/extensions/.obsolete', {'saoudrizwan.claude-dev-3.40.0': True})
        copilot = self.write('.vscode/extensions/github.copilot-chat-0.44.0/package.json', {
            'publisher': 'GitHub', 'name': 'copilot-chat', 'version': '0.44.0',
        })
        self.assertEqual(extension_installations(self.home), [
            ('Cline', valid.parent, '3.45.1'), ('GitHub Copilot VS Code', copilot.parent, '0.44.0'),
        ])

    @unittest.skipIf(os.geteuid() == 0, 'root can read permission-denied fixtures')
    def test_inaccessible_metadata_is_reported_and_respects_exclusion(self):
        path = self.write('.gemini/projects.json', {'projects': {str(self.project): 'film'}})
        path.chmod(0)
        issues = set()
        stores, projects = self.scan(issues=issues)
        self.assertEqual({agent for agent, _, _ in stores}, {'Gemini CLI'})
        self.assertEqual(projects, {})
        self.assertTrue(any(str(path) in issue for issue in issues))
        issues.clear()
        self.scan(issues=issues, excluded=(path,))
        self.assertEqual(issues, set())
