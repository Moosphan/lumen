import gzip
import json
import hashlib
import os
import plistlib
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

from engine.lumen.service import dispatch


class LocalInventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.manual = self.base / "manual"
        self.manual.mkdir()

    def write(self, relative, data=b"data"):
        path = self.home / relative
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(data)
        return path

    def call(self, cmd, **args):
        return dispatch(self.state, {"cmd": cmd, **args}, local_home=self.home)

    def test_default_scan_finds_installed_apps_and_agent_projects(self):
        app = self.write("Applications/Editor.app/Contents/Info.plist",
                         plistlib.dumps({"CFBundleIdentifier": "example.editor"}))
        root = self.home / "Projects/film"
        self.write("Projects/film/render.mp4")
        session = {"type": "session_meta", "payload": {"cwd": str(root)}}
        self.write(".codex/sessions/rollout.jsonl", json.dumps(session).encode())
        self.write("Documents/Codex/2026-11-24/new-project/dist/thumb.png")
        build_app = "Projects/film/build/Runner.app/Contents/Info.plist"
        self.write(build_app, plistlib.dumps({"CFBundleIdentifier": "example.runner"}))
        report = self.call("scan_local", deep=False)
        self.assertEqual(report["roots"], [])
        self.assertEqual([r["path"] for r in report["apps"]], [str(app.parents[1])])
        self.assertGreater(report["apps"][0]["inventory_bytes"], 0)
        self.assertEqual(report["projects"][str(root)], ["Codex"])
        self.assertEqual(report["projects"][str(self.home / "Documents/Codex/2026-11-24/new-project")], ["Codex"])
        self.assertEqual(sum(r["bytes"] for r in report["categories"]), report["indexed_bytes"])

    def test_conductor_attributes_a_real_git_worktree_and_keeps_chat_and_handoff(self):
        primary = self.write('Projects/cut/source.txt', b'production source').parent
        workspace = self.home / 'conductor/workspaces/cut/剪辑'
        workspace.parent.mkdir(parents=True)
        env = dict(PATH=os.defpath, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

        def git(*args):
            subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-C', str(primary), *args],
                           env=env, capture_output=True, check=True)

        git('init', '-q')
        git('add', 'source.txt')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'fixture')
        git('worktree', 'add', '-q', '--detach', str(workspace))
        tracked = workspace / 'source.txt'
        notes = self.write(workspace / '.context/notes.md', b'private project handoff')
        generated = self.write(workspace / 'build/preview.mov', b'preview')
        chat = self.write('Library/Application Support/com.conductor.app/chats.sqlite', b'private local conversation')
        self.write('Applications/Conductor.app/Contents/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': 'com.conductor.app', 'CFBundleShortVersionString': '0.99.1',
        }))
        self.write('.codex/sessions/worktree.jsonl', json.dumps({
            'type': 'session_meta', 'payload': {'cwd': str(workspace)},
        }).encode())

        manual = self.call('scan', roots=[str(notes.parent), str(chat.parent)], deep=False)
        self.assertEqual({row['path']: row['decision'] for row in manual['files']},
                         {str(notes): 'keep', str(chat): 'keep'})
        self.call('settings', roots=[])

        report = self.call('scan_local', startup=True)
        self.assertEqual(report['projects'].get(str(workspace)), ['Codex', 'Conductor'])
        self.assertNotIn(str(primary), report['projects'])
        item = next(row for row in report['agent_inventory'] if row['name'] == 'Conductor')
        self.assertEqual(item['kind'], 'workspace-manager')
        self.assertTrue(item['installed'])
        self.assertGreater(item['bytes'], 0)
        rows = {row['path']: row for row in report['files']}
        self.assertEqual(rows[str(notes)]['decision'], 'keep')
        self.assertEqual(rows[str(chat)]['decision'], 'keep')
        self.assertEqual(rows[str(tracked)]['decision'], 'defer')
        self.assertEqual(rows[str(generated)]['decision'], 'defer')
        protected = [notes, chat, tracked, generated]
        result = self.call('run', paths=list(map(str, protected)), scan_id=report['scan_id'], action='quarantine')
        self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(protected))
        self.assertTrue(all(path.is_file() for path in protected))

    def test_aider_discovers_local_project_and_protects_only_recognized_aider_data(self):
        header = b"\n# aider chat started at 2026-11-25 11:21:05\n\n"
        log = self.write("Work/团队/film/.aider.chat.history.md", header + b"synthetic-private-text")
        prompt = self.write("Work/团队/film/.aider.input.history", b"synthetic prompt")
        tags = self.write("Work/团队/film/.aider.tags.cache.v4/entry.dat", b"synthetic tags")
        source = self.write("Work/团队/film/reference.md", b"synthetic source")
        self.write("Projects/reserved/.aider.chat.history.md", b"not an Aider header")
        self.write("Projects/settings-only/.aider.conf.yml", b"model: fixture")
        self.write("Projects/node_modules/dependency/.aider.chat.history.md", header)
        self.write("Library/Caches/example/.aider.chat.history.md", header)
        link = self.home / "Projects/Link"
        link.symlink_to(log.parent, target_is_directory=True)

        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"], {str(log.parent): ["Aider"]})
        files = {row["path"]: row for row in report["files"]}
        for item in (log, prompt, tags):
            self.assertIn("Aider", files[str(item)]["agents"])
            self.assertEqual(files[str(item)]["decision"], "keep")
        self.assertEqual(files[str(source)]["projects"], [str(log.parent)])
        self.assertNotIn("synthetic-private-text", (self.state / "workspace.json").read_text())

    def test_amp_discovers_configured_projects_and_preserves_config_and_credentials(self):
        project = self.write('Projects/字幕/source.txt').parent
        config = self.write(project / '.amp/settings.jsonc', b'{ // comment\n "amp.showCosts": true,\n}')
        self.write('Projects/unrelated/.amp/settings.json', b'{"arbitrary":true}')
        credential = self.write('.local/share/amp/secrets.json', b'private credential')
        support = self.write('.config/amp/settings.json', b'{"amp.showCosts": true}')
        self.write('Applications/Amp.app/Contents/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': 'com.ampcode.amp.macos',
        }))
        manual = self.call('scan', roots=[str(config.parent), str(credential.parent), str(support.parent)], deep=False)
        self.assertEqual({row['decision'] for row in manual['files']}, {'keep'})
        self.call('settings', roots=[])

        report = self.call('scan_local', startup=True)
        self.assertEqual(report['projects'].get(str(project)), ['Amp'])
        self.assertTrue(next(row for row in report['agent_inventory'] if row['name'] == 'Amp')['installed'])
        self.assertNotIn(str(self.home / 'Projects/unrelated'), report['projects'])
        paths = [str(config), str(credential), str(support)]
        result = self.call('run', paths=paths, scan_id=report['scan_id'], action='quarantine')
        self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(paths))
        config.write_text('{}')
        self.assertNotIn(str(project), self.call('scan_local', startup=True)['projects'])

    def test_kiro_cli_and_kas_share_a_family_and_keep_local_session_data(self):
        project = self.write('Projects/Kiro Film/source.txt').parent
        kas = self.write('.kiro/sessions/0123456789abcdef/sess_fixture/session.json', json.dumps({
            'id': 'sess_fixture', 'workspacePaths': [str(project)],
        }).encode())
        legacy = self.write('.kiro/sessions/cli/fixture.json', json.dumps({
            'cwd': str(project), 'session_state': {},
        }).encode())
        transcript = self.write('.kiro/sessions/cli/fixture.jsonl', b'fixture transcript')
        credential = self.write('Library/Application Support/kiro-cli/settings/token.tmp', b'fixture credential')
        protected = [kas, legacy, transcript, credential]
        manual = self.call('scan', roots=[str(kas.parents[2]), str(credential.parents[1])], deep=False)
        self.assertEqual({row['path']: row['decision'] for row in manual['files']},
                         {str(path): 'keep' for path in protected})
        self.call('settings', roots=[])

        report = self.call('scan_local', startup=True)
        self.assertEqual(report['projects'][str(project)], ['Kiro', 'Kiro CLI'])
        agents = {row['name']: row for row in report['agent_inventory']}
        self.assertEqual({agents[name]['family'] for name in ('Kiro', 'Kiro CLI')}, {'Kiro'})
        self.assertEqual(agents['Kiro CLI']['kind'], 'cli')
        self.assertGreater(agents['Kiro CLI']['bytes'], 0)
        result = self.call('run', paths=list(map(str, protected)), scan_id=report['scan_id'], action='quarantine')
        self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(protected))
        self.assertTrue(all(path.is_file() for path in protected))

    def test_kiro_custom_session_storage_is_protected_in_manual_only_scan(self):
        custom = self.base / 'Custom Kiro'
        transcript = self.write(custom / 'sessions/cli/fixture.jsonl', b'fixture transcript')
        credential = self.write(custom / 'settings/fixture.tmp', b'fixture credential')
        with patch.dict(os.environ, {'KIRO_HOME': str(custom)}):
            report = self.call('scan', roots=[str(custom)], deep=True)
        self.assertEqual({row['path']: row['decision'] for row in report['files']},
                         {str(transcript): 'keep', str(credential): 'keep'})

    def test_trae_retains_profile_state_in_manual_and_automatic_inventory(self):
        project = self.write('Projects/Trae Film/source.ts').parent
        editor = 'Library/Application Support/Trae CN'
        metadata = self.write(f'{editor}/User/workspaceStorage/fixture/workspace.json',
                              json.dumps({'folder': project.as_uri()}).encode())
        snapshot = self.write(f'{editor}/ModularData/ai-agent/snapshot/work/preview.tmp', b'private editor changes')
        config = self.write(f'{editor}/storage.json', b'private account state')
        extension = self.write('.trae-cn/extensions/fixture/output.tmp', b'private extension state')
        self.write(extension.parent / '.git', b'gitdir: fixture')
        self.write('Applications/Trae.app/Contents/Info.plist', plistlib.dumps({'CFBundleIdentifier': 'com.trae.app'}))
        protected = [metadata, snapshot, config, extension]
        report = self.call('scan', roots=[str(self.home / editor), str(self.home / '.trae-cn')], deep=True)
        self.assertEqual({row['path']: row['decision'] for row in report['files']},
                         {str(path): 'keep' for path in protected})
        self.assertNotIn(str(extension.parent), report['projects'])

        self.call('settings', roots=[])
        report = self.call('scan_local', startup=True)
        self.assertEqual(report['projects'][str(project)], ['Trae'])
        agent, = report['agent_inventory']
        self.assertEqual((agent['family'], agent['kind'], agent['installed']), ('Trae', 'ide', True))
        self.assertGreater(agent['bytes'], 0)
        result = self.call('run', paths=list(map(str, protected)), scan_id=report['scan_id'], action='quarantine')
        self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(protected))

    def test_traework_inventories_managed_projects_and_retains_outputs_and_runtime(self):
        profile = self.home / 'Library/Application Support/TRAE SOLO CN'
        project = profile / 'ModularData/ai-agent/work-mode-projects/6a4db2ef0f25b4742bf9cdc2'
        output = self.write(project / 'dist/preview.html', b'Keep this finished document.')
        disk = self.write(profile / 'ModularData/ai-agent/vm/vms/workspace.img', b'Keep this VM.')
        state = self.write(profile / 'ModularData/ai-agent/database.db', b'Keep this conversation.')
        self.write(project / '.git', b'gitdir: synthetic fixture')
        self.write('Applications/TRAE SOLO CN.app/Contents/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': 'cn.trae.solo.app', 'CFBundleName': 'TRAE SOLO CN',
        }))
        self.write('.trae-cn/settings.json', b'potentially shared configuration')
        protected = [output, disk, state]
        manual = self.call('scan', roots=[str(profile)], deep=True)
        self.assertEqual({row['path']: row['decision'] for row in manual['files']},
                         {str(path): 'keep' for path in protected})
        self.assertFalse(manual['projects'])
        self.call('settings', roots=[])
        report = self.call('scan_local', startup=True)
        self.assertEqual(report['projects'].get(str(project)), ['TraeWork'])
        agent = next(row for row in report['agent_inventory'] if row['name'] == 'TraeWork')
        self.assertEqual((agent['kind'], agent['family'], agent['installed']), ('desktop', 'Trae', True))
        self.assertIn(str(project), agent['projects'])
        stores = {row['path']: row for row in agent['stores']}
        self.assertGreater(stores[str(disk.parents[1])]['bytes'], 0)
        rows = {row['path']: row for row in report['files']}
        self.assertEqual(rows[str(output)]['projects'], [str(project)])
        self.assertEqual(rows[str(self.home / '.trae-cn/settings.json')]['agents'], ['Trae'])
        self.assertEqual([rows[str(path)]['decision'] for path in protected], ['keep'] * len(protected))
        result = self.call('run', paths=list(map(str, protected)), scan_id=report['scan_id'], action='quarantine')
        self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(protected))

    def test_trae_inventories_managed_work_projects_and_preserves_migration_state(self):
        identity = '6a4db2ef0f25b4742bf9cdc2'
        relative = f'ModularData/ai-agent/work-mode-projects/{identity}'
        profile = self.home / 'Library/Application Support/Trae CN'
        project = profile / relative
        delivery = self.write(project / 'presentation.html', b'preserve migrated deliverable')
        disk = self.write(profile / 'ModularData/ai-agent/vm/vms/workspace.img', b'preserve fused VM')
        legacy = self.home / 'Library/Application Support/TRAE SOLO CN' / relative
        marker = self.write(legacy / f'TRAE_PROJECT_MOVED_{identity}.md', (
            f'This project was migrated to:\n\n{project}\n\nUse the target directory above. '
            'This directory only records migration state.\n'
        ).encode())
        report = self.call('scan_local', startup=True)
        self.assertEqual(report['projects'].get(str(project)), ['Trae'])
        self.assertNotIn(str(legacy), report['projects'])
        by_path = {row['path']: row for row in report['files']}
        self.assertEqual(by_path[str(delivery)]['projects'], [str(project)])
        self.assertEqual(by_path[str(marker)]['agents'], ['TraeWork'])
        candidates = [delivery, disk, marker]
        self.assertEqual([by_path[str(path)]['decision'] for path in candidates], ['keep'] * len(candidates))
        agent = next(row for row in report['agent_inventory'] if row['name'] == 'Trae')
        self.assertTrue(any(row['path'] == str(disk.parents[1]) and row['bytes'] > 0 for row in agent['stores']))
        result = self.call('run', paths=list(map(str, candidates)), scan_id=report['scan_id'], action='quarantine')
        self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(candidates))

    def test_trae_cli_shares_ide_family_and_preserves_custom_history_and_state(self):
        project = self.write('Projects/Trae Film/source.ts').parent
        self.write('Library/Application Support/Trae/User/workspaceStorage/fixture/workspace.json',
                   json.dumps({'folder': project.as_uri()}).encode())
        custom = self.base / 'TRAE Private State'
        session_id = '019fbcca-9fd4-7d20-83dc-0762b2f839b0'
        rollout = self.write(custom / f'sessions/2026/11/26/rollout-2026-11-26T11-22-33-{session_id}.jsonl',
                             json.dumps({'type': 'session_meta', 'payload': {
                                 'id': session_id, 'cwd': str(project),
                             }}).encode() + b'\nprivate session body')
        history = self.write(custom / 'history.jsonl', b'private prompts')
        credential = self.write(custom / 'auth.json', b'private account')
        wal = self.write(custom / 'thread_history_1.sqlite-wal', b'private state')
        different = self.write(custom / 'other.tmp', b'unrelated')
        protected = [rollout, history, credential, wal]
        with patch.dict(os.environ, {'TRAECLI_HOME': str(custom), 'TRAE_HOME': '', 'TRAEX_HOME': ''}):
            manual = self.call('scan', roots=[str(custom)], deep=True)
            decisions = {row['path']: row['decision'] for row in manual['files']}
            self.assertEqual([decisions[str(path)] for path in protected], ['keep'] * len(protected))
            self.assertEqual(decisions[str(different)], 'review')
            report = self.call('scan_local', startup=True)
            self.assertEqual(report['projects'][str(project)], ['Trae', 'Trae CLI'])
            agents = {row['name']: row for row in report['agent_inventory']}
            self.assertEqual(agents['Trae CLI']['family'], agents['Trae']['family'])
            self.assertEqual(agents['Trae CLI']['kind'], 'cli')
            self.assertGreater(agents['Trae CLI']['bytes'], 0)
            result = self.call('run', paths=list(map(str, protected)), scan_id=report['scan_id'], action='quarantine')
        self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(protected))
        self.assertTrue(all(path.is_file() for path in protected))

    def test_trae_cli_attributes_protected_database_files_without_claiming_other_custom_files(self):
        project = self.write('Projects/Dub/src.ts').parent
        home = self.base / 'Custom TRAE CLI'
        home.mkdir()
        database = home / 'state_5.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                'CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT NOT NULL, rollout_path TEXT NOT NULL, '
                'source TEXT NOT NULL, updated_at INTEGER NOT NULL)'
            )
            connection.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?)', (
                '019fbcca-9fd4-7d20-83dc-0762b2f839b0', str(project), '', 'cli', 1,
            ))
            connection.commit()
        history = self.write(home / 'thread_history_1.sqlite', b'opaque history fixture')
        ordinary = self.write(home / 'unrelated.txt', b'user file')
        with patch.dict(os.environ, {'TRAECLI_HOME': str(home)}):
            self.call('scan', roots=[str(home)], deep=False)
            report = self.call('scan_local', startup=True)
            self.assertEqual(report['projects'].get(str(project)), ['Trae CLI'])
            rows = {row['path']: row for row in report['files']}
            self.assertEqual(rows[str(ordinary)]['decision'], 'review')
            self.assertFalse(rows[str(ordinary)]['agents'])
            for path in (database, history):
                self.assertEqual(rows[str(path)]['agent_store_path'], str(path))
                self.assertEqual(rows[str(path)]['decision'], 'keep')
            item = next(row for row in report['agent_inventory'] if row['name'] == 'Trae CLI')
            stores = {row['path']: row for row in item['stores']}
            self.assertEqual(stores[str(database)]['count'], 1)
            self.assertEqual(stores[str(history)]['count'], 1)
            result = self.call('run', paths=[str(database), str(history)], scan_id=report['scan_id'],
                               action='quarantine')
            self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped', 'skipped'])

    def test_oh_my_pi_does_not_double_count_the_shared_pi_directory_setting(self):
        primary = self.write('Projects/OMP workspace/source.ts').parent
        historical = self.write('Projects/OMP historical workspace/source.ts').parent
        original_pi = self.write('Projects/Pi workspace/source.ts').parent
        agent_dir = self.home / '.omp/custom-agent'
        session_root = agent_dir / 'custom-sessions'
        sid = '019c625b-b900-7000-8000-000000000001'
        timestamp = '2026-11-26T20:40:10.000Z'
        session = self.write(session_root / ('2026-11-26T20-40-10-000Z_' + sid + '.jsonl'), (
            json.dumps(dict(type='session', version=3, id=sid, timestamp=timestamp, cwd=str(primary))) + '\n'
        ).encode())
        self.write('.pi/agent/sessions/session.jsonl', (
            json.dumps(dict(type='session', version=3, id='separate-pi', cwd=str(original_pi))) + '\n'
        ).encode())
        credentials = self.write(agent_dir / 'auth.json', b'synthetic credentials')
        archive_root = agent_dir / 'archive/sessions'
        archived_id = '019c625b-b900-7000-8000-000000000002'
        archive = self.write(archive_root / '-Projects-OMP-historical' /
                             ('2026-11-26T20-40-10-000Z_' + archived_id + '.jsonl.gz'),
                             gzip.compress((json.dumps(dict(type='session', version=3, id=archived_id,
                                                            timestamp=timestamp, cwd=str(historical))) + '\n').encode()))
        worktree = self.write('.omp/wt/synthetic/source.ts', b'synthetic worktree')
        originals = {path: path.read_bytes() for path in (session, credentials, archive, worktree)}
        with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(agent_dir),
                                     'PI_CODING_AGENT_SESSION_DIR': str(session_root)}):
            manual = self.call('scan', roots=[str(self.home / '.omp')], deep=True)
            manual_files = {row['path']: row for row in manual['files']}
            self.assertTrue(all(manual_files[str(path)]['decision'] == 'keep' for path in originals))
            report = self.call('scan_local', startup=True)
            self.assertEqual(report['projects'].get(str(primary)), ['Oh My Pi'])
            self.assertEqual(report['projects'].get(str(historical)), ['Oh My Pi'])
            self.assertEqual(report['projects'].get(str(original_pi)), ['Pi'])
            tools = {row['name']: row for row in report['agent_inventory']}
            self.assertEqual(tools['Oh My Pi']['kind'], 'cli')
            self.assertNotIn(str(session_root), {store['path'] for store in tools['Pi']['stores']})
            rows = {row['path']: row for row in report['files']}
            self.assertTrue(all(rows[str(path)]['agents'] == ['Oh My Pi'] for path in originals))
            self.assertEqual(rows[str(archive)]['agent_store_path'], str(archive_root))
            self.assertEqual(rows[str(archive)]['decision'], 'keep')
            action = self.call('run', paths=list(map(str, originals)), scan_id=report['scan_id'], action='quarantine')
            self.assertEqual([row['status'] for row in action['last_action']['items']], ['skipped'] * len(originals))
            self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_openhands_inventories_native_canvas_and_legacy_cli_without_reusing_remote_paths(self):
        canvas = self.home / '.openhands/agent-canvas'
        workspace = self.write('Projects/Canvas project/source.py').parent
        legacy = self.write('Projects/Legacy project/main.py').parent
        alternate = self.write('Projects/New canvas project/source.py').parent
        unrelated = self.write('Projects/Unrelated/source.py').parent
        sid = '0196af9c754f430799ef39aa868a6700'
        managed = self.write(canvas / 'workspaces' / sid / 'delivery.zip', b'synthetic project').parent

        def entry(identity, project, *, kind='LocalWorkspace'):
            return json.dumps({
                'id': identity, 'workspace': {'kind': kind, 'working_dir': str(project)},
                'title': 'PRIVATE_OPENHANDS_TITLE',
            }).encode()

        meta = self.write(canvas / f'dev_conversations/{sid}/meta.json', entry(sid, workspace))
        event = self.write(meta.parent / 'events/event-00000.json', b'PRIVATE_OPENHANDS_EVENT')
        managed_id = '0196af9c754f430799ef39aa868a6701'
        self.write(canvas / f'dev_conversations/{managed_id}/meta.json', entry(managed_id, managed))
        legacy_id = '868ce027-f044-4dbd-b1bd-db264d48a88b'
        cli = self.write(f'.openhands/conversations/{legacy_id.replace("-", "")}/base_state.json',
                         entry(legacy_id, legacy))
        remote_id = '0196af9c754f430799ef39aa868a6702'
        self.write(canvas / f'dev_conversations/{remote_id}/meta.json',
                   entry(remote_id, unrelated, kind='RemoteWorkspace'))
        invalid = self.write(canvas / 'dev_conversations/0196af9c754f430799ef39aa868a6703/meta.json',
                             entry(sid, unrelated))
        docker = self.write(canvas / f'conversations/{sid}/meta.json', entry(sid, unrelated))
        linked_id = '0196af9c754f430799ef39aa868a6704'
        linked = self.write(canvas / f'dev_conversations/{linked_id}/meta.json', entry(linked_id, unrelated))
        linked.symlink_to(linked.rename(self.base / 'linked-meta.json'))
        oversized_id = '0196af9c754f430799ef39aa868a6705'
        self.write(canvas / f'dev_conversations/{oversized_id}/meta.json',
                   entry(oversized_id, unrelated) + b' ' * (128 * 1024))
        state = self.write('Library/Application Support/OpenHands Agent Canvas/Local State', b'synthetic app data')
        secrets = self.write('.openhands/secrets.json', b'PRIVATE_OPENHANDS_CREDENTIAL')
        app = self.write('Applications/OpenHands Agent Canvas.app/Contents/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': 'dev.openhands.agent-canvas', 'CFBundleName': 'OpenHands Agent Canvas',
        })).parents[1]
        originals = {p: p.read_bytes() for p in (event, cli, invalid, docker, state, secrets, managed / 'delivery.zip')}
        manual = self.call('scan', roots=[str(self.home / '.openhands'), str(state.parent)], deep=True)
        manual_rows = {row['path']: row for row in manual['files']}
        self.assertTrue(all(manual_rows[str(p)]['decision'] == 'keep' for p in originals))

        report = self.call('scan_local', startup=True)
        expected = {str(workspace): ['OpenHands Agent Canvas'], str(managed): ['OpenHands Agent Canvas'],
                    str(legacy): ['OpenHands CLI']}
        self.assertEqual(report['projects'], expected)
        agents = {row['name']: row for row in report['agent_inventory']}
        canvas_row, cli_row = agents['OpenHands Agent Canvas'], agents['OpenHands CLI']
        self.assertEqual(canvas_row['installations'][0]['path'], str(app))
        self.assertEqual(canvas_row['kind'], 'desktop')
        self.assertEqual(cli_row['kind'], 'cli')
        self.assertEqual(canvas_row['family'], cli_row['family'])
        self.assertFalse(cli_row['installed'])
        rows = {row['path']: row for row in report['files']}
        self.assertEqual(rows[str(event)]['agent_store_path'], str(canvas / 'dev_conversations'))
        self.assertEqual(rows[str(cli)]['agent_store_path'], str(self.home / '.openhands/conversations'))
        self.assertTrue(all(rows[str(p)]['decision'] == 'keep' for p in originals))
        snapshot = (self.state / 'workspace.json').read_text()
        self.assertNotIn('PRIVATE_OPENHANDS_TITLE', snapshot)
        self.assertNotIn('PRIVATE_OPENHANDS_EVENT', snapshot)
        self.assertNotIn('PRIVATE_OPENHANDS_CREDENTIAL', snapshot)

        meta.write_bytes(entry(sid, alternate))
        refreshed = self.call('scan_local', startup=True)
        expected[str(alternate)] = expected.pop(str(workspace))
        self.assertEqual(refreshed['projects'], expected)
        result = self.call('run', paths=list(map(str, originals)), scan_id=refreshed['scan_id'], action='quarantine')
        self.assertTrue(all(item['status'] == 'skipped' for item in result['last_action']['items']))
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)

    def test_vibe_custom_session_root_keeps_verified_data_without_claiming_its_neighbors(self):
        project = self.write('Projects/Vibe Project/source.py').parent
        sessions = self.base / 'Custom Vibe Sessions'
        session_root = sessions / 'session_20261126_201001_7fac124f'
        meta = self.write(session_root / 'meta.json', json.dumps({
            'session_id': '7fac124f-4a91-4f0e-9e68-a321432060f2',
            'environment': {'working_directory': str(project)}, 'total_messages': 1,
        }).encode())
        transcript = self.write(session_root / 'messages.jsonl', b'opaque synthetic transcript')
        outside = self.write(sessions / 'unrelated.mov', b'ordinary material')
        custom_home = self.home / 'Vibe Custom Home'
        key = self.write(custom_home / '.env', b'MISTRAL_API_KEY=synthetic')
        worktree_file = self.write(custom_home / 'worktrees/source-project/worktree/output.mov', b'working output')
        default_history = self.write('.vibe/logs/session/older.json', b'opaque legacy data')
        with patch.dict(os.environ, {'VIBE_HOME': str(custom_home), 'VIBE_SESSION_LOGGING__SAVE_DIR': str(sessions)}):
            manual = self.call('scan', roots=[str(default_history.parent)], deep=True)
            self.assertEqual(next(row for row in manual['files'] if row['path'] == str(default_history))['decision'], 'keep')
            unauthorized = self.call('scan_local', startup=True)
            self.assertNotIn(str(project), unauthorized['projects'])
            self.call('scan', roots=[str(sessions)], deep=False)
            report = self.call('scan_local', startup=True)
            self.assertEqual(report['projects'].get(str(project)), ['Mistral Vibe'])
            agent = next(row for row in report['agent_inventory'] if row['name'] == 'Mistral Vibe')
            self.assertEqual(agent['kind'], 'cli')
            self.assertIn(str(session_root), {store['path'] for store in agent['stores']})
            self.assertNotIn(str(sessions), {store['path'] for store in agent['stores']})
            files = {row['path']: row for row in report['files']}
            self.assertFalse(files[str(outside)]['agents'])
            originals = {path: path.read_bytes() for path in (meta, transcript, key, worktree_file, default_history)}
            self.assertTrue(all(files[str(path)]['decision'] == 'keep' for path in originals))
            action = self.call('run', paths=list(map(str, originals)), scan_id=report['scan_id'], action='quarantine')
            self.assertEqual([row['status'] for row in action['last_action']['items']], ['skipped'] * len(originals))
            self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_hermes_custom_profile_is_scoped_and_preserves_state_and_credentials(self):
        project = self.write('Projects/Hermes Project/source.py').parent
        home = self.base / 'Custom Hermes'
        home.mkdir()
        database = home / 'state.db'
        with closing(sqlite3.connect(database)) as db:
            db.executescript('''
                CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, cwd TEXT);
                CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
            ''')
            db.execute('INSERT INTO sessions VALUES (?, ?, ?)', ('session', 'cli', str(project)))
            db.execute('INSERT INTO state_meta VALUES (?, ?)', ('gateway.local_policy.v1:session', json.dumps({
                'session_id': 'session', 'entry': {'session_id': 'session'},
                'policy': {'cwd': str(project), 'terminal_json': json.dumps({'TERMINAL_CWD': str(project)})},
            })))
            db.commit()
        credentials = self.write(home / 'auth.json', b'synthetic credentials')
        ordinary = self.write(home / 'ordinary.txt', b'user file')
        history = self.write('.hermes/sessions/session.jsonl', b'{ "body": "synthetic transcript" }')
        plist = self.write('Applications/Hermes.app/Contents/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': 'com.nousresearch.hermes', 'CFBundleShortVersionString': '0.23.4',
        }))
        with patch.dict(os.environ, {'HERMES_HOME': str(home)}):
            manual = self.call('scan', roots=[str(history.parent)], deep=True)
            self.assertEqual(next(row for row in manual['files'] if row['path'] == str(history))['decision'], 'keep')
            unauthorized = self.call('scan_local', startup=True)
            self.assertNotIn(str(project), unauthorized['projects'])
            self.call('scan', roots=[str(home)], deep=False)
            report = self.call('scan_local', startup=True)
            self.assertEqual(report['projects'].get(str(project)), ['Hermes Agent'])
            agent = next(row for row in report['agent_inventory'] if row['name'] == 'Hermes Agent')
            self.assertTrue(agent['installed'])
            self.assertEqual(agent['installations'][0]['path'], str(plist.parents[1]))
            rows = {row['path']: row for row in report['files']}
            self.assertFalse(rows[str(ordinary)]['agents'])
            protected = (database, credentials, history)
            originals = {path: path.read_bytes() for path in protected}
            self.assertTrue(all(rows[str(path)]['decision'] == 'keep' for path in protected))
            action = self.call('run', paths=list(map(str, protected)), scan_id=report['scan_id'], action='quarantine')
            self.assertEqual([row['status'] for row in action['last_action']['items']], ['skipped'] * len(protected))
            self.assertEqual({path: path.read_bytes() for path in protected}, originals)

    def test_zed_app_and_thread_stores_preserve_data_in_automatic_and_manual_scans(self):
        project = self.write('Projects/Zed Project/source.swift').parent
        bundle = self.write('Applications/Zed Preview.app/Contents/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': 'dev.zed.Zed-Preview', 'CFBundleShortVersionString': '1.30.2',
        })).parents[1]
        app_data = self.home / 'Library/Application Support/Zed'
        database = app_data / 'db/0-preview/db.sqlite'
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as db:
            db.executescript('''
                CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO kv_store VALUES ('thread-metadata-remote-connection-backfill', '1');
                CREATE TABLE sidebar_threads (folder_paths TEXT, folder_paths_order TEXT,
                    main_worktree_paths TEXT, main_worktree_paths_order TEXT, remote_connection TEXT, updated_at TEXT);
            ''')
            db.execute('INSERT INTO sidebar_threads VALUES (?, ?, ?, ?, ?, ?)',
                       (str(project), '0', None, None, None, '2026-11-26'))
            db.commit()
        history = self.write(app_data / 'threads/threads.db', b'opaque message body')
        sidecar = self.write(app_data / 'threads/threads.db-wal', b'history WAL')
        settings = self.write('.config/zed/settings.json', b'{ "private": true }')
        originals = {path: path.read_bytes() for path in (database, history, sidecar, settings)}

        manual = self.call('scan', roots=[str(app_data), str(settings.parent)], deep=True)
        files = {row['path']: row for row in manual['files']}
        self.assertTrue(all(files[str(path)]['decision'] == 'keep' for path in originals))
        automatic = self.call('scan_local', startup=True)
        self.assertEqual(automatic['projects'].get(str(project)), ['Zed'])
        agent = next(row for row in automatic['agent_inventory'] if row['name'] == 'Zed')
        self.assertEqual((agent['kind'], agent['installed']), ('ide', True))
        self.assertEqual(agent['installations'][0]['path'], str(bundle))
        self.assertTrue({str(app_data / 'threads'), str(app_data / 'db'), str(settings.parent)} <=
                        {store['path'] for store in agent['stores']})
        result = self.call('run', paths=list(map(str, originals)), scan_id=automatic['scan_id'], action='quarantine')
        self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(originals))
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_crush_unregistered_project_and_manual_cleanup_protection(self):
        project = self.write('Projects/Crush Film/source.ts').parent
        data = project / '.crush'
        data.mkdir()
        database = data / 'crush.db'
        with closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY)')
            db.commit()
        wal = self.write(data / 'crush.db-wal', b'opaque sidecar')
        logs = self.write(data / 'logs/crush.log.1', b'old log')
        config = self.write(project / '.crushrc', b'exit 99')
        fake = self.write('Projects/Impostor/.crush/crush.db', b'not SQLite')
        linked = self.home / 'Projects/Link/.crush/crush.db'
        linked.parent.mkdir(parents=True)
        linked.symlink_to(database)
        originals = {path: path.read_bytes() for path in (database, wal, logs, config)}

        manual = self.call('scan', roots=[str(project)], deep=True)
        files = {row['path']: row for row in manual['files']}
        self.assertTrue(all(files[str(path)]['decision'] == 'keep' for path in originals))
        automatic = self.call('scan_local', startup=True)
        self.assertEqual(automatic['projects'].get(str(project)), ['Crush'])
        self.assertNotIn(str(fake.parents[1]), automatic['projects'])
        self.assertNotIn(str(linked.parents[1]), automatic['projects'])
        agent = next(row for row in automatic['agent_inventory'] if row['name'] == 'Crush')
        self.assertEqual(agent['kind'], 'cli')
        self.assertGreater(agent['bytes'], 0)
        self.assertEqual({store['path'] for store in agent['stores']}, {str(data), str(config)})
        result = self.call('run', paths=list(map(str, originals)), scan_id=automatic['scan_id'], action='quarantine')
        self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(originals))
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_kilo_extension_and_custom_runtime_share_one_family_and_preserve_wal(self):
        local = self.write('Projects/Kilo Film/local.ts').parent
        pending = self.write('Projects/Pending/src.ts').parent
        custom = self.base / 'Custom Kilo'
        custom.mkdir()
        database = custom / 'agent.db'
        ordinary = self.write(custom / 'unrelated.txt', b'user file')
        config = self.write('.config/kilo/kilo.json', b'{}')
        cache_file = self.write('.cache/kilo/bin/linter', b'cached tool')
        extension = self.write('.vscode-insiders/extensions/kilocode.kilo-code-7.8.32/package.json', json.dumps({
            'publisher': 'kilocode', 'name': 'kilo-code', 'version': '7.8.32',
        }).encode())
        editor_state = self.write('Library/Application Support/Code - Insiders/User/globalStorage/'
                                  'kilocode.kilo-code/settings.json', b'{}')
        with closing(sqlite3.connect(database)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE session (project_id TEXT, directory TEXT, workspace_id TEXT)')
            db.execute('INSERT INTO session VALUES (?, ?, ?)', ('global', str(local), None))
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('INSERT INTO session VALUES (?, ?, ?)', ('global', str(pending), None))
            db.commit()
            wal = Path(str(database) + '-wal')
            shm = Path(str(database) + '-shm')
            before = {path: path.read_bytes() for path in (database, wal, shm)}
            with patch.dict(os.environ, {'KILO_DB': str(database), 'XDG_CONFIG_HOME': '', 'XDG_CACHE_HOME': ''}):
                unauthorized = self.call('scan_local', startup=True)
                self.assertNotIn(str(local), unauthorized['projects'])
                self.assertTrue(any('KILO_DB' in issue for issue in unauthorized['issues']))
                self.call('scan', roots=[str(custom)], deep=False)
                report = self.call('scan_local', startup=True)
                self.assertEqual(report['projects'].get(str(local)), ['Kilo Code CLI'])
                self.assertNotIn(str(pending), report['projects'])
                names = {row['name']: row for row in report['agent_inventory']}
                cli, vscode = names['Kilo Code CLI'], names['Kilo Code VS Code']
                self.assertEqual((cli['family'], vscode['family']), ('Kilo Code', 'Kilo Code'))
                self.assertFalse(cli['installed'])
                self.assertTrue(cli['partial'])
                self.assertTrue(vscode['installed'])
                self.assertEqual(vscode['installations'][0]['path'], str(extension.parent))
                rows = {row['path']: row for row in report['files']}
                self.assertFalse(rows[str(ordinary)]['agents'])
                protected = (database, wal, shm, config, cache_file, editor_state)
                for path in protected:
                    self.assertEqual(rows[str(path)]['decision'], 'keep')
                result = self.call('run', paths=list(map(str, protected)), scan_id=report['scan_id'], action='quarantine')
                self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(protected))
                self.assertEqual({path: path.read_bytes() for path in before}, before)
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                refreshed = self.call('scan_local', startup=True)
                self.assertEqual(refreshed['projects'].get(str(pending)), ['Kilo Code CLI'])

    def test_trae_cli_keeps_coco_sessions_in_manual_cache_scan(self):
        project = self.write('Projects/Legacy Trailer/source.ts').parent
        identity = '019fbcca-9fd4-7d20-83dc-0762b2f839b0'
        base = f'Library/Caches/coco/sessions/{identity}'
        session = self.write(f'{base}/session.json', json.dumps({
            'id': identity, 'metadata': {'cwd': str(project)},
        }).encode())
        events = self.write(f'{base}/events.jsonl', b'private message')
        traces = self.write(f'{base}/traces.jsonl', b'private message')
        history = self.write('Library/Caches/coco/history.jsonl', b'legacy prompts')
        unrelated = self.write('Library/Caches/coco/network.tmp', b'generic cache')
        self.write(f'{base}/.git', b'gitdir: malformed fixture')
        protected = [session, events, traces, history]
        manual = self.call('scan', roots=[str(unrelated.parent)], deep=True)
        rows = {row['path']: row for row in manual['files']}
        self.assertEqual(rows[str(unrelated)]['decision'], 'review')
        self.assertEqual([rows[str(path)]['decision'] for path in protected], ['keep'] * len(protected))
        self.assertEqual([rows[str(path)]['duplicate_group'] for path in (events, traces)], ['', ''])
        self.assertNotIn(str(session.parent), manual['projects'])
        self.call('settings', roots=[])
        report = self.call('scan_local', startup=True)
        self.assertEqual(report['projects'].get(str(project)), ['Trae CLI'])
        agent = next(row for row in report['agent_inventory'] if row['name'] == 'Trae CLI')
        stores = {row['path']: row for row in agent['stores']}
        self.assertEqual(stores[str(session.parents[1])]['count'], 3)
        self.assertEqual(stores[str(history)]['count'], 1)
        result = self.call('run', paths=list(map(str, protected)), action='quarantine', scan_id=report['scan_id'])
        self.assertEqual([row['status'] for row in result['last_action']['items']], ['skipped'] * len(protected))

    def test_aider_manual_scope_and_discovery_cache_revalidate_marker(self):
        project = self.manual / "worktree"
        project.mkdir()
        marker = project / ".aider.chat.history.md"
        marker.write_bytes(b"\r\n# aider chat started at 2026-11-25 12:00:00\r\n\r\n")
        self.assertNotIn(str(project), self.call("scan_local", startup=True)["projects"])
        self.call("scan", roots=[str(self.manual)], deep=False)
        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"][str(project)], ["Aider"])
        self.assertNotIn(str(project), self.call("settings", roots=[])["projects"])
        self.assertTrue(marker.exists())
        self.assertEqual(self.call("scan", roots=[str(self.manual)], deep=False)["projects"][str(project)], ["Aider"])
        marker.unlink()
        self.assertNotIn(str(project), self.call("scan_local", startup=True)["projects"])

    def test_aider_artifacts_stay_protected_in_manual_only_scan(self):
        marker = self.manual / ".AIDER.CHAT.HISTORY.MD"
        marker.write_bytes(b"unrecognized historical format")
        protected = self.manual / ".aider.tags.cache.v4" / "entry.dat"
        protected.parent.mkdir()
        protected.write_bytes(b"cache")
        report = self.call("scan", roots=[str(self.manual)], deep=True)
        rows = {row["path"]: row for row in report["files"]}
        self.assertEqual([rows[str(item)]["decision"] for item in (marker, protected)], ["keep", "keep"])
        result = self.call("run", paths=[str(marker), str(protected)], scan_id=report["scan_id"], action="quarantine")
        self.assertEqual({item["status"] for item in result["last_action"]["items"]}, {"skipped"})
        self.assertEqual(marker.read_bytes(), b"unrecognized historical format")

    def test_download_review_does_not_require_a_manual_folder_grant(self):
        item = self.write("Downloads/archive.zip")
        self.call("scan", roots=[str(self.manual)])
        report = self.call("scan_local", deep=False)
        self.assertEqual(report["roots"], [str(self.manual)])
        self.assertTrue(item.exists())
        result = self.call("run", paths=[str(item)], scan_id=report["scan_id"], action="quarantine")
        self.assertEqual(result["last_action"]["items"][0]["status"], "quarantined")
        self.call("restore", batch_id=result["last_action"]["id"])
        self.assertEqual(item.read_bytes(), b"data")

    def test_discovered_project_source_is_inventory_only(self):
        item = self.write("Documents/Codex/movie/notes.txt")
        first = self.call("scan_local", deep=False)
        row = next(r for r in first["files"] if r["path"] == str(item))
        self.assertEqual(row["decision"], "keep")
        result = self.call("run", paths=[str(item)], scan_id=first["scan_id"], action="quarantine")
        self.assertEqual(result["last_action"]["items"][0]["status"], "skipped")
        self.assertTrue(item.exists())

    def test_excluding_automatic_root_blocks_scan_and_actions(self):
        item = self.write("Downloads/archive.zip")
        first = self.call("scan_local", deep=False)
        latest = self.call("settings", local_exclude=[str(item.parent)])
        self.assertFalse(latest["files"])
        with self.assertRaises(ValueError):
            self.call("run", paths=[str(item)], scan_id=first["scan_id"], action="quarantine")
        self.assertTrue(item.exists())

    def test_startup_samples_session_metadata_without_changing_deep_scan_preference(self):
        early = self.write("Projects/early/notes.txt").parent
        late = self.write("Projects/late/notes.txt").parent
        first = {"type": "session_meta", "payload": {"cwd": str(early)}}
        last = {"type": "turn_context", "payload": {"cwd": str(late)}}
        long_message = {"type": "response_item", "payload": {"text": "x" * (132 * 1024)}}
        records = "\n".join(map(json.dumps, (first, long_message, last)))
        self.write(".codex/sessions/rollout.jsonl", records.encode())

        normal = self.call("scan_local", deep=True)
        self.assertIn(str(late), normal["projects"])
        startup = self.call("scan_local", startup=True)
        self.assertTrue(startup["deep"])
        self.assertIn(str(early), startup["projects"])
        self.assertNotIn(str(late), startup["projects"])
        self.assertTrue(any("后续工作目录" in issue for issue in startup["issues"]))

    def test_agent_store_inventory_is_protected_and_deduplicates_shared_bytes(self):
        root = self.write("Projects/film/notes.txt").parent
        codex = self.write(".codex/sessions/rollout.jsonl", json.dumps(
            {"type": "session_meta", "payload": {"cwd": str(root)}}).encode())
        self.write(".claude/projects/film/session.jsonl", json.dumps({"cwd": str(root)}).encode())
        shared = self.write(".codex/sessions/shared.log", b"shared")
        alias = self.home / ".claude/projects/film/shared.log"
        os.link(shared, alias)

        report = self.call("scan_local", startup=True)
        agents = {row["name"]: row for row in report["agent_inventory"]}
        self.assertEqual(report["projects"][str(root)], ["Claude Code", "Codex"])
        self.assertEqual(agents["Codex"]["projects"], [str(root)])
        self.assertEqual(agents["Codex"]["stores"][0]["path"], str(codex.parent))
        self.assertEqual(agents["Claude Code"]["stores"][0]["shared_bytes"], shared.stat().st_blocks * 512)
        self.assertTrue(agents["Claude Code"]["supports_events"])
        self.assertFalse(agents["Codex"]["has_verified_cleanup"])
        indexed = {row["path"]: row for row in report["files"]}
        self.assertEqual([indexed[str(p)]["decision"] for p in (codex, shared, alias)], ["keep"] * 3)
        self.assertEqual(report["indexed_bytes"],
                         sum(path.stat().st_blocks * 512 for path in
                             (root / "notes.txt", codex, alias.parent / "session.jsonl", shared)))

    def test_deepseek_and_pi_inventory_shares_project_and_protects_tool_data(self):
        project = self.write("Projects/film/notes.txt", b"scene").parent
        workspace = "49fb9c05-a1c4-419e-9254-a6379f88240a"
        self.write(".dsh/storages/workspace.json", json.dumps({
            "unit": {"name": "workspace", "version": 2},
            "global": {"initialized": True, "workspaceIds": [workspace]},
            "tables": {"workspaces": {workspace: {"path": str(project)}}},
        }).encode())
        self.write("Applications/DeepSeek Harness.app/Contents/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": "com.deepseek.dsh", "CFBundleShortVersionString": "0.1.0",
        }))
        credential = self.write(".dsh/.env", b"fixture credential")
        transcript = self.write(".pi/agent/sessions/--film--/example.jsonl", (json.dumps({
            "type": "session", "version": 3, "id": "fixture-session", "cwd": str(project),
        }) + "\n").encode())
        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"][str(project)], ["DeepSeek Harness", "Pi"])
        agents = {row["name"]: row for row in report["agent_inventory"]}
        self.assertEqual(agents["DeepSeek Harness"]["installations"][0]["version"], "0.1.0")
        self.assertTrue(any(row["bytes"] > 0 and row["path"] == str(transcript.parents[1])
                            for row in agents["Pi"]["stores"]))
        self.assertGreater(agents["DeepSeek Harness"]["stores"][0]["bytes"], 0)
        self.assertEqual({row["decision"] for row in report["files"]
                          if row["path"] in (str(credential), str(transcript))}, {"keep"})
        report = self.call("scan", roots=[str(self.home / ".dsh"), str(transcript.parent)], deep=True)
        blocked = self.call("run", paths=list(map(str, (credential, transcript))),
                            scan_id=report["scan_id"], action="quarantine")
        self.assertEqual([row["status"] for row in blocked["last_action"]["items"]], ["skipped", "skipped"])
        self.assertEqual(credential.read_bytes(), b"fixture credential")
        self.assertTrue(transcript.is_file())

    def test_session_index_cache_refreshes_changed_files_and_forgets_excluded_stores(self):
        old = self.write("Projects/old/notes.txt").parent
        new = self.write("Projects/new/notes.txt").parent
        session = self.write(".codex/sessions/rollout.jsonl", json.dumps(
            {"type": "session_meta", "payload": {"cwd": str(old)}}).encode())
        self.assertIn(str(old), self.call("scan_local", startup=True)["projects"])
        with patch("engine.lumen.scan._cwd_records", side_effect=AssertionError("unmodified metadata was parsed again")):
            self.assertIn(str(old), self.call("scan_local", startup=True)["projects"])

        session.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(new)}}))
        refreshed = self.call("scan_local", startup=True)
        self.assertIn(str(new), refreshed["projects"])
        self.assertNotIn(str(old), refreshed["projects"])
        excluded = self.call("settings", local_exclude=[str(session.parent)])
        self.assertNotIn(str(new), excluded["projects"])
        self.assertFalse(excluded["agent_inventory"])

    def test_custom_session_store_must_stay_in_the_scoped_home(self):
        project = self.write("Projects/scene/render.mov").parent
        config = self.home / ".agent-data"
        self.write(".agent-data/projects/scene/session.jsonl", json.dumps({"cwd": str(project)}).encode())
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            result = self.call("scan_local", startup=True)
        self.assertEqual(result["projects"][str(project)], ["Claude Code"])
        self.assertEqual(result["agent_inventory"][0]["stores"][0]["path"], str(config / "projects"))

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.manual)}):
            result = self.call("scan_local", startup=True)
        self.assertNotIn(str(project), result["projects"])
        self.assertFalse(result["agent_inventory"])

    def test_hidden_and_symlinked_session_locations_are_never_read(self):
        project = self.write("Projects/hidden/source.txt").parent
        self.write(".codex/sessions/.Trash/retired.jsonl", json.dumps({
            "type": "session_meta", "payload": {"cwd": str(project)},
        }).encode())
        self.write(".codex/sessions/.git/retired.jsonl", json.dumps({
            "type": "session_meta", "payload": {"cwd": str(project)},
        }).encode())
        cycle = self.home / ".cycle"
        cycle.symlink_to(cycle, target_is_directory=True)
        with patch.dict(os.environ, {"CODEX_HOME": str(cycle)}):
            result = self.call("scan_local", startup=True)
        self.assertNotIn(str(project), result["projects"])

    def test_external_custom_store_requires_grant_and_stays_read_only(self):
        project = self.write("Projects/film/notes.txt").parent
        index = self.manual / "sessions"
        index.mkdir()
        source = index / "session.jsonl"
        source.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(project)}}))
        with patch.dict(os.environ, {"CODEX_HOME": str(self.manual)}):
            self.assertFalse(self.call("scan_local", startup=True)["agent_inventory"])
            self.call("scan", roots=[str(index)], deep=False)
            result = self.call("scan_local", startup=True)
            self.assertEqual(result["projects"][str(project)], ["Codex"])
            item = next(row for row in result["files"] if row["path"] == str(source))
            self.assertEqual((item["decision"], item["agent_store_path"]), ("keep", str(index)))
            outcome = self.call("run", paths=[str(source)], scan_id=result["scan_id"], action="quarantine")
            self.assertEqual(outcome["last_action"]["items"][0]["status"], "skipped")
            self.assertTrue(source.is_file())
            result = self.call("scan", roots=[], deep=False)
            self.assertFalse(result["agent_inventory"])

    def test_unreadable_store_metadata_marks_agent_footprint_partial(self):
        store = self.write(".gemini/projects.json", b"invalid json").parent
        agent, = self.call("scan_local", startup=True)["agent_inventory"]
        self.assertTrue(agent["partial"])
        self.assertTrue(any(row["path"] == str(store) and row["partial"] for row in agent["stores"]))

    def test_local_inventory_lists_models_and_protects_their_configuration(self):
        q4 = self.write(".lmstudio/models/Publisher/Camera/Camera-Q4_K_M.gguf", b"weight")
        manifest = self.write(".lmstudio/models/Publisher/Camera/README.md")
        report = self.call("scan_local", startup=True)
        model, = report["model_inventory"]["models"]
        self.assertEqual((model["tool"], model["quantization"], model["path"]),
                         ("LM Studio", "Q4_K_M", str(q4)))
        self.assertEqual(model["active_state"], "unknown")
        rows = {row["path"]: row for row in report["files"]}
        self.assertEqual(rows[str(manifest)]["decision"], "keep")
        self.assertTrue(any(row.get("model_store") for row in report["local_locations"]))
        result = self.call("run", action="quarantine", paths=[str(q4), str(manifest)],
                           scan_id=report["scan_id"])
        self.assertEqual({item["status"] for item in result["last_action"]["items"]}, {"skipped"})

    def test_openclaw_default_hidden_workspace_appears_in_workspace_inventory(self):
        workspace = self.write(".openclaw/workspace/notes.txt").parent
        asset = self.write("Media/footage.wav")
        self.write(".openclaw/workspace/timeline.otio", json.dumps({
            "OTIO_SCHEMA": "Timeline.1",
            "tracks": {"OTIO_SCHEMA": "Stack.1", "children": [
                {"OTIO_SCHEMA": "Clip.1", "media_reference": {
                    "OTIO_SCHEMA": "ExternalReference.1", "target_url": asset.as_uri(),
                }},
            ]},
        }).encode())
        self.write(".openclaw/openclaw.json", json.dumps(
            {"agents": {"defaults": {"workspace": str(workspace)}}}).encode())
        self.call("scan", roots=[str(asset.parent)], deep=False)
        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"][str(workspace)], ["OpenClaw"])
        self.assertIn(str(workspace), next(row for row in report["agent_inventory"]
                                          if row["name"] == "OpenClaw")["projects"])
        source = next(row for row in report["files"] if row["path"] == str(asset))
        self.assertEqual(source["decision"], "keep")
        self.assertTrue(source["refs"])

    def test_developer_cache_candidates_use_scoped_quarantine_and_preserve_vm_images(self):
        npm = self.write(".npm/_cacache/content-v2/sha512/aa/bb/" + "c" * 124, b"cache bytes")
        docker = self.write("Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw", b"VM bytes")
        report = self.call("scan_local", startup=True)
        groups = {row["tool"]: row for row in report["developer_caches"]}
        self.assertEqual(groups["npm"]["review_paths"], [str(npm)])
        self.assertEqual(groups["Docker"]["review_paths"], [])
        self.assertIsNone(groups["Docker"]["isolated_build_cache_bytes"])
        result = self.call("run", paths=[str(npm), str(docker)], scan_id=report["scan_id"], action="quarantine")
        status = {row["path"]: row["status"] for row in result["last_action"]["items"]}
        self.assertEqual(status, {str(npm): "quarantined", str(docker): "skipped"})
        self.call("restore", batch_id=result["last_action"]["id"])
        self.assertEqual(npm.read_bytes(), b"cache bytes")
        self.assertEqual(docker.read_bytes(), b"VM bytes")

    def test_manual_case_alias_cannot_quarantine_docker_vm(self):
        docker = self.write("Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw", b"VM bytes")
        alias = self.home / "library/containers/com.docker.docker/data/vms/0/data"
        if not alias.is_dir():
            self.skipTest("requires a case-insensitive fixture volume")
        report = self.call("scan", roots=[str(alias)], deep=True)
        row, = [entry for entry in report["files"] if entry["ino"] == docker.stat().st_ino]
        self.assertEqual(row["decision"], "keep")
        docker_group, = report["developer_caches"]
        self.assertEqual(docker_group["review_paths"], [])
        result = self.call("run", paths=[row["path"]], scan_id=report["scan_id"], action="quarantine")
        self.assertEqual(result["last_action"]["items"][0]["status"], "skipped")
        self.assertEqual(docker.read_bytes(), b"VM bytes")

    def test_model_recipe_survives_rescan_and_revalidates_destination_grant(self):
        weights = b"versioned weights"
        digest = hashlib.sha256(weights).hexdigest()
        repository = ".cache/huggingface/hub/models--org--weights"
        blob = self.write(repository + "/blobs/" + digest, weights)
        artifact = self.home / repository / "snapshots" / ("a" * 40) / "model.safetensors"
        artifact.parent.mkdir(parents=True)
        artifact.symlink_to(os.path.relpath(blob, artifact.parent))
        report = self.call("scan_local", startup=True)
        model, = [item for item in report["model_inventory"]["models"] if item["version"]]
        with self.assertRaisesRegex(ValueError, "版本"):
            self.call("model_pin", model_id=model["id"], scan_id=report["scan_id"],
                      filename=artifact.name, digest="sha256:" + "b" * 64)
        recipe = self.call("model_pin", model_id=model["id"], scan_id=report["scan_id"],
                           filename=artifact.name)["recipe"]
        artifact.unlink()
        blob.unlink()
        scanned = self.call("scan", roots=[str(self.manual)])
        self.assertEqual(scanned["model_recovery_recipes"], [recipe])

        def revoke(proof, destination, *, cancel, excluded_roots, precommit):
            self.assertEqual(proof.details()["digest"], recipe["digest"])
            self.assertEqual(destination, self.manual)
            self.call("settings", roots=[])
            with precommit():
                self.fail("revoked directory was accepted for publication")

        with patch("engine.lumen.model_recovery.recover", side_effect=revoke):
            with self.assertRaisesRegex(ValueError, "授权"):
                self.call("model_recover", recipe_id=recipe["id"], destination=str(self.manual))

    def test_configured_comfy_weights_are_discovered_in_the_scoped_home(self):
        weight = self.write("CustomWeights/landscape.safetensors", b"synthetic")
        self.write("Documents/ComfyUI/extra_model_paths.yaml",
                   f"studio:\n  base_path: {self.home}\n  checkpoints: CustomWeights\n".encode())
        report = self.call("scan_local", startup=True)
        model, = report["model_inventory"]["models"]
        self.assertEqual((model["path"], model["tool"]), (str(weight), "ComfyUI"))
        report = self.call("settings", local_exclude=[str(weight.parent)])
        self.assertEqual(report["model_inventory"]["models"], [])

    def test_explicit_lm_studio_model_folder_is_scoped_and_revocable(self):
        weight = self.manual / 'Publisher' / 'Camera' / 'Camera-Q8_0.gguf'
        weight.parent.mkdir(parents=True)
        weight.write_text('synthetic')
        root = {"path": str(self.manual), "tool": "LM Studio"}
        with self.assertRaises(ValueError):
            self.call("settings", model_roots=[root])
        report = self.call("settings", roots=[str(self.manual)], model_roots=[root])
        model, = report["model_inventory"]["models"]
        self.assertEqual((model["path"], model["tool"]), (str(weight), "LM Studio"))
        removed = self.call("settings", roots=[])
        self.assertEqual(removed["model_roots"], [])
        self.assertFalse(removed["model_inventory"]["models"])

    def test_manual_comfy_bin_weights_stay_protected_without_model_scope(self):
        project = self.manual / 'ComfyUI'
        weight = project / 'models/checkpoints/model.bin'
        weight.parent.mkdir(parents=True)
        weight.write_bytes(b'weights')
        workflow = project / 'user/default/workflows/render.json'
        workflow.parent.mkdir(parents=True)
        workflow.write_text(json.dumps({"nodes": [
            {"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": [weight.name]},
            {"id": "\ud800", "type": "UnknownLoader\ud800", "widgets_values": []},
        ]}))
        report = self.call("scan", roots=[str(project)], deep=False)
        model, = report["model_inventory"]["models"]
        self.assertEqual(model["path"], str(weight))
        self.assertTrue(model["workflow_refs"])
        self.assertTrue(report["model_inventory"]["issues"])
        current = next(row for row in report["files"] if row["path"] == str(weight))
        self.assertEqual(current["decision"], "keep")
        blocked = self.call("run", action="quarantine", paths=[str(weight)], scan_id=report["scan_id"])
        self.assertEqual(blocked["last_action"]["items"][0]["status"], "skipped")

    def test_agent_observation_is_removed_when_a_grant_is_renewed_for_replaced_directory(self):
        self.call("scan", roots=[str(self.manual)])
        event = {"hook_event_name": "Stop", "session_id": "session", "turn_id": "turn", "cwd": str(self.manual)}
        self.call("agent_event", agent="Codex", event=event)
        self.manual.rename(self.manual.with_name("old manual"))
        self.manual.mkdir()
        self.assertEqual(self.call("status")["agent_observations"], [])
        report = self.call("scan", roots=[str(self.manual)], renew_roots=[str(self.manual)])
        self.assertEqual(report["agent_observations"], [])
        self.assertFalse(self.call("agent_event", agent="Codex", event=event)["duplicate"])

    def test_overlapping_session_sources_keep_distinct_parser_metadata(self):
        codex = self.write("Projects/codex/source.txt").parent
        claude = self.write("Projects/claude/source.txt").parent
        self.write(".custom/sessions/projects/session.jsonl", (
            json.dumps({"type": "session_meta", "payload": {"cwd": str(codex)}}) + "\n" +
            json.dumps({"cwd": str(claude)})
        ).encode())
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".custom"),
                                     "CLAUDE_CONFIG_DIR": str(self.home / ".custom/sessions")}):
            for _ in range(2):
                result = self.call("scan_local", startup=True)
                self.assertEqual(result["projects"][str(codex)], ["Codex"])
                self.assertEqual(result["projects"][str(claude)], ["Claude Code"])

    def test_invalid_session_paths_cannot_poison_persisted_metadata(self):
        project = self.write("Projects/valid/source.txt").parent
        self.write(".codex/sessions/rollout.jsonl", "\n".join(json.dumps({
            "type": "session_meta", "payload": {"cwd": cwd},
        }) for cwd in ("relative\ud800", "/nul\0byte", str(project))).encode())
        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"], {str(project): ["Codex"]})
        cache = json.loads((self.state / "workspace.json").read_text())["agent_metadata"]
        self.assertEqual([row["paths"] for row in cache.values()], [[str(project)]])

    def test_installed_agent_and_two_discovered_agent_sources_are_attributed_read_only(self):
        self.write("Applications/Cursor.app/Contents/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": "com.todesktop.230313mzl4w4u92", "CFBundleShortVersionString": "test",
        }))
        root = self.write("Projects/shared cut/notes.txt").parent
        cursor = self.write("Library/Application Support/Cursor/User/workspaceStorage/id/workspace.json",
                            json.dumps({"folder": root.as_uri()}).encode())
        gemini = self.write(".gemini/projects.json",
                            json.dumps({"projects": {str(root): "shared-cut"}}).encode())

        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"][str(root)], ["Cursor", "Gemini CLI"])
        agents = {row["name"]: row for row in report["agent_inventory"]}
        self.assertTrue(agents["Cursor"]["installed"])
        self.assertEqual(agents["Cursor"]["installations"][0]["version"], "test")
        self.assertFalse(agents["Gemini CLI"]["installed"])
        rows = {r["path"]: r for r in report["files"]}
        for path, owner in ((cursor, "Cursor"), (gemini, "Gemini CLI")):
            self.assertEqual(rows[str(path)]["agents"], [owner])
            self.assertEqual(rows[str(path)]["decision"], "keep")
        outcome = self.call("run", paths=[str(cursor), str(gemini)], scan_id=report["scan_id"],
                            action="quarantine")
        self.assertTrue(all(row["status"] == "skipped" for row in outcome["last_action"]["items"]))
        self.assertTrue(cursor.exists() and gemini.exists())

    def test_devin_desktop_reuses_windsurf_family_and_guards_editor_state(self):
        self.write('Applications/Devin.app/Contents/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': 'com.exafunction.windsurf', 'CFBundleShortVersionString': '3.14.30833',
        }))
        root = self.write('Projects/电影 Project/source.txt').parent
        support = 'Library/Application Support/Devin/User'
        identity = self.write(f'{support}/workspaceStorage/local/workspace.json',
                              json.dumps({'folder': root.as_uri()}).encode())
        editor_state = self.write(f'{support}/workspaceStorage/local/state.vscdb', b'private indexed state')
        global_state = self.write(f'{support}/globalStorage/settings.json', b'private settings')
        cascade = self.write('.codeium/windsurf/cascade/conversation.pb', b'private conversation')

        report = self.call('scan_local', startup=True)
        self.assertEqual(report['projects'].get(str(root)), ['Windsurf'])
        agent = next(row for row in report['agent_inventory'] if row['name'] == 'Windsurf')
        self.assertTrue(agent['installed'])
        self.assertEqual(agent['installations'][0]['version'], '3.14.30833')
        rows = {row['path']: row for row in report['files']}
        protected = (identity, editor_state, global_state, cascade)
        for path in protected:
            self.assertEqual((rows[str(path)]['agents'], rows[str(path)]['decision']), (['Windsurf'], 'keep'))
        outcome = self.call('run', paths=list(map(str, protected)), scan_id=report['scan_id'], action='quarantine')
        self.assertTrue(all(row['status'] == 'skipped' for row in outcome['last_action']['items']))
        self.assertTrue(all(path.is_file() for path in protected))

    def test_shared_windsurf_workspace_lists_two_projects_and_protects_its_config(self):
        first = self.write('Projects/One/source.txt').parent
        second = self.write('Projects/Two/source.txt').parent
        manifest = self.write('Projects/One/build/edit.code-workspace', json.dumps({
            'folders': [{'path': '..'}, {'uri': second.as_uri()}],
        }).encode())
        self.write('Library/Application Support/Windsurf/User/workspaceStorage/saved/workspace.json',
                   json.dumps({'workspace': manifest.as_uri()}).encode())

        report = self.call('scan_local', startup=True)
        self.assertEqual(report['projects'], {str(first): ['Windsurf'], str(second): ['Windsurf']})
        row = next(row for row in report['files'] if row['path'] == str(manifest))
        self.assertEqual(row['decision'], 'keep')
        result = self.call('run', paths=[str(manifest)], scan_id=report['scan_id'], action='quarantine')
        self.assertEqual(result['last_action']['items'][0]['status'], 'skipped')
        self.assertTrue(manifest.is_file())

    def test_windsurf_untitled_config_is_protected_even_in_a_manual_only_scan(self):
        project = self.write('Projects/剪辑/source.txt').parent
        payload = json.dumps({'folders': [{'path': str(project)}]}).encode()
        manifest = self.write('Library/Application Support/Devin/Workspaces/1795610510123/workspace.json', payload)
        control = self.write('Other/Workspaces/1795610510123/workspace.json', payload)
        report = self.call('scan', roots=[str(manifest.parent), str(control.parent)], deep=False)
        rows = {row['path']: row for row in report['files']}
        self.assertEqual(rows[str(manifest)]['decision'], 'keep')
        self.assertEqual(rows[str(control)]['decision'], 'review')
        result = self.call('run', paths=[str(manifest)], scan_id=report['scan_id'], action='quarantine')
        self.assertEqual(result['last_action']['items'][0]['status'], 'skipped')
        self.assertEqual(manifest.read_bytes(), payload)

        report = self.call('scan_local', startup=True)
        self.assertEqual(report['projects'].get(str(project)), ['Windsurf'])
        rows = {row['path']: row for row in report['files']}
        self.assertEqual(rows[str(manifest)]['agents'], ['Windsurf'])

    def test_deep_inventory_does_not_analyze_agent_store_payloads(self):
        source = self.write(".codex/sessions/capture.html", b'<img src="image.png">')
        copy = self.write(".codex/sessions/capture-copy.html", source.read_bytes())
        target = self.write(".codex/sessions/image.png")
        result = self.call("scan_local", deep=True)
        items = {row["path"]: row for row in result["files"]}
        self.assertTrue(all(str(p) in items for p in (source, copy, target)))
        self.assertEqual(result["hash_reads"], 0)
        self.assertEqual(result["static_refs"], [])
        self.assertTrue(all(row["decision"] == "keep" for row in items.values()))

    def test_application_cache_analysis_preserves_credentials_and_requires_an_idle_app(self):
        app = self.write("Applications/Editor.app/Contents/Info.plist",
                         plistlib.dumps({"CFBundleIdentifier": "example.editor"})).parents[1]
        cache = self.write("Library/Containers/example.editor/Data/Library/Caches/preview.bin", b"rebuild")
        credential = self.write("Library/Containers/example.editor/Data/Library/Caches/Cookies", b"session")
        sidecar = credential.with_name("Cookies.sqlite-wal")
        sidecar.write_bytes(b"session journal")
        result = self.call("scan_local", startup=True)
        self.assertNotIn(str(cache), {r["path"] for r in result["files"]})
        result = self.call("scan_app", path=str(app))
        rows = {r["path"]: r for r in result["files"]}
        self.assertEqual((rows[str(cache)]["cleanup_kind"], rows[str(cache)]["decision"]), ("cache", "review"))
        self.assertEqual(rows[str(credential)]["decision"], "keep")
        self.assertEqual(rows[str(sidecar)]["decision"], "keep")
        with patch("engine.lumen.app_cleanup.available", return_value=False):
            busy = self.call("run", paths=[str(cache)], scan_id=result["scan_id"], action="quarantine")
            self.assertEqual(busy["last_action"]["items"][0]["status"], "skipped")
        with patch("engine.lumen.app_cleanup.available", return_value=True):
            direct = self.call("run", paths=[str(cache)], scan_id=busy["scan_id"], action="clean")
            self.assertEqual(direct["last_action"]["items"][0]["status"], "skipped")
            moved = self.call("run", paths=[str(cache), str(credential)], scan_id=direct["scan_id"], action="quarantine")
        self.assertEqual({r["status"] for r in moved["last_action"]["items"]}, {"quarantined", "skipped"})
        self.assertEqual(credential.read_bytes(), b"session")
        self.call("restore", batch_id=moved["last_action"]["id"])
        self.assertEqual(cache.read_bytes(), b"rebuild")

    def test_chromium_code_cache_uses_profile_and_format_evidence_with_reversible_review(self):
        app = self.write("Applications/Chrome.app/Contents/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": "com.google.Chrome", "CFBundleName": "Google Chrome",
        })).parents[1]
        root = "Library/Caches/Google/Chrome/Profile 1/Code Cache/"
        entry = (struct.pack("<QIIII", 0xfcfb6d1ba7725c30, 5, 3, 0, 0) + b"key-bytecode" +
                 struct.pack("<QIIII", 0xf4fa6f45970d41d8, 0, 0, 0, 0) * 2)
        js = self.write(root + "js/ab1234567890cdef_0", entry)
        wasm = self.write(root + "wasm/ef1234567890abcd_0", entry)
        paired = self.write(root + "js/ab1234567890dead_0", entry)
        companion = self.write(root + "js/ab1234567890dead_1", b"dependent stream")
        forged = self.write(root + "js/cd1234567890abef_0", entry[:-8])
        index = self.write(root + "js/index-dir/the-real-index", b"cache index")
        credentials = self.write(root + "js/Cookies", b"session")
        offline = self.write("Library/Application Support/Google/Chrome/Profile 1/Service Worker/CacheStorage/offline")
        first = self.call("scan_local", startup=True)
        self.assertEqual(next(row for row in first["files"] if row["path"] == str(js))["decision"], "keep")
        self.assertNotIn(str(offline), {row["path"] for row in first["files"]})

        report = self.call("scan_app", path=str(app))
        files = {row["path"]: row for row in report["files"]}
        self.assertEqual([files[str(path)]["decision"] for path in (js, wasm)], ["review", "review"])
        self.assertEqual(files[str(js)]["cleanup_kind"], "compiled_code")
        self.assertEqual(files[str(js)]["app_path"], str(app))
        self.assertEqual(files[str(wasm)]["cache_profile"], "Profile 1")
        for path in (paired, companion, forged, index, credentials):
            self.assertEqual(files[str(path)]["decision"], "keep", path)
        self.assertNotIn(str(offline), files)

        with patch("engine.lumen.scan._registered_code", return_value=(True, [app])):
            busy = self.call("run", paths=[str(js)], scan_id=report["scan_id"], action="quarantine")
        self.assertEqual(busy["last_action"]["items"][0]["status"], "skipped")
        with patch("engine.lumen.scan._registered_code", return_value=(False, [])):
            unregistered = self.call("run", paths=[str(js)], scan_id=busy["scan_id"], action="quarantine")
            self.assertEqual(unregistered["last_action"]["items"][0]["status"], "skipped")
        with patch("engine.lumen.scan._registered_code", return_value=(False, [app])):
            direct = self.call("run", paths=[str(js)], scan_id=unregistered["scan_id"], action="clean")
            self.assertEqual(direct["last_action"]["items"][0]["status"], "skipped")
            moved = self.call("run", paths=[str(js)], scan_id=direct["scan_id"], action="quarantine")
        self.assertFalse(js.exists())
        self.assertEqual(offline.read_bytes(), b"data")
        self.assertEqual(credentials.read_bytes(), b"session")
        self.call("restore", batch_id=moved["last_action"]["id"])
        self.assertEqual(js.read_bytes(), entry)

    def test_chromium_entries_remain_protected_without_unambiguous_selected_app(self):
        root = "Library/Caches/Chromium/Default/Code Cache/js"
        item = self.write(root + "/ab1234567890cdef_0", b"unverified raw data")
        manual = self.call("scan", roots=[str(item.parent)], deep=False)
        row, = manual["files"]
        self.assertEqual(row["decision"], "keep")
        for name in ("Chromium", "Other Chromium"):
            self.write(f"Applications/{name}.app/Contents/Info.plist",
                       plistlib.dumps({"CFBundleIdentifier": "org.chromium.Chromium"}))
        self.call("scan_local", startup=True)
        report = self.call("scan_app", path=str(self.home / "Applications/Chromium.app"))
        self.assertEqual(next(row for row in report["files"] if row["path"] == str(item))["decision"], "keep")

    def test_chromium_persistent_cache_is_explained_and_never_selected_for_cleanup(self):
        app = self.write("Applications/Chrome.app/Contents/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": "com.google.Chrome",
        })).parents[1]
        root = "Library/Caches/Google/Chrome/Profile 2/Code Cache/pc/"
        database = self.write(root + "partition/cache.db", b"SQLite format 3\0fixture")
        journal = self.write(root + "partition/cache.db-wal", b"fixture write-ahead log")
        targets = {str(path): path.read_bytes() for path in (database, journal)}

        initial = self.call("scan_local", startup=True)
        self.assertEqual({row["decision"] for row in initial["files"] if row["path"] in targets}, {"keep"})
        report = self.call("scan_app", path=str(app))
        rows = [row for row in report["files"] if row["path"] in targets]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual((row.get("cache_backend"), row["app_path"], row["cache_profile"], row["decision"]),
                             ("persistent", str(app), "Profile 2", "keep"))
            self.assertIn("持久化代码缓存", row["reason"])
            self.assertNotIn("cleanup_kind", row)

        report = self.call("scan", roots=[str(database.parent)], deep=True)
        for action in ("quarantine", "clean", "trash"):
            report = self.call("run", paths=list(targets), scan_id=report["scan_id"], action=action)
            self.assertEqual([row["status"] for row in report["last_action"]["items"]], ["skipped", "skipped"])
        self.assertEqual({path: Path(path).read_bytes() for path in targets}, targets)

    def test_xcode_candidates_exclude_compiled_deliverables_and_sources(self):
        self.write("Applications/Xcode.app/Contents/Info.plist",
                   plistlib.dumps({"CFBundleIdentifier": "com.apple.dt.Xcode"}))
        prefix = "Library/Developer/Xcode/DerivedData/Example/"
        intermediate = self.write(prefix + "Build/Intermediates.noindex/output.o")
        product = self.write(prefix + "Build/Products/export.zip")
        checkout = self.write(prefix + "SourcePackages/checkouts/module/README.md")
        report = self.call("scan_local", startup=True)
        rows = {r["path"]: r for r in report["files"]}
        self.assertEqual(rows[str(intermediate)]["cleanup_kind"], "build")
        self.assertNotIn("cleanup_kind", rows[str(product)])
        self.assertNotIn("cleanup_kind", rows[str(checkout)])

    def test_space_tree_ranks_immediate_children_and_does_not_follow_links(self):
        biggest = self.write("Projects/big/deep.bin", bytes(32 * 1024))
        small = self.write("Downloads/notes.txt", b"hello")
        hidden = self.write("Projects/hidden/private.bin", bytes(64 * 1024))
        (self.home / "cross-link").symlink_to(self.manual, target_is_directory=True)
        (self.manual / "outside").write_bytes(bytes(128 * 1024))
        os.link(biggest, biggest.with_name("same-inode.bin"))
        self.call("settings", local_exclude=[str(hidden.parent)])
        top = self.call("size_tree")
        self.assertEqual([row["name"] for row in top["children"]], ["Projects", "Downloads", "cross-link"])
        self.assertEqual(top["bytes"], (os.stat(biggest).st_blocks + os.stat(small).st_blocks) * 512)
        projects = self.call("size_tree", path=str(self.home / "Projects"))
        self.assertEqual([row["name"] for row in projects["children"]], ["big"])
        self.assertEqual(projects["children"][0]["bytes"], os.stat(biggest).st_blocks * 512)
        with self.assertRaises(ValueError):
            self.call("size_tree", path=str(self.home / "cross-link"))

    def test_space_tree_uses_parent_snapshot_for_unvisited_descendants(self):
        file = self.write("Projects/film/cuts/preview.mov", bytes(16 * 1024))
        top = self.call("size_tree")
        read_workspace = patch("engine.lumen.service.load_config", side_effect=AssertionError("read full workspace"))
        rebuild = patch("engine.lumen.space_tree.build_index", side_effect=AssertionError("walked subtree twice"))
        with read_workspace, rebuild:
            for folder in (self.home / "Projects", self.home / "Projects/film", file.parent):
                cached = self.call("size_tree", path=str(folder))
                self.assertEqual(cached["indexed_at"], top["indexed_at"])
                self.assertEqual(cached["bytes"], top["bytes"])

        new_file = self.write("Projects/film/cuts/new.cut", bytes(8192))
        refreshed = self.call("size_tree", path=str(file.parent), refresh=True)
        self.assertIn(str(new_file), {item["path"] for item in refreshed["children"]})
        self.call("settings", local_exclude=[str(new_file)])
        filtered = self.call("size_tree", path=str(file.parent))
        self.assertNotIn(str(new_file), {item["path"] for item in filtered["children"]})
