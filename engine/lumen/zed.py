"""Read Zed's local sidebar project metadata without accessing conversation bodies."""
from .integrations import recorded_path


_AGENT = 'Zed'
_PROFILE = ('Library', 'Application Support', 'Zed')
_LOCATIONS = {
    _PROFILE: 'Zed 本地应用数据',
    (*_PROFILE, 'db'): 'Zed 工作区与会话索引',
    (*_PROFILE, 'threads'): 'Zed 原生会话库与历史',
    ('.config', 'zed'): 'Zed 用户配置',
    ('.local', 'state', 'Zed'): 'Zed 运行状态',
    ('Library', 'Caches', 'Zed'): 'Zed 本地工具缓存',
    ('Library', 'Logs', 'Zed'): 'Zed 日志',
}
_PROTECTED_PREFIXES = {
    tuple(part.casefold() for part in location)
    for location in _LOCATIONS
}
_CHANNELS = ('stable', 'preview', 'nightly', 'dev')
_REMOTE_MIGRATION = 'thread-metadata-remote-connection-backfill'


def _path_list(value, order):
    if not isinstance(value, str) or not value or not isinstance(order, str):
        return ()
    paths = value.split('\n')
    indices = order.split(',')
    # Zed writes one order index per path. A mismatch can be a newline within a filename.
    if len(paths) != len(indices) or set(indices) != {str(index) for index in range(len(paths))}:
        return ()
    parsed = [recorded_path(path) for path in paths]
    return parsed if all(parsed) else ()


def _projects(metadata, path):
    # https://github.com/zed-industries/zed/blob/v1.30.2/crates/agent_ui/src/thread_metadata_store.rs
    # https://github.com/zed-industries/zed/blob/v1.30.2/crates/util/src/path_list.rs
    from .agent_adapters import _metadata_tables, _read_database_metadata

    def read_rows(database):
        names = {'sidebar_threads', 'kv_store'}
        if _metadata_tables(database, names) != names:
            metadata.issue(path, 'Zed 会话索引格式未受支持')
            return []
        if database.execute(
            "SELECT 1 FROM main.kv_store WHERE key = ? AND value = '1'", (_REMOTE_MIGRATION,)
        ).fetchone() is None:
            metadata.issue(path, 'Zed 会话的远程连接标记尚未完成迁移，本次仅盘点占用')
            return []
        # Only project paths and their serialization order are projected.
        return database.execute('''
            SELECT CASE WHEN length(CAST(folder_paths AS BLOB)) <= 32768 THEN folder_paths END,
                   CASE WHEN length(CAST(folder_paths_order AS BLOB)) <= 4096 THEN folder_paths_order END,
                   CASE WHEN length(CAST(main_worktree_paths AS BLOB)) <= 32768 THEN main_worktree_paths END,
                   CASE WHEN length(CAST(main_worktree_paths_order AS BLOB)) <= 4096 THEN main_worktree_paths_order END
            FROM main.sidebar_threads
            WHERE remote_connection IS NULL
            ORDER BY updated_at DESC
            LIMIT ?
        ''', (metadata.limit + 1,)).fetchall()

    rows = _read_database_metadata(metadata, path, _AGENT, read_rows) or ()
    if len(rows) > metadata.limit:
        metadata.issue(path, 'Zed 会话索引达到本次读取上限')
    for paths, order, main, main_order in rows[:metadata.limit]:
        yield from _path_list(paths, order)
        yield from _path_list(main, main_order)


def discover_zed(metadata, store, add):
    # https://github.com/zed-industries/zed/blob/v1.30.2/crates/paths/src/paths.rs
    # https://github.com/zed-industries/zed/blob/v1.30.2/crates/db/src/db.rs
    for location, title in _LOCATIONS.items():
        store(_AGENT, metadata.home.joinpath(*location), title)
    root = metadata.home.joinpath(*_PROFILE, 'db')
    if metadata.directory(root):
        for channel in _CHANNELS:
            for project in _projects(metadata, root / f'0-{channel}/db.sqlite'):
                add(str(project), _AGENT)


def protected_artifact(path):
    parts = tuple(part.casefold() for part in path.parts)
    return any(parts[index:index + size] in _PROTECTED_PREFIXES
               for index in range(len(parts) - 1) if parts[index] in ('library', '.local', '.config')
               for size in (2, 3))
