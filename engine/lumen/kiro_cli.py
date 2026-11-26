"""Local Kiro CLI storage inventory. Session transcripts and credentials remain opaque."""
import os
import re
import sqlite3

from .integrations import recorded_path


_NAME = 'Kiro CLI'
_SESSION_ID = re.compile(r'[a-zA-Z0-9_-]{1,128}', re.ASCII)
_CLI_DATA = ('library', 'application support', 'kiro-cli')
_CONFIG_DIRS = ('settings', 'agents', 'steering', 'skills')
_TABLES = {
    'conversations': {'key': ('TEXT', 1), 'value': ('TEXT', 0)},
    'conversations_v2': {'key': ('TEXT', 1), 'conversation_id': ('TEXT', 2), 'value': ('TEXT', 0),
                         'created_at': ('INTEGER', 0), 'updated_at': ('INTEGER', 0)},
}


def _valid_custom_root(home, path):
    return path is not None and path not in (home, home / '.kiro', *home.parents)


def homes(metadata):
    # https://kiro.dev/docs/cli/reference/cli-commands/
    default = metadata.home / '.kiro'
    custom = metadata.configured('KIRO_HOME')
    return (default, custom) if _valid_custom_root(metadata.home, custom) else (default,)


def custom_protected_roots(home):
    # Match only known Kiro subdirectories in manual scans. This grants no access.
    custom = recorded_path(os.environ.get('KIRO_HOME'))
    if _valid_custom_root(home, custom):
        return [str(custom / name) for name in ('sessions', *_CONFIG_DIRS)]
    return []


def protected_artifact(path):
    parts = tuple(part.casefold() for part in path.parts)
    return any(parts[index:index + 3] == _CLI_DATA or (
               part == '.kiro' and parts[index + 1] in ('sessions', *_CONFIG_DIRS))
               for index, part in enumerate(parts[:-1]))


def _keys(database, limit):
    database.execute('PRAGMA trusted_schema=OFF')
    database.execute('PRAGMA query_only=ON')
    database.execute('PRAGMA temp_store=MEMORY')
    steps = iter(range(100))
    database.set_progress_handler(lambda: int(next(steps, None) is None), 1000)
    tables = {name for name, in database.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('conversations', 'conversations_v2') AND rootpage > 0"
    )}
    paths = set()
    recognized = False
    for table, expected in _TABLES.items():
        if table not in tables:
            continue
        columns = {name: (kind.upper(), pk) for _, name, kind, _, _, pk in database.execute(f'PRAGMA table_info({table})')}
        if any(columns.get(name) != signature for name, signature in expected.items()):
            continue
        recognized = True
        for key, in database.execute(
                f"SELECT DISTINCT key FROM {table} WHERE typeof(key) = 'text' "
                "AND substr(key, 1, 1) = '/' AND length(CAST(key AS BLOB)) <= 4096 LIMIT ?", (limit + 1,)):
            paths.add(key)
    if not recognized:
        raise ValueError('unknown Kiro CLI conversation schema')
    return sorted(paths)


def _database_cwds(metadata, path):
    # https://github.com/kirodotdev/Kiro/issues/6440
    # https://github.com/kirodotdev/Kiro/issues/6318
    from .agent_adapters import _readonly_database

    try:
        with metadata.open(path) as stream:
            if stream is None:
                return []
            before = os.fstat(stream.fileno())
            if before.st_size > metadata.database_limit:
                metadata.issue(path, 'Kiro CLI 数据库超过本次读取上限')
                return []
            wal = path.with_name(path.name + '-wal')
            try:
                with metadata.open(wal) as pending:
                    if pending is not None and os.fstat(pending.fileno()).st_size:
                        metadata.issue(path, 'Kiro CLI 仅识别已写入主库的会话')
            except OSError as error:
                metadata.failed(wal, error)
            with _readonly_database(stream, before, metadata.database_limit) as database:
                paths = _keys(database, metadata.limit)
            after = os.fstat(stream.fileno())
            if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                metadata.issue(path, 'Kiro CLI 数据库读取期间有变更')
                return []
            if len(paths) > metadata.limit:
                metadata.issue(path, 'Kiro CLI 会话目录数超过本次读取上限')
            return paths[:metadata.limit]
    except OSError as error:
        metadata.failed(path, error)
    except (sqlite3.Error, ValueError):
        metadata.issue(path, 'Kiro CLI 数据库损坏、过大或格式未受支持')
    return []


def discover_kiro_cli(metadata, store, add):
    application = metadata.home / 'Library/Application Support/kiro-cli'
    if store(_NAME, application, 'Kiro CLI 数据库和应用状态'):
        for project in _database_cwds(metadata, application / 'data.sqlite3'):
            add(project, _NAME)
    # https://github.com/aws-samples/sample-kiro-credit-audit/blob/main/kiro_credit_audit.py
    for root in homes(metadata):
        for name in _CONFIG_DIRS:
            store(_NAME, root / name, 'Kiro CLI 配置和工程指令')
        sessions = root / 'sessions/cli'
        if not store(_NAME, sessions, 'Kiro CLI 旧版会话存储'):
            continue
        transcripts = {path.stem for path in metadata.children(sessions, suffix='.jsonl')}
        for path in metadata.children(sessions, suffix='.json'):
            if path.stem not in transcripts or not _SESSION_ID.fullmatch(path.stem):
                continue
            session = metadata.read_json(path, dict, limit=256 * 1024)
            if (session is not None and isinstance(session.get('session_state'), dict)
                    and session.get('session_id', path.stem) == path.stem):
                add(session.get('cwd'), _NAME)
