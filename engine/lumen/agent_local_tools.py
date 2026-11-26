"""Read scoped metadata from local agent applications."""
import json
import os
import re
import sqlite3
from itertools import islice

from . import amp, deepseek_sessions, kiro_cli, trae_cli
from .integrations import recorded_path


_AGENT_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
_OPENCLAW_PROFILE = re.compile(_AGENT_ID.pattern, re.IGNORECASE | re.ASCII)
_QWEN_RECORD_TYPES = ('user', 'assistant', 'tool_result', 'system')
_METADATA_WINDOW = 64 * 1024
_CONTINUE_SESSION_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.IGNORECASE | re.ASCII)


def _prefix(metadata, path, limit):
    try:
        with metadata.open(path) as stream:
            if stream is None:
                return b'', True
            limit = min(limit, metadata.budget)
            payload = stream.read(limit)
            metadata.budget -= len(payload)
            return payload, len(payload) < limit or len(payload) == os.fstat(stream.fileno()).st_size
    except OSError as error:
        metadata.failed(path, error)
        return b'', True


def _claude_desktop(metadata, store, add):
    # Official configuration root and reports of the local Cowork paths for macOS.
    # https://modelcontextprotocol.io/docs/develop/connect-local-servers
    # https://github.com/anthropics/claude-code/issues/25177
    # https://github.com/anthropics/claude-code/issues/78191
    agent = 'Claude Desktop'
    root = metadata.home / 'Library/Application Support/Claude'
    store(agent, root, 'Claude Desktop 本地配置和应用数据')
    store(agent, root / 'vm_bundles', 'Claude Cowork 本地虚拟机运行环境')
    sessions = root / 'local-agent-mode-sessions'
    if store(agent, sessions, 'Claude Cowork 本地会话存储'):
        metadata.issue(sessions, 'Claude Cowork 缺少可验证的本机工程目录格式')
    store(agent, metadata.home / 'Library/Logs/Claude', 'Claude Desktop 和 Cowork 本地日志')


def _home_path(value, home):
    if not isinstance(value, str) or '${' in value:
        return None
    value = value.strip()
    if value == '~' or value.startswith('~/'):
        value = str(home) + value[1:]
    return recorded_path(value)


def _openclaw_entries(agents, config, metadata):
    if 'entries' in agents:
        entries = agents['entries']
        if isinstance(entries, dict) and 'list' not in agents:
            rows = entries.items()
        else:
            metadata.issue(config, 'OpenClaw Agent 配置格式未受支持')
            return {}
    elif 'list' in agents:
        entries = agents['list']
        if not isinstance(entries, list):
            metadata.issue(config, 'OpenClaw Agent 配置格式未受支持')
            return {}
        rows = ((entry.get('id'), entry) for entry in entries if isinstance(entry, dict))
    else:
        return {'main': {}}
    if len(entries) > metadata.limit:
        metadata.issue(config, 'OpenClaw Agent 数超过本次读取上限')
        return {}
    result = {agent_id: entry for agent_id, entry in rows
              if isinstance(agent_id, str) and _AGENT_ID.fullmatch(agent_id) and isinstance(entry, dict)}
    if len(result) != len(entries):
        metadata.issue(config, 'OpenClaw Agent 标识无效或重复')
        return {}
    return result


