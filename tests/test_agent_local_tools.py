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
            'GOOSE_PATH_ROOT', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME',
            'CONTINUE_GLOBAL_DIR', 'DSH_HOME', 'PI_CODING_AGENT_DIR', 'PI_CODING_AGENT_SESSION_DIR',
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

    def test_amp_reads_only_legacy_thread_project_fields_and_revalidates_changes(self):
        threads = '.local/share/amp/threads'
        ids = [f'T-0199aaaa-bbbb-7ccc-8ddd-eeeeffff000{index}' for index in range(7)]
        local = self.project
        alternate = self.home / '另一个项目 🎬'
        private = 'private-prompt-marker'

        def thread(identifier, uri, *, client='CLI', **extra):
            return {'id': identifier, 'v': 42, 'created': 1768178184664,
                    'env': {'initial': {'platform': {'client': client}, 'trees': [{'uri': uri}]}},
                    'messages': [{'role': 'user', 'content': private}], **extra}

        marker = self.write(f'{threads}/{ids[0]}.json', thread(ids[0], local.as_uri()))
        self.write(f'{threads}/{ids[1]}.json', thread(ids[0], alternate.as_uri()))
        self.write(f'{threads}/{ids[2]}.json', thread(ids[2], 'file://server/Users/fake/project'))
        self.write(f'{threads}/{ids[3]}.json', thread(ids[3], alternate.as_uri(), executor='orb'))
        self.write(f'{threads}/{ids[4]}.json', thread(ids[4], alternate.as_uri(), client='Web'))
        self.write(f'{threads}/{ids[5]}.json', thread(ids[5], alternate.as_uri(), extra_text='x' * (256 * 1024)))
        (marker.parent / f'{ids[6]}.json').symlink_to(marker)
        config = self.write('.config/amp/settings.jsonc', b'private settings')
        secrets = self.write('.local/share/amp/secrets.json', b'private credentials')
        cache = {}
        stores, projects = self.scan(cache=cache)
        self.assertEqual(projects, {local: {'Amp'}})
        self.assertEqual({path for agent, path, _ in stores if agent == 'Amp'}, {config.parent, secrets.parent})
        self.assertNotIn(private, json.dumps(cache))
        self.assertNotIn(str(secrets), cache['readers']['discover_amp']['stamps'])
        self.write(marker, thread(ids[0], alternate.as_uri()))
        self.assertEqual(self.scan(cache=cache)[1], {alternate: {'Amp'}})
        self.assertEqual(self.scan(excluded=(marker,), cache=cache)[1], {})

    def test_amp_legacy_xdg_storage_requires_explicit_scope(self):
        external = self.base / 'external-data'
        identifier = 'T-0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001'
        marker = self.write(external / 'amp/threads' / f'{identifier}.json', {
            'id': identifier, 'v': 1, 'created': 1768178184664,
            'env': {'initial': {'platform': {'client': 'CLI'}, 'trees': [{'uri': self.project.as_uri()}]}},
        })
        cache = {}
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(external)}):
            self.assertEqual(self.scan(cache=cache)[1], {})
            stores, projects = self.scan(cache=cache, authorized=(external,))
            self.assertEqual(projects, {self.project: {'Amp'}})
            self.assertIn(('Amp', marker.parents[1], 'Amp CLI 会话、凭证和本地数据'), stores)
            self.assertEqual(self.scan(cache=cache)[1], {})

    def test_conductor_requires_a_linked_git_worktree_marker_and_refreshes_edits(self):
        root = 'conductor/workspaces/film'
        workspace = self.home / root / '剪辑'
        metadata = self.home / 'Projects/main/.git/worktrees/剪辑'
        marker = self.write(workspace / '.git', f'gitdir: {metadata}\n'.encode())
        notes = self.write(workspace / '.context/plan.md', b'private context')
        chat = self.write('Library/Application Support/com.conductor.app/chats.sqlite', b'private chat')
        self.write(f'{root}/ordinary/readme.txt', b'not a worktree')
        self.write(f'{root}/clone/.git/config', b'ordinary Git repository')
        self.write(f'{root}/submodule/.git', b'gitdir: ../../.git/modules/content\n')
        self.write(f'{root}/wrong-location/nested/.git', f'gitdir: {metadata}\n'.encode())
        self.write('Projects/unrelated/.git', f'gitdir: {metadata}\n'.encode())
        alias = self.home / root / 'alias'
        alias.mkdir()
        (alias / '.git').symlink_to(marker)
        (alias.parent / 'linked-workspace').symlink_to(workspace, target_is_directory=True)

        cache = {}
        stores, projects = self.scan(cache=cache)
        self.assertEqual(projects, {workspace: {'Conductor'}})
        self.assertEqual({path for agent, path, _ in stores if agent == 'Conductor'}, {notes.parent, chat.parent})
        stamps = cache['readers']['_conductor']['stamps']
        self.assertIn(str(marker), stamps)
        self.assertNotIn(str(metadata), stamps)
        self.assertNotIn('private chat', json.dumps(cache))
        marker.write_bytes(b'gitdir: /some/where/invalid\n')
        self.assertEqual(self.scan(cache=cache)[1], {})
        marker.write_bytes(f'gitdir: {metadata}\n'.encode())
        self.assertEqual(self.scan(cache=cache)[1], {workspace: {'Conductor'}})
        self.assertEqual(self.scan(cache=cache, excluded=(marker,))[1], {})

    def test_deepseek_reads_only_registered_stable_workspaces_and_revalidates_changes(self):
        active = '49fb9c05-a1c4-419e-9254-a6379f88240a'
        changed = '847b2a80-72e5-4af3-a44c-f4a7e5587383'
        stale = 'ddade71c-4776-4d04-a390-769917152c17'
        registry = {
            'unit': {'name': 'workspace', 'version': 2},
            'global': {'initialized': True, 'workspaceIds': [active, changed],
                       'pendingMutation': {'operation': 'delete', 'workspaceId': changed}},
            'tables': {'workspaces': {
                active: {'path': str(self.project), 'title': 'private-title', 'sessionIds': []},
                changed: {'path': str(self.home / 'Changing'), 'sessionIds': []},
                stale: {'path': str(self.home / 'Stale'), 'sessionIds': []},
            }},
        }
        metadata = self.write('.dsh/storages/workspace.json', registry)
        session = self.write('.dsh/sessions/--project--/session/session.v4.jsonl.zstd', b'private compressed payload')
        desktop = self.write('Library/Application Support/@deepseek-ai/dsh-desktop/state.json', b'opaque')
        cache = {}
        stores, projects = self.scan(cache=cache)
        self.assertEqual(projects, {self.project: {'DeepSeek Harness'}})
        self.assertIn(('DeepSeek Harness', session.parents[3], 'DeepSeek Harness 会话、配置与插件数据'), stores)
        self.assertTrue(any(agent == 'DeepSeek Harness' and path == desktop.parent for agent, path, _ in stores))
        self.assertNotIn('private-title', json.dumps(cache))
        registry['unit']['version'] = 3
        metadata.write_text(json.dumps(registry))
        self.assertEqual(self.scan(cache=cache)[1], {})

    def test_deepseek_custom_home_requires_scope_and_does_not_follow_symlink(self):
        external = self.base / 'harness'
        external.mkdir()
        (external / 'storages').mkdir()
        workspace = '49fb9c05-a1c4-419e-9254-a6379f88240a'
        registry = external / 'storages/workspace.json'
        registry.write_text(json.dumps({
            'unit': {'name': 'workspace', 'version': 2},
            'global': {'initialized': True, 'workspaceIds': [workspace]},
            'tables': {'workspaces': {workspace: {'path': str(self.project), 'sessionIds': []}}},
        }))
        with patch.dict(os.environ, {'DSH_HOME': str(external)}):
            self.assertEqual(self.scan()[1], {})
            self.assertEqual(self.scan(authorized=(external,))[1], {self.project: {'DeepSeek Harness'}})
            source = registry.rename(external / 'untargeted.json')
            registry.symlink_to(source)
            self.assertEqual(self.scan(authorized=(external,))[1], {})

    def _deepseek_log(self, project, identifier, version, *, compressed=False, bucket=None, header_id=None):
        key = bucket or '--' + str(project).replace('/', '-').lstrip('-') + '--'
        line = (json.dumps({'type': 'session', 'version': version, 'id': header_id or identifier,
                            'createdAt': 1795610528154, 'isSeeded': False,
                            'delegationDepth': 0, 'cwd': str(project)}) + '\n').encode()
        name = f'session.v{version}.jsonl' if version else 'session.jsonl'
        if compressed:
            # A standard single-segment Zstd frame carrying one raw block.
            line = (bytes.fromhex('28b52ffda0') + len(line).to_bytes(4, 'little') +
                    (len(line) * 8 + 1).to_bytes(3, 'little') + line)
            name += '.zstd'
        return self.write(f'.dsh/sessions/{key}/{identifier}/{name}', line + b'ignored private body')

    def test_deepseek_headless_uses_only_authoritative_log_headers(self):
        log = self._deepseek_log
        legacy = self.home / 'Projects/legacy'
        log(legacy, 'session-legacy', 0)
        wrong = self.home / 'Projects/wrong'
        log(wrong, 'session-wrong', 4, header_id='foreign')
        log(wrong, 'session-misplaced', 4, bucket='--elsewhere--')
        log(wrong, 'session-newer', 2)
        log(wrong, 'session-newer', 99)
        log(wrong, 'session-mixed', 2)
        log(wrong, 'session-mixed', 4, compressed=True)
        log(wrong, 'session-duplicate', 4)
        log(self.home / 'Projects/ambiguous', 'session-duplicate', 4)
        older = log(wrong, 'session-link', 3)
        (older.parent / 'session.v4.jsonl').symlink_to(older)
        self.assertEqual(self.scan()[1], {legacy: {'DeepSeek Harness'}})

    def test_deepseek_compressed_header_is_bounded_and_revalidated(self):
        from engine.lumen.zstd_header import _decoder

        if _decoder() is None:
            self.skipTest('source checkout has no Zstandard decoder; the native package bundles one')
        current = self.home / 'Projects/current'
        first = self._deepseek_log(current, 'session-current', 4, compressed=True)
        cache = {}
        self.assertEqual(self.scan(cache=cache)[1], {current: {'DeepSeek Harness'}})
        first.write_bytes(first.read_bytes()[:12])
        self.assertEqual(self.scan(cache=cache)[1], {})
        self.assertNotIn('ignored private body', json.dumps(cache))

    def test_pi_uses_only_session_headers_for_default_and_authorized_custom_storage(self):
        header = {'type': 'session', 'version': 3, 'id': 'session-123', 'cwd': str(self.project)}
        legacy = {key: value for key, value in header.items() if key != 'version'}
        history = self.write('.pi/agent/sessions/--bucket--/fixture.jsonl',
                             json.dumps(legacy).encode() + b'\nprivate ignored body\n' * 2000)
        self.write('.pi/agent/sessions/--bucket--/unknown.jsonl',
                   (json.dumps(dict(header, version=99, cwd=str(self.home / 'Unknown'))) + '\n').encode())
        self.write('.pi/agent/sessions/--bucket--/message.jsonl',
                   (json.dumps(dict(header, type='message', cwd=str(self.home / 'Message'))) + '\n').encode())
        custom = self.base / 'pi-sessions'
        custom.mkdir()
        custom_project = self.home / 'Projects/Reusable'
        (custom / 'custom.jsonl').write_text(json.dumps(dict(header, cwd=str(custom_project))) + '\n')
        with patch.dict(os.environ, {'PI_CODING_AGENT_SESSION_DIR': str(custom)}):
            cache = {}
            self.assertEqual(self.scan(cache=cache)[1], {self.project: {'Pi'}})
            _, projects = self.scan(authorized=(custom,), cache=cache)
            self.assertEqual(projects, {self.project: {'Pi'}, custom_project: {'Pi'}})
            self.assertNotIn('private ignored body', json.dumps(cache))
            history.unlink()
            self.assertEqual(self.scan(authorized=(custom,), cache=cache)[1], {custom_project: {'Pi'}})

    def test_continue_associates_only_linked_local_sessions_without_reading_transcripts(self):
        from engine.lumen.agent_adapters import extension_installations

        root = '.continue/sessions'
        real = '5492b813-1f5f-42fa-8013-aa3e5584fd4c'
        removed = '7c6d075b-0226-4384-8b19-8224c6bc3915'
        ambiguous = '3c90de89-929f-4491-9ce9-a2d070213253'
        session = self.write(f'{root}/{real}.json', b'synthetic private transcript')
        self.write(f'{root}/{ambiguous}.json', b'separate transcript')
        self.write(f'{root}/sessions.json', [
            {'sessionId': real, 'workspaceDirectory': self.project.as_uri(), 'title': 'private-title'},
            {'sessionId': removed, 'workspaceDirectory': str(self.home / 'Removed')},
            {'sessionId': ambiguous, 'workspaceDirectory': str(self.home / 'Ambiguous')},
            {'sessionId': ambiguous, 'workspaceDirectory': 'vscode-remote://ssh-remote+fixture/project'},
        ])
        manifest = self.write('.vscode/extensions/continue.continue-1.3.40/package.json', {
            'publisher': 'Continue', 'name': 'continue', 'version': '1.3.40',
        })
        cache = {}
        stores, projects = self.scan(cache=cache)
        self.assertIn(('Continue', session.parent, 'Continue 本机会话存储'), stores)
        self.assertEqual(projects, {self.project: {'Continue'}})
        self.assertNotIn('private-title', json.dumps(cache))
        self.assertEqual(extension_installations(self.home), [('Continue', manifest.parent, '1.3.40')])
        session.unlink()
        self.assertEqual(self.scan(cache=cache)[1], {})

    def test_continue_custom_location_needs_scope_and_rejects_symlink_entries(self):
        external = self.base / 'continue-data'
        sessions = external / 'sessions'
        sessions.mkdir(parents=True)
        identifier = '5492b813-1f5f-42fa-8013-aa3e5584fd4c'
        (sessions / 'sessions.json').write_text(json.dumps([
            {'sessionId': identifier, 'workspaceDirectory': str(self.project)},
        ]))
        transcript = sessions / f'{identifier}.json'
        transcript.write_bytes(b'private')
        with patch.dict(os.environ, {'CONTINUE_GLOBAL_DIR': str(external)}):
            self.assertEqual(self.scan()[1], {})
            self.assertEqual(self.scan(authorized=(external,))[1], {self.project: {'Continue'}})
            transcript.unlink()
            transcript.symlink_to(sessions / 'sessions.json')
            self.assertEqual(self.scan(authorized=(external,))[1], {})

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
