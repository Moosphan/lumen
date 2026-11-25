import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from engine.lumen.agent_adapters import discover


class LocalAgentToolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.project = self.home / 'Projects/电影 Project'
        keys = (
            'OPENCLAW_HOME', 'OPENCLAW_STATE_DIR', 'OPENCLAW_CONFIG_PATH',
            'OPENCLAW_WORKSPACE_DIR', 'OPENCLAW_PROFILE', 'QWEN_HOME', 'QWEN_RUNTIME_DIR',
            'GOOSE_PATH_ROOT', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME',
        )
        env = patch.dict(os.environ, dict.fromkeys(keys, ''))
        env.start()
        self.addCleanup(env.stop)

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        return path

    def scan(self, **options):
        from engine.lumen import agent_local_tools

        return discover(self.home, True, adapters=agent_local_tools.ADAPTERS, **options)

    def test_claude_separates_desktop_cowork_runtime_and_cowork_sessions(self):
        root = 'Library/Application Support/Claude'
        config = self.write(f'{root}/claude_desktop_config.json', {
            'mcpServers': {'filesystem': {'command': 'synthetic', 'args': [str(self.project)]}},
        })
        sessions = self.write(f'{root}/local-agent-mode-sessions/account/device/local_session.json',
                              {'cwd': str(self.project), 'userSelectedFolders': [str(self.project)]})
        runtime = self.write(f'{root}/vm_bundles/claudevm.bundle/disk.img', b'fixture')
        issues = set()
        stores, projects = self.scan(issues=issues)
        self.assertEqual(projects, {})
        self.assertEqual(set(path for _, path, _ in stores),
                         {config.parent, sessions.parents[2], runtime.parents[1]})
        session_store = next(row for row in stores if row[1] == sessions.parents[2])
        runtime_store = next(row for row in stores if row[1] == runtime.parents[1])
        self.assertNotEqual(session_store[2], runtime_store[2])
        self.assertTrue(any('Cowork' in issue for issue in issues))

    def test_openclaw_keeps_per_agent_sessions_and_automation_stores_separate(self):
        other = self.home / 'Projects/other'
        source = self.write('.openclaw/openclaw.json', {'agents': {'entries': {
            'research': {'workspace': '~/Projects/电影 Project'},
            'support': {'workspace': str(other), 'agentDir': str(self.home / 'AgentState/support')},
        }}, 'cron': {'enabled': True}})
        legacy = self.write('.openclaw/agents/research/sessions/sessions.json', {
            'agent:research:main': {'sessionId': 'fixture', 'cwd': '/remote/session'},
        })
        current = self.home / 'AgentState/support/openclaw-agent.sqlite'
        current.parent.mkdir(parents=True)
        with closing(sqlite3.connect(current)) as db, db:
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY)')
        cron = self.write('.openclaw/cron/jobs.json', {
            'version': 1, 'jobs': [{'id': 'fixture', 'agentId': 'research', 'payload': {
                'kind': 'agentTurn', 'message': str(self.project), 'cwd': '/remote/job',
            }}],
        })
        shared = self.write('.openclaw/state/openclaw.sqlite', b'SQLite format 3\0')
        stores, projects = self.scan()
        self.assertEqual(projects, {self.project: {'OpenClaw'}, other: {'OpenClaw'}})
        indexed = {path: evidence for name, path, evidence in stores if name == 'OpenClaw'}
        self.assertEqual(set(indexed), {source.parent, legacy.parent, current.parent, cron.parent, shared.parent})
        self.assertIn('research', indexed[legacy.parent])
        self.assertIn('support', indexed[current.parent])
        self.assertIn('调度', indexed[cron.parent])
        self.assertIn('调度', indexed[shared.parent])

    def test_openclaw_remote_and_include_configs_keep_stores_without_linking_paths(self):
        path = self.write('.openclaw/openclaw.json', {
            'gateway': {'mode': 'remote'}, 'agents': {'defaults': {'workspace': str(self.project)}},
        })
        cache = {}
        stores, projects = self.scan(cache=cache)
        self.assertEqual(projects, {})
        self.assertEqual([row[1] for row in stores], [path.parent])
        path.write_text(json.dumps({'$include': './other.json',
                                    'agents': {'defaults': {'workspace': str(self.project)}}}))
        self.write('.openclaw/other.json', {'agents': {'defaults': {'workspace': str(self.project)}}})
        issues = set()
        self.assertEqual(self.scan(cache=cache, issues=issues)[1], {})
        self.assertTrue(any('$include' in issue for issue in issues))

    def test_openclaw_accepts_an_authorized_profile_state_and_legacy_roster(self):
        target = self.base / 'agent-state'
        target.mkdir()
        (target / 'openclaw.json').write_text(json.dumps({
            'agents': {'list': [{'id': 'main', 'workspace': str(self.project)}]},
        }))
        with patch.dict(os.environ, {'OPENCLAW_STATE_DIR': str(target)}):
            self.assertEqual(self.scan(), ([], {}))
            stores, projects = self.scan(authorized=(target,))
        self.assertEqual([row[1] for row in stores], [target])
        self.assertEqual(projects, {self.project: {'OpenClaw'}})

    def test_openclaw_resolves_inherited_workspace_for_each_configured_agent(self):
        base = self.home / 'Projects'
        self.write('.openclaw/openclaw.json', {'agents': {
            'defaults': {'workspace': str(base)}, 'entries': {'research': {}, 'support': {}},
        }})
        self.assertEqual(self.scan()[1], {
            base / 'research': {'OpenClaw'}, base / 'support': {'OpenClaw'},
        })

    def test_openclaw_named_profile_is_selected_and_invalidates_cached_results(self):
        source = self.write('.openclaw-Research/openclaw.json', {
            'agents': {'defaults': {'workspace': str(self.project)}},
        })
        cache = {}
        self.assertEqual(self.scan(cache=cache), ([], {}))
        with patch.dict(os.environ, {'OPENCLAW_PROFILE': ' Research '}):
            stores, projects = self.scan(cache=cache)
        self.assertEqual([row[1] for row in stores], [source.parent])
        self.assertEqual(projects, {self.project: {'OpenClaw'}})
        self.assertEqual(self.scan(cache=cache), ([], {}))

    def test_qwen_reads_only_documented_local_chat_record_cwd(self):
        root = '.qwen/projects/encoded/chats'
        row = {'uuid': 'record', 'parentUuid': None, 'sessionId': 'one', 'type': 'user',
               'timestamp': '2026-11-01T10:50:23.000Z', 'version': '0.28.4', 'cwd': str(self.project),
               'message': {'cwd': '/fake/path', 'content': 'private synthetic prompt'}}
        path = self.write(f'{root}/one.jsonl', json.dumps(row).encode() + b'\n')
        self.write(f'{root}/renamed.jsonl', json.dumps(dict(row, cwd='/fake/renamed')).encode() + b'\n')
        self.write(f'{root}/managed.jsonl', b'\n'.join([
            json.dumps({'type': 'system', 'subtype': 'managed_session_header_v1'}).encode(),
            json.dumps(dict(row, sessionId='managed', cwd='/remote/cwd')).encode(),
        ]))
        self.write(f'{root}/one.runtime.json', {'cwd': '/fake/runtime'})
        cache = {}
        stores, projects = self.scan(cache=cache)
        self.assertEqual(projects, {self.project: {'Qwen Code'}})
        self.assertEqual([item[1] for item in stores], [path.parents[3]])
        self.assertNotIn('private synthetic prompt', json.dumps(cache))
        self.assertNotIn('/remote/cwd', json.dumps(cache))
        other = self.home / 'Projects/replacement'
        path.write_text(json.dumps(dict(row, cwd=str(other))) + '\n')
        self.assertEqual(self.scan(cache=cache)[1], {other: {'Qwen Code'}})

    def test_qwen_read_is_bounded_and_scope_checked(self):
        target = self.base / 'qwen-runtime'
        path = target / 'projects/example/chats/one.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            'uuid': 'record', 'parentUuid': None, 'sessionId': 'one', 'type': 'user',
            'timestamp': '2026-11-01T10:50:23.000Z', 'version': '0.28.4', 'cwd': str(self.project),
        }) + '\n' + 'x' * (1024 * 1024))
        with patch.dict(os.environ, {'QWEN_RUNTIME_DIR': str(target)}):
            self.assertEqual(self.scan(), ([], {}))
            self.assertEqual(self.scan(authorized=(target,))[1], {self.project: {'Qwen Code'}})

    def test_qwen_uses_global_settings_runtime_directory_within_scan_scope(self):
        settings = self.write('.qwen/settings.json', {
            'advanced': {'runtimeOutputDir': '~/AgentState/qwen'},
        })
        path = self.write('AgentState/qwen/projects/example/chats/one.jsonl', json.dumps({
            'uuid': 'record', 'parentUuid': None, 'sessionId': 'one', 'type': 'user',
            'timestamp': '2026-11-01T10:50:23.000Z', 'version': '0.28.4', 'cwd': str(self.project),
        }).encode() + b'\n')
        stores, projects = self.scan()
        self.assertEqual([row[1] for row in stores], [settings.parent, path.parents[2]])
        self.assertEqual(projects, {self.project: {'Qwen Code'}})

    def test_goose_reads_working_directory_read_only_and_excludes_containers(self):
        root = self.home / 'Library/Application Support/Block/goose'
        path = root / 'sessions/sessions.db'
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, working_dir TEXT, container_id TEXT, name TEXT)')
            db.executemany('INSERT INTO sessions VALUES (?, ?, ?, ?)', [
                ('local', str(self.project), None, '/fake/from/name'),
                ('container', '/remote/container', 'container', 'foreign'),
                ('relative', 'relative/path', None, 'relative'),
            ])
        before = path.read_bytes()
        cache = {}
        stores, projects = self.scan(cache=cache)
        self.assertEqual([row[1] for row in stores], [root, path.parent])
        self.assertEqual(projects, {self.project: {'goose'}})
        self.assertNotIn('/fake/from/name', json.dumps(cache))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_goose_authorized_override_scopes_reads_to_the_data_store(self):
        target = self.base / 'goose'
        path = target / 'data/sessions/sessions.db'
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, working_dir TEXT)')
            db.execute('INSERT INTO sessions VALUES (?, ?)', ('fixture', str(self.project)))
        with patch.dict(os.environ, {'GOOSE_PATH_ROOT': str(target)}):
            self.assertEqual(self.scan(), ([], {}))
            stores, projects = self.scan(authorized=(path.parent,))
        self.assertEqual([row[1] for row in stores], [path.parent])
        self.assertEqual(projects, {self.project: {'goose'}})

    def test_goose_reports_wal_coverage_and_keeps_uncheckpointed_data_unmodified(self):
        path = self.home / 'Library/Application Support/Block/goose/sessions/sessions.db'
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, working_dir TEXT)')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            with db:
                db.execute('INSERT INTO sessions VALUES (?, ?)', ('fixture', str(self.project)))
            wal = path.with_name(path.name + '-wal')
            before = wal.read_bytes()
            self.assertTrue(before)
            cache = {}
            issues = set()
            self.assertEqual(self.scan(issues=issues, cache=cache)[1], {})
            self.assertTrue(any('主库' in issue for issue in issues))
            self.assertEqual(wal.read_bytes(), before)
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.assertEqual(self.scan(cache=cache)[1], {self.project: {'goose'}})


if __name__ == '__main__':
    unittest.main()