def _openclaw_profile(metadata, store, add, home, root, config, environment_workspace):
    agent = 'OpenClaw'
    if root != metadata.home:
        store(agent, root, 'OpenClaw 本地配置、工具与状态')
    store(agent, root / 'cron', 'OpenClaw 历史后台调度存储')
    store(agent, root / 'state', 'OpenClaw 共享状态与后台调度存储')
    state_agents = root / 'agents'
    for directory in metadata.children(state_agents, directories=True):
        if _AGENT_ID.fullmatch(directory.name):
            store(agent, directory / 'agent', f'OpenClaw Agent {directory.name} 会话与运行状态')
            store(agent, directory / 'sessions', f'OpenClaw Agent {directory.name} 历史会话存档')

    value = metadata.read_json(config, dict, limit=1024 * 1024)
    if value is None:
        return
    gateway = value.get('gateway', {})
    if not isinstance(gateway, dict) or gateway.get('mode') not in (None, 'local'):
        metadata.issue(config, 'OpenClaw 仅归因本机网关的工作区')
        return
    # Includes can change ownership and gateway mode. Treat such configs as incomplete.
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if '$include' in node:
                metadata.issue(config, 'OpenClaw $include 配置未解析')
                return
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)

    agents = value.get('agents', {})
    if not isinstance(agents, dict):
        return
    entries = _openclaw_entries(agents, config, metadata)
    defaults = agents.get('defaults', {})
    if isinstance(defaults, dict):
        default_workspace = _home_path(defaults.get('workspace'), home)
    else:
        default_workspace = None
    legacy_defaults = [agent_id for agent_id, entry in entries.items()
                       if entry.get('default') is True and agents.get('ownership') != 'explicit']
    owner = next(iter(entries)) if len(entries) == 1 else None
    if len(legacy_defaults) == 1:
        owner = legacy_defaults[0]
    for agent_id, entry in entries.items():
        agent_dir = _home_path(entry.get('agentDir'), home)
        if agent_dir is not None:
            database = agent_dir / 'openclaw-agent.sqlite'
            if not metadata.allowed(database):
                metadata.issue(database, 'OpenClaw 自定义会话位置未获读取权限')
            elif _prefix(metadata, database, 16)[0] == b'SQLite format 3\0':
                store(agent, agent_dir, f'OpenClaw Agent {agent_id} 会话与运行状态')
        workspace = _home_path(entry.get('workspace'), home)
        runtime = entry.get('runtime')
        if workspace is None and 'workspace' not in entry and (
                not isinstance(runtime, dict) or runtime.get('type') != 'acp'):
            if default_workspace is not None:
                workspace = default_workspace if agent_id == owner else default_workspace / agent_id
            elif agent_id == owner:
                workspace = environment_workspace
        if workspace is not None:
            add(str(workspace), agent)


def _openclaw(metadata, store, add):
    # https://docs.openclaw.ai/concepts/multi-agent/index.html
    # https://docs.openclaw.ai/automation/cron-jobs/how-it-works
    # https://github.com/openclaw/openclaw/blob/v2026.11.9/src/agents/agent-scope-config.ts
    home = metadata.configured('OPENCLAW_HOME') or metadata.home
    state = metadata.configured('OPENCLAW_STATE_DIR')
    config = metadata.configured('OPENCLAW_CONFIG_PATH')
    workspace = metadata.configured('OPENCLAW_WORKSPACE_DIR')
    profile = (metadata.setting('OPENCLAW_PROFILE') or '').strip()
    default = home / '.openclaw'
    named = home / f'.openclaw-{profile}' if (
        profile.lower() != 'default' and _OPENCLAW_PROFILE.fullmatch(profile)
    ) else None
    selected = state or named or default
    for root in dict.fromkeys((metadata.home / '.openclaw', default, selected)):
        config_path = config if config is not None and root == selected else root / 'openclaw.json'
        _openclaw_profile(metadata, store, add, home, root, config_path,
                          workspace if root == selected else None)


def _qwen_cwd(metadata, path):
    payload, complete = _prefix(metadata, path, _METADATA_WINDOW)
    if not complete and not payload.endswith(b'\n'):
        payload = payload.rpartition(b'\n')[0]
    directories = set()
    for line in payload.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            break
        if not isinstance(record, dict):
            break
        owner = record.get('systemPayload')
        if record.get('type') == 'system' and (
                record.get('subtype') == 'managed_session_header_v1'
                or (record.get('subtype') == 'session_execution_engine'
                    and isinstance(owner, dict) and owner.get('engine') == 'managed')):
            metadata.issue(path, 'Qwen Code 托管会话仅盘点存储')
            return None
        if (record.get('sessionId') == path.stem and record.get('type') in _QWEN_RECORD_TYPES
                and isinstance(record.get('uuid'), str) and isinstance(record.get('timestamp'), str)):
            directory = recorded_path(record.get('cwd'))
            if directory is not None:
                directories.add(directory)
    if len(directories) == 1:
        return str(directories.pop())
    if directories or not complete:
        metadata.issue(path, 'Qwen Code 会话工作目录不一致或超出读取范围')
    return None


