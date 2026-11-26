"""Local Charm Crush project evidence and data locations."""
from itertools import islice

from .integrations import recorded_path


_AGENT = 'Crush'
_DATABASE = 'crush.db'
_DATABASE_FILES = (_DATABASE, 'crush.db-wal', 'crush.db-shm', 'crush.db-journal', 'crush.lock')
_PROJECT_CONFIGS = ('crushrc', '.crushrc', 'crush.json', '.crush.json')
_GLOBAL_FILES = ('projects.json', 'projects.json.lock', 'crush.json', *_DATABASE_FILES)
_GLOBAL_PREFIXES = frozenset({('.local', 'share', 'crush'), ('.config', 'crush'), ('.cache', 'crush')})


def _project_storage(project, data, store):
    if data == project / '.crush':
        store(_AGENT, data, 'Crush 工程会话与本地数据')
    elif data is not None:
        # A custom location may also contain ordinary user files.
        for name in (*_DATABASE_FILES, 'crush.json'):
            store(_AGENT, data / name, 'Crush 自定义工程数据', file=True)
    for name in _PROJECT_CONFIGS:
        store(_AGENT, project / name, 'Crush 工程配置', file=True)


def _read_projects(metadata, root, store, add):
    registry = root / 'projects.json'
    value = metadata.read_json(registry, dict, limit=1024 * 1024)
    if value is None:
        return
    rows = value.get('projects')
    if not isinstance(rows, list):
        metadata.issue(registry, 'Crush 工程登记格式未受支持')
        return
    if len(rows) > metadata.limit:
        metadata.issue(registry, 'Crush 工程登记达到本次读取上限')
    for row in islice(rows, metadata.limit):
        if not isinstance(row, dict):
            continue
        project = recorded_path(row.get('path'))
        if project is None or project in (metadata.home, root) or not metadata.directory(project):
            continue
        add(str(project), _AGENT)
        _project_storage(project, recorded_path(row.get('data_dir')), store)


def discover_crush(metadata, store, add):
    # https://github.com/charmbracelet/crush/blob/v0.102.0/internal/projects/projects.go
    # https://github.com/charmbracelet/crush/blob/v0.102.0/internal/config/load.go
    for variable, relative, evidence in (
        ('XDG_DATA_HOME', '.local/share', 'Crush 全局登记、会话与本地数据'),
        ('XDG_CONFIG_HOME', '.config', 'Crush 用户配置'),
        ('XDG_CACHE_HOME', '.cache', 'Crush 本地工具缓存'),
    ):
        default = metadata.home / relative
        custom = metadata.configured(variable)
        for base in dict.fromkeys((default, custom or default)):
            root = base / 'crush'
            if store(_AGENT, root, evidence) and variable == 'XDG_DATA_HOME':
                _read_projects(metadata, root, store, add)
    configured = metadata.configured('CRUSH_GLOBAL_DATA')
    if configured is not None:
        for name in _GLOBAL_FILES:
            store(_AGENT, configured / name, 'Crush 自定义全局数据', file=True)
        _read_projects(metadata, configured, store, add)
    configured = metadata.configured('CRUSH_GLOBAL_CONFIG')
    if configured is not None:
        for name in ('crush.json', 'crushrc'):
            store(_AGENT, configured / name, 'Crush 自定义用户配置', file=True)


def discover_project_database(metadata, directory, store, add):
    # https://github.com/charmbracelet/crush/blob/v0.102.0/internal/db/connect.go
    root = directory / '.crush'
    if not metadata.directory(root):
        return
    path = root / _DATABASE
    try:
        with metadata.open(path) as stream:
            if stream is None or stream.read(16) != b'SQLite format 3\x00':
                return
    except OSError as error:
        metadata.failed(path, error)
        return
    add(str(directory), _AGENT)
    _project_storage(directory, root, store)


def protected_artifact(path):
    parts = path.parts
    return (path.name in (*_DATABASE_FILES, *_PROJECT_CONFIGS)
            or '.crush' in parts[:-1]
            or any(parts[index:index + size] in _GLOBAL_PREFIXES
                   for index in range(len(parts) - 1) if parts[index].startswith('.')
                   for size in (2, 3)))
