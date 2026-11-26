"""Read bounded TRAE CLI session metadata."""
import json
import os
import re
import sqlite3

from .integrations import recorded_path
from .zstd_header import first_line


_AGENT = 'Trae CLI'
_DEFAULT = '.trae/cli'
_STORES = {'sessions': 'TRAE CLI 活动会话', 'archived_sessions': 'TRAE CLI 归档会话'}
_LEGACY_SESSION_ROOTS = ('Library/Caches/coco/sessions', '.cache/coco/sessions', 'Library/Caches/trae-cli/sessions')
_LEGACY_BOUNDARIES = tuple(('/' + root.casefold(), '/' + root.casefold() + '/') for root in _LEGACY_SESSION_ROOTS)
_LEGACY_PROMPT_HISTORY = ('Library/Caches/coco/history.jsonl', '.cache/coco/history.jsonl')
_LEGACY_HISTORY_SUFFIXES = tuple('/' + path.casefold() for path in _LEGACY_PROMPT_HISTORY)
_PROTECTED_FILES = frozenset({'auth.json', 'config.toml', 'history.jsonl', 'hooks.json', 'session_index.jsonl'})
_DATABASE = re.compile(r'(?:state|thread_history)_[0-9]{1,6}\.sqlite(?:-wal|-shm|-journal)?')
_STATE_DB = 'state_5.sqlite'
_DATABASE_STORES = {_STATE_DB: 'TRAE CLI 会话索引', 'thread_history_1.sqlite': 'TRAE CLI 会话正文库'}
_DB_SIDECARS = ('-wal', '-shm', '-journal')
# Declared type, NOT NULL flag, primary-key position, hidden-column flag.
_THREAD_SCHEMA = {
    'id': ('TEXT', 0, 1, 0),
    'cwd': ('TEXT', 1, 0, 0),
    'rollout_path': ('TEXT', 1, 0, 0),
    'source': ('TEXT', 1, 0, 0),
    'updated_at': ('INTEGER', 1, 0, 0),
}
_THREAD_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.ASCII)
_ROLLOUT = re.compile(
    rf'rollout-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}}-'
    rf'({_THREAD_ID.pattern})\.jsonl(\.zst)?', re.ASCII,
)
_DATE_PARTS = tuple(re.compile(pattern) for pattern in (r'[0-9]{4}', r'0[1-9]|1[0-2]', r'0[1-9]|[12][0-9]|3[01]'))
_HEADER_LIMIT = 32 * 1024
_COMPRESSED_INPUT_LIMIT = 256 * 1024


def custom_home(home, setting=os.environ.get):
    # https://trae.cn/trae-cli/install_v2.sh
    direct = setting('TRAECLI_HOME')
    if direct:
        root = recorded_path(direct)
    else:
        base = recorded_path(setting('TRAE_HOME') or setting('TRAEX_HOME'))
        root = base / 'cli' if base is not None and base != home and base not in home.parents else None
    if root is not None and root not in (home, home / _DEFAULT, *home.parents):
        return root
    return None


def protected_artifact(path, custom):
    if custom is None or custom not in path.parents:
        return False
    first, *remaining = path.relative_to(custom).parts
    return (first in _STORES or not remaining and
            (first in _PROTECTED_FILES or _DATABASE.fullmatch(first) is not None))


def is_legacy_session_data(folded_path):
    return (
        folded_path.endswith(_LEGACY_HISTORY_SUFFIXES)
        or ('/sessions' in folded_path
            and any(folded_path.endswith(root) or prefix in folded_path for root, prefix in _LEGACY_BOUNDARIES))
    )