def _qwen_code(metadata, store, add):
    # https://github.com/QwenLM/qwen-code/blob/v0.28.4/packages/core/src/config/storage.ts
    # https://github.com/QwenLM/qwen-code/blob/v0.28.4/packages/core/src/services/chatRecordingService.ts
    # https://github.com/QwenLM/qwen-code/blob/v0.28.4/packages/cli/src/config/settingsSchema.ts
    agent = 'Qwen Code'
    default = metadata.home / '.qwen'
    configured = metadata.configured('QWEN_HOME') or default
    runtime = metadata.configured('QWEN_RUNTIME_DIR')
    if runtime is None:
        settings = metadata.read_json(configured / 'settings.json', dict, limit=256 * 1024) or {}
        advanced = settings.get('advanced')
        if isinstance(advanced, dict):
            runtime = _home_path(advanced.get('runtimeOutputDir'), metadata.home)
            if runtime is not None and not metadata.allowed(runtime / 'projects'):
                metadata.issue(runtime / 'projects', 'Qwen Code 自定义会话位置不在当前授权范围内')
    for root in dict.fromkeys((default, configured)):
        if root != metadata.home:
            store(agent, root, 'Qwen Code 配置、会话与工具存储')
    inspected = 0
    for root in dict.fromkeys((default, configured, runtime or configured)):
        projects = root / 'projects'
        if root == runtime or root == metadata.home:
            store(agent, projects, 'Qwen Code 自定义会话存储')
            store(agent, root / 'tmp', 'Qwen Code 自定义临时工具存储')
            store(agent, root / 'debug', 'Qwen Code 自定义调试记录')
        for project in metadata.children(projects, directories=True):
            for transcript in metadata.children(project / 'chats', suffix='.jsonl'):
                if inspected >= metadata.limit or not metadata.budget:
                    metadata.issue(projects, 'Qwen Code 会话数或数据量超过本次读取上限')
                    return
                inspected += 1
                add(_qwen_cwd(metadata, transcript), agent)


def _goose_cwds(metadata, path):
    # Reuse the pinned immutable SQLite reader without importing adapters at module load.
    from .agent_adapters import _readonly_database

    try:
        with metadata.open(path) as stream:
            if stream is None:
                return []
            before = os.fstat(stream.fileno())
            if before.st_size > metadata.database_limit:
                metadata.issue(path, 'goose 会话数据库超过本次读取上限')
                return []
            wal = path.with_name(path.name + '-wal')
            try:
                with metadata.open(wal) as pending:
                    if pending is not None and os.fstat(pending.fileno()).st_size:
                        metadata.issue(path, 'goose 仅识别已写入主库的会话')
            except OSError as error:
                metadata.failed(wal, error)
            with _readonly_database(stream, before, metadata.database_limit) as database:
                database.execute('PRAGMA trusted_schema=OFF')
                database.execute('PRAGMA query_only=ON')
                database.execute('PRAGMA temp_store=MEMORY')
                steps = iter(range(100))
                database.set_progress_handler(lambda: int(next(steps, None) is None), 1000)
                schema = database.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'sessions' AND type = 'table' AND rootpage > 0"
                ).fetchone()
                if schema is None:
                    metadata.issue(path, 'goose 会话数据库格式未受支持')
                    return []
                columns = {column[1] for column in database.execute('PRAGMA table_info(sessions)')}
                predicate = 'WHERE container_id IS NULL' if 'container_id' in columns else ''
                rows = database.execute(
                    'SELECT CASE WHEN length(CAST(working_dir AS BLOB)) <= 4096 '
                    f'THEN working_dir END FROM sessions {predicate} LIMIT ?', (metadata.limit + 1,)
                ).fetchall()
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                metadata.issue(path, 'goose 会话数据库读取期间有变更')
                return []
            if len(rows) > metadata.limit:
                metadata.issue(path, 'goose 会话记录数超过本次读取上限')
            return [cwd for (cwd,) in rows[:metadata.limit]]
    except OSError as error:
        metadata.failed(path, error)
    except (sqlite3.Error, ValueError):
        metadata.issue(path, 'goose 会话数据库损坏、过大或格式未受支持')
    return []


