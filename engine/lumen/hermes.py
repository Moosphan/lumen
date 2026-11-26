"""Inventory Hermes Agent profile data and verified local workspace metadata."""
import re

from .integrations import recorded_path


_AGENT = 'Hermes Agent'
_PROFILE_NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
_POLICY_PREFIX = 'gateway.local_policy.v1:'
_DESKTOP_NAMES = ('Hermes', 'Hermes Agent', 'Hermes Light')
_PROFILE_FILES = ('state.db', 'state.db-wal', 'state.db-shm', 'state.db-journal',
                  'config.yaml', 'profile.yaml', 'auth.json', '.env', 'SOUL.md')


def _profile_projects(metadata, database, add):
    # https://github.com/NousResearch/hermes-agent/blob/v0.23.4/persistence/sqlite/authority/_local.py
    # https://github.com/NousResearch/hermes-agent/blob/v0.23.4/gateway/session_policy.py
    from .agent_adapters import _metadata_tables, _read_database_metadata

    def read_rows(connection):
        names = {'sessions', 'state_meta'}
        if _metadata_tables(connection, names) != names:
            metadata.issue(database, 'Hermes Agent 本机运行策略格式未受支持')
            return None
        # Keep request contents and terminal settings inside SQLite. Only the verified cwd leaves it.
        return connection.execute('''
            WITH receipts AS MATERIALIZED (
                SELECT substr(key, ?) AS owner,
                       CASE WHEN length(CAST(value AS BLOB)) <= 262144 AND json_valid(value) THEN value END AS receipt
                FROM main.state_meta WHERE key GLOB ? LIMIT ?
            ), runtime AS MATERIALIZED (
                SELECT owner, json_extract(receipt, '$.session_id') AS recorded_owner,
                       json_extract(receipt, '$.entry.session_id') AS current_session,
                       json_extract(receipt, '$.policy.cwd') AS policy_cwd,
                       json_extract(receipt, '$.policy.terminal_json') AS terminal
                FROM receipts
            )
            SELECT CASE WHEN owner = recorded_owner AND sessions.cwd = policy_cwd
                              AND length(CAST(sessions.cwd AS BLOB)) <= 4096
                              AND sessions.source IN ('local', 'cli', 'tui', 'gui', 'desktop', 'acp', 'oneshot')
                              AND CASE WHEN json_valid(terminal)
                                  THEN coalesce(nullif(lower(trim(json_extract(terminal, '$.TERMINAL_ENV'))), ''),
                                                'local') = 'local'
                                       AND json_extract(terminal, '$.TERMINAL_CWD') = policy_cwd
                                  ELSE 0 END
                         THEN sessions.cwd END
            FROM runtime LEFT JOIN main.sessions AS sessions ON sessions.id = runtime.current_session
        ''', (len(_POLICY_PREFIX) + 1, _POLICY_PREFIX + '*', metadata.limit + 1)).fetchall()

    rows = _read_database_metadata(metadata, database, _AGENT, read_rows)
    if rows is None:
        return False
    if len(rows) > metadata.limit:
        metadata.issue(database, 'Hermes Agent 会话策略达到本次读取上限')
    for (value,) in rows[:metadata.limit]:
        path = recorded_path(value)
        if path is not None:
            add(str(path), _AGENT)
    return True


def _deleted(metadata, profile):
    marker = profile.parent / '.deleted' / profile.name
    try:
        with metadata.open(marker) as stream:
            return stream is not None
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as error:
        metadata.failed(marker, error)
        return True


def discover_hermes(metadata, store, add):
    # https://github.com/NousResearch/hermes-agent/blob/v0.23.4/hermes_constants.py
    default = metadata.home / '.hermes'
    seen = {default}
    if store(_AGENT, default, 'Hermes Agent 本地历史、配置和运行数据'):
        _profile_projects(metadata, default / 'state.db', add)
        profiles = [root for root in metadata.children(default / 'profiles', directories=True)
                    if _PROFILE_NAME.fullmatch(root.name)]
        limit = 24 if metadata.quick else 96
        if len(profiles) > limit:
            metadata.issue(default / 'profiles', 'Hermes Agent 配置档数量达到本次读取上限')
        for profile in profiles[:limit]:
            seen.add(profile)
            if not _deleted(metadata, profile) and _profile_projects(metadata, profile / 'state.db', add):
                store(_AGENT, profile, f'Hermes Agent 配置档 · {profile.name}')

    configured = metadata.configured('HERMES_HOME')
    if configured is not None and configured not in seen and (
            configured.parent.name != 'profiles' or not _deleted(metadata, configured)
    ) and _profile_projects(metadata, configured / 'state.db', add):
        for name in _PROFILE_FILES:
            store(_AGENT, configured / name, 'Hermes Agent 自定义配置档文件', file=True)

    for name in _DESKTOP_NAMES:
        store(_AGENT, metadata.home / 'Library/Application Support' / name, f'{name} 桌面数据')


def protected_artifact(path):
    parts = path.parts
    return ('.hermes' in parts[:-1] or any(
        parts[index:index + 2] == ('Library', 'Application Support') and parts[index + 2] in _DESKTOP_NAMES
        for index in range(len(parts) - 3)))