def _legacy_projects(metadata, root):
    # https://docs.trae.cn/cli_upgrade-trae-cli
    # https://github.com/vibe-cafe/vibe-usage/blob/3f4695ecd660311ea423b2e7c7a0fc3a76be6ed4/src/parsers/trae-cli.js
    for session in metadata.children(root, directories=True):
        if not _THREAD_ID.fullmatch(session.name):
            continue
        if metadata.budget == 0:
            metadata.issue(root, 'TRAE CLI 旧版 CoCo 会话元数据超过本次读取上限')
            return
        record = metadata.read_json(session / 'session.json', dict, limit=_HEADER_LIMIT)
        fields = record.get('metadata') if record is not None and record.get('id') == session.name else None
        project = recorded_path(fields.get('cwd')) if isinstance(fields, dict) else None
        if project is None:
            metadata.issue(root, '部分 TRAE CLI 旧版 CoCo 会话标识或工作目录无法核验')
            continue
        yield session.name, project


def _rollouts(metadata, root, dated):
    pending = [(root, 0)]
    remaining = metadata.limit
    while pending:
        if remaining == 0 or metadata.budget == 0:
            metadata.issue(root, 'TRAE CLI 会话元数据超过本次读取上限')
            return
        directory, depth = pending.pop()
        remaining -= 1
        if dated and depth < len(_DATE_PARTS):
            pending.extend((path, depth + 1) for path in metadata.children(directory, directories=True)
                           if _DATE_PARTS[depth].fullmatch(path.name))
            continue
        for path in reversed(metadata.children(directory, prefix='rollout-')):
            match = _ROLLOUT.fullmatch(path.name)
            if match is None:
                continue
            if remaining == 0 or metadata.budget == 0:
                metadata.issue(root, 'TRAE CLI 会话元数据超过本次读取上限')
                return
            remaining -= 1
            yield path, match[1], bool(match[2])


def _project(metadata, path, identity, compressed):
    try:
        with metadata.open(path) as stream:
            if stream is None:
                return None
            if compressed:
                header, spent = first_line(stream, min(metadata.budget, _COMPRESSED_INPUT_LIMIT), _HEADER_LIMIT)
            else:
                header = stream.readline(min(metadata.budget, _HEADER_LIMIT))
                spent = len(header)
            metadata.budget -= spent
        if header is None or not header.endswith(b'\n'):
            return None
        record = json.loads(header)
        payload = record.get('payload') if isinstance(record, dict) and record.get('type') == 'session_meta' else None
        if isinstance(payload, dict) and payload.get('id') == identity:
            return recorded_path(payload.get('cwd'))
    except OSError as error:
        metadata.failed(path, error)
    except (ValueError, RecursionError):
        pass
    return None


def _thread_rows(database, limit):
    database.execute('PRAGMA trusted_schema=OFF')
    database.execute('PRAGMA query_only=ON')
    database.execute('PRAGMA temp_store=MEMORY')
    steps = iter(range(100))
    database.set_progress_handler(lambda: int(next(steps, None) is None), 1000)
    if database.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'threads' AND rootpage > 0"
    ).fetchone() is None:
        raise ValueError('no physical threads table')
    columns = {name: (kind.upper(), required, pk, hidden) for _, name, kind, required, _, pk, hidden
               in database.execute('PRAGMA main.table_xinfo(threads)')}
    if any(columns.get(name) != signature for name, signature in _THREAD_SCHEMA.items()):
        raise ValueError('unknown threads schema')
    return database.execute(
        "SELECT id, cwd FROM main.threads WHERE typeof(id) = 'text' AND length(CAST(id AS BLOB)) = 36 "
        "AND typeof(cwd) = 'text' AND substr(cwd, 1, 1) = '/' AND length(CAST(cwd AS BLOB)) <= 4096 "
        "ORDER BY updated_at DESC, id DESC LIMIT ?", (limit + 1,)
    ).fetchall()


def _active_sidecar(metadata, database, suffix):
    path = database.with_name(database.name + suffix)
    try:
        with metadata.open(path) as stream:
            return stream is not None and os.fstat(stream.fileno()).st_size > 0
    except (FileNotFoundError, NotADirectoryError):
        return False