def _goose(metadata, store, add):
    # https://github.com/aaif-goose/goose/blob/v1.60.2/crates/goose/src/config/paths.rs
    # https://github.com/aaif-goose/goose/blob/v1.60.2/crates/goose/src/session/session_manager.rs
    agent = 'goose'
    mac = metadata.home / 'Library/Application Support/Block/goose'
    data = [mac, metadata.home / '.local/share/goose']
    store(agent, mac, 'goose 本地应用、配置和工具存储')
    for variable, fallback, evidence in (
            ('XDG_CONFIG_HOME', '.config', 'goose 配置存储'),
            ('XDG_STATE_HOME', '.local/state', 'goose 运行状态与日志'),
            ('XDG_DATA_HOME', '.local/share', 'goose 本地工具存储')):
        default = metadata.home / fallback
        configured = metadata.configured(variable)
        for directory in dict.fromkeys((default, configured or default)):
            root = directory / 'goose'
            store(agent, root, evidence)
            if variable == 'XDG_DATA_HOME':
                data.append(root)
    configured = metadata.configured('GOOSE_PATH_ROOT')
    if configured is not None:
        store(agent, configured / 'config', 'goose 自定义配置存储')
        store(agent, configured / 'state', 'goose 自定义运行状态与日志')
        store(agent, configured / 'data', 'goose 自定义工具存储')
        data.append(configured / 'data')
    for root in dict.fromkeys(data):
        sessions = root / 'sessions'
        if store(agent, sessions, 'goose 本机会话存储'):
            for cwd in _goose_cwds(metadata, sessions / 'sessions.db'):
                add(cwd, agent)


def _continue(metadata, store, add):
    # https://github.com/continuedev/continue/blob/5522c6f44ca0ac3528b37244818fbfa39b5af470/core/util/history.ts
    from .agent_ide import _local_folder_uri

    agent = 'Continue'
    default = metadata.home / '.continue'
    configured = metadata.configured('CONTINUE_GLOBAL_DIR') or default
    for root in dict.fromkeys((default, configured)):
        if root == default:
            store(agent, root, 'Continue 配置、索引和本地工具数据')
        else:
            store(agent, root / 'index', 'Continue 自定义代码索引')
        sessions = root / 'sessions'
        if not store(agent, sessions, 'Continue 本机会话存储'):
            continue
        index = sessions / 'sessions.json'
        rows = metadata.read_json(index, list, limit=2 * 1024 * 1024)
        if rows is None:
            continue
        if len(rows) > metadata.limit:
            metadata.issue(index, 'Continue 仅核对会话索引中的近期记录')
        identifiers = set()
        conflicts = set()
        for row in rows:
            key = row.get('sessionId') if isinstance(row, dict) else None
            if isinstance(key, str) and _CONTINUE_SESSION_ID.fullmatch(key):
                normalized = key.casefold()
                if normalized in identifiers:
                    conflicts.add(normalized)
                identifiers.add(normalized)
        for row in rows[-metadata.limit:]:
            key = row.get('sessionId') if isinstance(row, dict) else None
            if not isinstance(key, str) or not _CONTINUE_SESSION_ID.fullmatch(key) or key.casefold() in conflicts:
                continue
            location = row.get('workspaceDirectory')
            directory = recorded_path(location) or _local_folder_uri(location)
            if directory is None:
                continue
            # Check that the index row still has a local backing file without reading its contents.
            path = sessions / f'{key}.json'
            try:
                with metadata.open(path) as stream:
                    if stream is not None:
                        add(str(directory), agent)
            except OSError as error:
                metadata.failed(path, error)


def _deepseek_homes(metadata):
    default = metadata.home / '.dsh'
    configured = metadata.configured('DSH_HOME') or default
    return tuple(root for root in dict.fromkeys((default, configured)) if root != metadata.home)


def _deepseek_harness(metadata, store, add):
    # The shipped base uses the single JSON unit in storages. The global
    # workspace order is authoritative, including during create/delete recovery.
    # https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/workspace/workspace/src/spec.ts
    agent = 'DeepSeek Harness'
    roots = _deepseek_homes(metadata)
    for root in roots:
        if not store(agent, root, 'DeepSeek Harness 会话、配置与插件数据'):
            continue
        registry = root / 'storages/workspace.json'
        document = metadata.read_json(registry, dict, limit=2 * 1024 * 1024)
        if document is None:
            continue
        state, tables = document.get('global'), document.get('tables')
        ids = state.get('workspaceIds') if isinstance(state, dict) and state.get('initialized') is True else None
        records = tables.get('workspaces') if isinstance(tables, dict) else None
        if document.get('unit') != {'name': 'workspace', 'version': 2} or not isinstance(ids, list) or not isinstance(records, dict):
            metadata.issue(registry, 'DeepSeek Harness 工作区登记未初始化或格式未受支持')
            continue
        pending = state.get('pendingMutation')
        changing = pending.get('workspaceId') if isinstance(pending, dict) else None
        if len(ids) > metadata.limit:
            metadata.issue(registry, 'DeepSeek Harness 工作区数超过本次读取上限')
        for identifier in ids[:metadata.limit]:
            if not isinstance(identifier, str) or identifier == changing:
                continue
            record = records.get(identifier)
            path = recorded_path(record.get('path')) if isinstance(record, dict) else None
            if path is not None and not any(path == data or data in path.parents for data in roots):
                add(str(path), agent)
    for relative, evidence in (
        ('Application Support/@deepseek-ai/dsh-desktop', 'DeepSeek Harness 桌面应用数据'),
        ('Logs/DeepSeek Harness', 'DeepSeek Harness 桌面日志'),
        ('Caches/com.deepseek.dsh', 'DeepSeek Harness 桌面缓存'),
        ('Caches/com.deepseek.dsh.ShipIt', 'DeepSeek Harness 更新缓存'),
        ('Caches/@deepseek-aidsh-desktop-updater', 'DeepSeek Harness 安装包缓存'),
        ('HTTPStorages/com.deepseek.dsh', 'DeepSeek Harness 网站存储'),
    ):
        store(agent, metadata.home / 'Library' / relative, evidence)


def _deepseek_headless(metadata, store, add):
    roots = _deepseek_homes(metadata)
    for root in roots:
        for project in deepseek_sessions.projects(metadata, root / 'sessions'):
            if not any(project == data or data in project.parents for data in roots):
                add(str(project), 'DeepSeek Harness')


def _pi_session_header(metadata, path):
    try:
        with metadata.open(path) as stream:
            if stream is None:
                return None
            limit = min(16 * 1024, metadata.budget)
            line = stream.readline(limit)
            metadata.budget -= len(line)
            if not line.endswith(b'\n'):
                return None
            value = json.loads(line)
    except OSError as error:
        metadata.failed(path, error)
        return None
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or value.get('type') != 'session':
        return None
    version = value.get('version', 1)
    identifier = value.get('id')
    if type(version) is not int or version not in (1, 2, 3) or not isinstance(identifier, str) or not 0 < len(identifier) <= 256:
        return None
    return recorded_path(value.get('cwd'))