def _database_projects(metadata, path):
    # Embedded migrations in the TRAE CLI 0.216.0 public release.
    # https://lf-cdn.trae.com.cn/obj/trae-com-cn/trae-cli/v2/public/stable/latest.json
    from .agent_adapters import _readonly_database

    try:
        with metadata.open(path) as stream:
            if stream is None:
                return []
            before = os.fstat(stream.fileno())
            if before.st_size > metadata.database_limit:
                metadata.issue(path, 'TRAE CLI 数据库超过本次读取上限')
                return []
            if _active_sidecar(metadata, path, '-journal'):
                metadata.issue(path, 'TRAE CLI 数据库存在回滚日志，本次仅盘点占用')
                return []
            if _active_sidecar(metadata, path, '-wal'):
                metadata.issue(path, 'TRAE CLI 仅识别已写入主库的会话')
            with _readonly_database(stream, before, metadata.database_limit) as database:
                rows = _thread_rows(database, metadata.limit)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                metadata.issue(path, 'TRAE CLI 数据库读取期间有变更')
                return []
            if _active_sidecar(metadata, path, '-journal'):
                metadata.issue(path, 'TRAE CLI 数据库读取期间生成回滚日志')
                return []
        if len(rows) > metadata.limit:
            metadata.issue(path, 'TRAE CLI 数据库会话数超过本次读取上限')
        return [(identifier, recorded_path(cwd)) for identifier, cwd in rows[:metadata.limit]
                if _THREAD_ID.fullmatch(identifier)]
    except OSError as error:
        metadata.failed(path, error)
    except (sqlite3.Error, ValueError):
        metadata.issue(path, 'TRAE CLI 数据库读取超限、损坏或格式未受支持')
    return []


def discover_trae_cli_history(metadata, store, add):
    # https://docs.trae.cn/cli_command-line-parameters
    # https://github.com/kenn-io/agentsview/tree/d92f8630a496c9237e4fa8316aa60c3387a59f59/internal/parser/testdata/data-version-corpus/traex
    default = metadata.home / _DEFAULT
    custom = custom_home(metadata.home, metadata.setting)
    homes = [default] if store(_AGENT, default, 'TRAE CLI 配置、历史与运行状态') else []
    if custom is not None:
        homes.append(custom)
    found = {}

    def associate(path, identity, project):
        if project is not None and found.setdefault(identity, project) != project:
            found[identity] = None
            metadata.issue(path, 'TRAE CLI 同一会话的工作目录冲突')

    for home in homes:
        if home == custom and not any(metadata.allowed(home / name) for name in (*_STORES, *_DATABASE_STORES)):
            metadata.issue(home, 'TRAE CLI 自定义会话位置未获读取权限')
            continue
        for filename, title in _DATABASE_STORES.items():
            path = home / filename
            if store(_AGENT, path, title, file=True) and filename == _STATE_DB:
                for identity, project in _database_projects(metadata, path):
                    associate(path, identity, project)
            for suffix in _DB_SIDECARS:
                store(_AGENT, home / (filename + suffix), title + '伴随文件', file=True)
        for name, title in _STORES.items():
            root = home / name
            if not store(_AGENT, root, title):
                continue
            for path, identity, compressed in _rollouts(metadata, root, dated=name == 'sessions'):
                project = _project(metadata, path, identity, compressed)
                if project is None:
                    subject = '压缩会话' if compressed else '会话'
                    metadata.issue(root, f'部分 TRAE CLI {subject}首行或工作目录无法核验')
                    continue
                associate(root, identity, project)
    for relative in _LEGACY_SESSION_ROOTS:
        root = metadata.home / relative
        if store(_AGENT, root, 'TRAE CLI 1.x CoCo 历史会话'):
            for identity, project in _legacy_projects(metadata, root):
                associate(root, identity, project)
    # https://github.com/deepcoldy/botmux/blob/84dc2f7717dd318bee448f2716dcfcba55a9b257/src/adapters/cli/coco.ts
    for relative in _LEGACY_PROMPT_HISTORY:
        store(_AGENT, metadata.home / relative, 'TRAE CLI 1.x CoCo 提示词历史', file=True)
    for project in found.values():
        if project is not None:
            add(str(project), _AGENT)