def _pi(metadata, store, add):
    # https://github.com/earendil-works/pi/blob/bfafd8ed5168bca1f1ca88ef0529cfd326664d16/packages/coding-agent/docs/session-format.md
    agent = 'Pi'
    default = metadata.home / '.pi/agent'
    configured = metadata.configured('PI_CODING_AGENT_DIR') or default
    session_override = metadata.configured('PI_CODING_AGENT_SESSION_DIR')
    roots = tuple(root for root in dict.fromkeys((default, configured)) if root != metadata.home)
    sessions = [root / 'sessions' for root in roots]
    for root in roots:
        if store(agent, root, 'Pi 配置、扩展与会话数据'):
            settings = metadata.read_json(root / 'settings.json', dict)
            path = _home_path(settings.get('sessionDir'), metadata.home) if settings is not None else None
            if path is not None and session_override is None:
                sessions.append(path)
    if session_override is not None:
        sessions.append(session_override)
    sessions = tuple(path for path in dict.fromkeys(sessions) if path != metadata.home)
    remaining = metadata.limit
    for root in sessions:
        if not store(agent, root, 'Pi 本机会话存储'):
            continue
        buckets = metadata.children(root, directories=True, prefix='--')
        for directory in (root, *(path for path in buckets if path.name.endswith('--'))):
            for session in metadata.children(directory, suffix='.jsonl'):
                if remaining == 0 or metadata.budget == 0:
                    metadata.issue(root, 'Pi 会话元数据超过本次读取上限')
                    return
                remaining -= 1
                path = _pi_session_header(metadata, session)
                if path is not None and not any(path == data or data in path.parents for data in (*roots, *sessions)):
                    add(str(path), agent)


def _conductor(metadata, store, add):
    # https://www.conductor.build/docs/concepts/git-worktrees
    # https://www.conductor.build/docs/reference/privacy
    agent = 'Conductor'
    store(agent, metadata.home / 'Library/Application Support/com.conductor.app', 'Conductor 会话和应用数据')
    root = metadata.home / 'conductor/workspaces'
    workspaces = (workspace for repo in metadata.children(root, directories=True)
                  for workspace in metadata.children(repo, directories=True))
    for position, workspace in enumerate(islice(workspaces, metadata.limit + 1)):
        if position == metadata.limit:
            metadata.issue(root, 'Conductor 工作区数量超过本次识别上限')
            break
        if _linked_worktree(metadata, workspace):
            add(str(workspace), agent)
            store(agent, workspace / '.context', 'Conductor 工作区附件和交接资料')


def _linked_worktree(metadata, workspace):
    # Read only the checkout marker. The linked Git directory may lie outside the scan scope.
    marker = metadata.read(workspace / '.git', 8192, gitfile=True)
    if marker is None or not marker.startswith(b'gitdir: '):
        return False
    lines = marker.splitlines()
    if len(lines) != 1 or len(lines[0]) == len(b'gitdir: '):
        return False
    target = recorded_path(os.path.normpath(workspace / os.fsdecode(lines[0][len(b'gitdir: '):])))
    return target is not None and target.parent.name == 'worktrees'


def conductor_user_data(path):
    """Protect known user-data paths even in a manual-only scan."""
    parts = tuple(part.casefold() for part in path.parts)
    for index, part in enumerate(parts):
        if part == 'library' and parts[index + 1:index + 3] == ('application support', 'com.conductor.app'):
            return True
        if (parts[index:index + 2] == ('conductor', 'workspaces')
                and parts[index + 4:index + 5] == ('.context',)):
            return True
    return False


ADAPTERS = (_claude_desktop, _openclaw, _qwen_code, _goose, _continue, _deepseek_harness, _deepseek_headless,
            _pi, _conductor, amp.discover_amp, kiro_cli.discover_kiro_cli, trae_cli.discover_trae_cli)
