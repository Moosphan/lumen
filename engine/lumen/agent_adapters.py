"""Read only the project metadata needed by the desktop inventory."""
import errno
from hashlib import sha256
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from itertools import islice
from pathlib import Path

from . import agent_ide, agent_local_tools
from .actions import _directory
from .integrations import recorded_path


_MIB = 1024 * 1024
_SLUG = re.compile(r'[a-z0-9-]+')
_CACHE_SCHEMA = 1
EXTENSIONS = {
    ('saoudrizwan', 'claude-dev'): 'Cline',
    ('github', 'copilot-chat'): 'GitHub Copilot VS Code',
    ('rooveterinaryinc', 'roo-cline'): 'Roo Code',
}


def _inside(path, root):
    return path == root or root in path.parents


def _stamp(path):
    # Check the entry through its real parent; never traverse a replaced symlink.
    try:
        with _directory(path.parent) as fd:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
    except OSError as error:
        return [error.errno]
    return [info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_mtime_ns, info.st_ctime_ns]


def _variable_stamp(key):
    value = os.environ.get(key)
    return sha256(os.fsencode(value)).hexdigest() if value is not None else None


class _Metadata:
    def __init__(self, home, quick, excluded, issues, authorized=()):
        self.home = home
        self.roots = (home, *authorized)
        self.excluded = tuple(excluded)
        self.hidden = {'.git', '.Trash', '.Trashes', f'.lumen-vault-{os.getuid()}'}
        self.issues = issues if issues is not None else set()
        self.limit = 240 if quick else 1600
        self.budget = (8 if quick else 32) * _MIB
        self.database_limit = (128 if quick else 512) * _MIB
        self.watched = None
        self.variables = {}

    def issue(self, path, message):
        self.issues.add(f'{path} · {message}')

    def allowed(self, path):
        return (any(_inside(path, root) for root in self.roots)
                and not any(_inside(path, root) for root in self.excluded)
                and not self.hidden.intersection(path.parts))

    def failed(self, path, error):
        if error.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            self.issue(path, '无法读取 Agent 元数据位置')

    def watch(self, path):
        if self.watched is not None and len(self.watched) <= 4096 and self.allowed(path):
            key = str(path)
            if key not in self.watched:
                self.watched[key] = _stamp(path)

    def directory(self, path):
        if self.allowed(path):
            self.watch(path)
            try:
                with _directory(path):
                    return True
            except OSError as error:
                self.failed(path, error)
        return False

    def children(self, path, *, directories=False, suffix=None, prefix=None):
        if not self.allowed(path):
            return []
        self.watch(path)
        try:
            with _directory(path) as fd, os.scandir(fd) as entries:
                found = []
                for index, entry in enumerate(islice(entries, self.limit + 1)):
                    if index == self.limit:
                        self.issue(path, 'Agent 元数据项目数超过本次读取上限')
                        break
                    child = path / entry.name
                    if (self.allowed(child) and (suffix is None or entry.name.endswith(suffix))
                            and (prefix is None or entry.name.startswith(prefix))
                            and (entry.is_dir(follow_symlinks=False) if directories else entry.is_file(follow_symlinks=False))):
                        found.append(child)
                return sorted(found)
        except OSError as error:
            self.failed(path, error)
            return []

    @contextmanager
    def open(self, path):
        if not self.allowed(path):
            yield None
            return
        self.watch(path)
        with _directory(path.parent) as fd:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                yield None
                return
            handle = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(handle, 'rb') as stream:
                current = os.fstat(stream.fileno())
                yield stream if (stat.S_ISREG(current.st_mode) and current.st_nlink == 1
                                 and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino)) else None

    def read(self, path, limit):
        try:
            with self.open(path) as stream:
                if stream is None:
                    return None
                info = os.fstat(stream.fileno())
                limit = min(limit, self.budget)
                if info.st_size > limit:
                    self.issue(path, 'Agent 元数据超过本次读取上限')
                    return None
                payload = stream.read(limit + 1)
                self.budget -= min(len(payload), self.budget)
                if len(payload) > limit:
                    self.issue(path, 'Agent 元数据超过本次读取上限')
                    return None
                return payload
        except OSError as error:
            self.failed(path, error)
            return None

    def read_json(self, path, expected, limit=32 * 1024):
        payload = self.read(path, limit)
        if payload is None:
            return None
        try:
            value = json.loads(payload)
            if isinstance(value, expected):
                return value
        except (ValueError, RecursionError):
            pass
        self.issue(path, 'Agent 元数据损坏或格式未受支持')
        return None

    def setting(self, key):
        value = os.environ.get(key)
        if self.watched is not None:
            self.variables[key] = _variable_stamp(key)
        return value

    def configured(self, key):
        value = self.setting(key)
        if value:
            path = recorded_path(value)
            if path is not None and any(_inside(path, root) or _inside(root, path) for root in self.roots):
                return path
            if path is not None:
                self.issue(path, f'{key} 指向当前用户目录之外')
        return None


def _cursor(metadata, store, add):
    # Cursor IDE 3.2.11 and VS Code's folder workspace metadata.
    # https://forum.cursor.com/t/159125
    user = metadata.home / 'Library/Application Support/Cursor'
    workspace = user / 'User/workspaceStorage'
    store('Cursor', workspace, 'Cursor IDE 工作区存储')
    store('Cursor', user / 'User/globalStorage', 'Cursor IDE 全局存储')
    store('Cursor', user / 'logs', 'Cursor IDE 日志')
    for folder in metadata.children(workspace, directories=True):
        value = metadata.read_json(folder / 'workspace.json', dict)
        directory = agent_ide._local_folder_uri(value.get('folder')) if value is not None else None
        if directory is not None:
            add(directory, 'Cursor')


def _gemini(metadata, store, add):
    # https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/config/projectRegistry.ts
    roots = [metadata.home / '.gemini', metadata.home / '.cache/.gemini']
    configured = metadata.configured('GEMINI_CLI_HOME')
    if configured is not None:
        roots.append(configured / '.gemini')
    for root in dict.fromkeys(roots):
        if not store('Gemini CLI', root, 'Gemini CLI 本地存储'):
            continue
        registry = root / 'projects.json'
        value = metadata.read_json(registry, dict, limit=2 * _MIB)
        projects = value.get('projects') if value is not None else {}
        if not isinstance(projects, dict):
            metadata.issue(registry, 'Gemini CLI 项目注册表格式未受支持')
            projects = {}
        markers = defaultdict(set)
        for base in (root / 'tmp', root / 'history'):
            for directory in metadata.children(base, directories=True):
                if not _SLUG.fullmatch(directory.name):
                    continue
                marker = directory / '.project_root'
                payload = metadata.read(marker, 4096)
                if payload is None:
                    continue
                try:
                    path = recorded_path(payload.decode('utf-8').strip())
                except UnicodeError:
                    path = None
                if path is not None:
                    markers[directory.name].add(path)
        if len(projects) > metadata.limit:
            metadata.issue(registry, 'Agent 元数据项目数超过本次读取上限')
        slug_counts = Counter(slug for slug in projects.values() if isinstance(slug, str))
        for raw_path, slug in islice(projects.items(), metadata.limit):
            if isinstance(slug, str) and _SLUG.fullmatch(slug) and slug not in markers:
                if slug_counts[slug] == 1:
                    add(raw_path, 'Gemini CLI')
                else:
                    metadata.issue(registry, 'Gemini CLI 项目标识重复')
        for paths in markers.values():
            if len(paths) == 1:
                add(str(next(iter(paths))), 'Gemini CLI')
            else:
                metadata.issue(root, 'Gemini CLI 项目所有权标记不一致')


def _cline(metadata, store, add):
    # v3.45.1 HistoryItem.cwdOnTaskInitialization and the later file-backed store.
    # https://github.com/cline/cline/blob/v3.45.1/src/shared/HistoryItem.ts
    # https://github.com/cline/cline/blob/main/.clinerules/storage.md
    roots = [(metadata.home / '.cline/data', 'tasks/taskHistory.json', 'Cline 共享任务存储')]
    for editor in ('Code', 'Code - Insiders'):
        root = metadata.home / 'Library/Application Support' / editor / 'User/globalStorage/saoudrizwan.claude-dev'
        roots.append((root, 'state/taskHistory.json', f'{editor} 扩展全局存储'))
    for root, index, evidence in roots:
        if not store('Cline', root, evidence):
            continue
        history = metadata.read_json(root / index, list, limit=4 * _MIB)
        if history is None:
            continue
        if len(history) > metadata.limit:
            metadata.issue(root / index, 'Agent 元数据项目数超过本次读取上限')
        for row in islice(history, metadata.limit):
            if isinstance(row, dict) and isinstance(row.get('id'), str) and row['id']:
                add(row.get('cwdOnTaskInitialization'), 'Cline')


def extension_installations(home: Path, excluded=(), *, authorized=()):
    """Find known extension manifests in the local VS Code directories."""
    metadata = _Metadata(home, True, excluded, issues=None, authorized=authorized)
    roots = [home / '.vscode/extensions', home / '.vscode-insiders/extensions']
    configured = metadata.configured('VSCODE_EXTENSIONS')
    if configured is not None:
        roots.append(configured)
    found = []
    for root in dict.fromkeys(roots):
        obsolete = metadata.read_json(root / '.obsolete', dict) or {}
        for (publisher, name), agent in EXTENSIONS.items():
            for folder in metadata.children(root, directories=True, prefix=f'{publisher}.{name}-'):
                if obsolete.get(folder.name):
                    continue
                manifest = metadata.read_json(folder / 'package.json', dict, limit=512 * 1024)
                if manifest is None or tuple(str(manifest.get(key, '')).casefold()
                                              for key in ('publisher', 'name')) != (publisher, name):
                    continue
                version = manifest.get('version')
                if isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.+-]+)?', version):
                    found.append((agent, folder, version))
    return found


@contextmanager
def _readonly_database(stream, info, limit):
    if sys.platform == 'darwin':
        # Darwin's /dev/fd reopens the pinned file even if the live path is replaced.
        uri = f'file:/dev/fd/{stream.fileno()}?mode=ro&immutable=1'
        with closing(sqlite3.connect(uri, uri=True)) as database:
            yield database
        return
    # Other SQLite VFSes may resolve descriptor symlinks back to the live path.
    if info.st_size > min(limit, 32 * _MIB):
        raise ValueError('database exceeds snapshot limit')
    payload = stream.read(info.st_size + 1)
    if len(payload) != info.st_size:
        raise ValueError('database changed during read')
    if hasattr(sqlite3.Connection, 'deserialize'):
        with closing(sqlite3.connect(':memory:')) as database:
            database.deserialize(payload)
            yield database
        return
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / 'snapshot.db'
        copy.write_bytes(payload)
        with closing(sqlite3.connect(copy.as_uri() + '?mode=ro&immutable=1', uri=True)) as database:
            yield database


def _opencode_sqlite(metadata, path):
    # v1 project/session tables and v2 session_v2/worktree tables.
    # https://github.com/anomalyco/opencode/blob/v1.2.15/packages/opencode/src/project/project.sql.ts
    # https://github.com/anomalyco/opencode/blob/dev/packages/core/src/worktree/sql.ts
    queries = {
        'project': ('id', 'worktree', 'sandboxes'),
        'worktree': ('project_id', 'directory'),
        'session': ('project_id', 'directory'),
        'session_v2': ('project_id', 'directory'),
    }
    result = {}
    try:
        with metadata.open(path) as stream:
            if stream is None:
                return result
            before = os.fstat(stream.fileno())
            if before.st_size > metadata.database_limit:
                metadata.issue(path, 'OpenCode 数据库超过本次读取上限')
                return result
            try:
                wal = path.with_name(path.name + '-wal')
                if metadata.allowed(wal):
                    metadata.watch(wal)
                    with _directory(wal.parent) as fd:
                        if os.stat(wal.name, dir_fd=fd, follow_symlinks=False).st_size:
                            metadata.issue(path, 'OpenCode 仅识别已写入主库的记录，部分会话可能待同步')
            except OSError as error:
                metadata.failed(wal, error)
            with _readonly_database(stream, before, metadata.database_limit) as database:
                database.execute('PRAGMA trusted_schema=OFF')
                database.execute('PRAGMA query_only=ON')
                database.execute('PRAGMA temp_store=MEMORY')
                attempts = iter(range(100))
                database.set_progress_handler(lambda: int(next(attempts, None) is None), 1000)
                tables = {name for (name,) in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('project','worktree','session','session_v2')"
                )}
                if not tables:
                    metadata.issue(path, 'OpenCode 数据库格式未受支持')
                for table, columns in queries.items():
                    if table not in tables:
                        continue
                    expressions = ', '.join(
                        f'CASE WHEN length(CAST({column} AS BLOB)) <= {32768 if column == "sandboxes" else 4096}'
                        f' THEN {column} END' for column in columns
                    )
                    try:
                        rows = database.execute(f'SELECT {expressions} FROM {table} LIMIT ?', (metadata.limit + 1,)).fetchall()
                        if len(rows) > metadata.limit:
                            metadata.issue(path, 'OpenCode 数据库记录数超过本次读取上限')
                        result[table] = rows[:metadata.limit]
                    except (sqlite3.Error, UnicodeError):
                        metadata.issue(path, 'OpenCode 数据库的部分记录损坏或格式未受支持')
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                metadata.issue(path, 'OpenCode 数据库读取期间有变更')
                return {}
    except OSError as error:
        metadata.failed(path, error)
    except (sqlite3.Error, ValueError):
        metadata.issue(path, 'OpenCode 数据库损坏、过大或格式未受支持')
        return {}
    return result


def _opencode(metadata, store, add):
    # v1.0.223 JSON layout and v1.2.15+ SQLite store use the XDG data root.
    # https://github.com/anomalyco/opencode/blob/v1.0.223/packages/opencode/src/storage/storage.ts
    data = [metadata.home / '.local/share']
    configured = metadata.configured('XDG_DATA_HOME')
    if configured is not None:
        data.append(configured)
    for directory in dict.fromkeys(data):
        root = directory / 'opencode'
        if not store('OpenCode', root, 'OpenCode 项目、会话与日志存储'):
            continue
        rows = _opencode_sqlite(metadata, root / 'opencode.db')
        for _, worktree, sandboxes in rows.get('project', ()):
            add(worktree, 'OpenCode')
            if isinstance(sandboxes, str):
                try:
                    sandboxes = json.loads(sandboxes)
                except (ValueError, RecursionError):
                    continue
                if isinstance(sandboxes, list):
                    for sandbox in islice(sandboxes, metadata.limit):
                        add(sandbox, 'OpenCode')
        for _, worktree in rows.get('worktree', ()):
            add(worktree, 'OpenCode')
        for table in ('session', 'session_v2'):
            for project, cwd in rows.get(table, ()):
                if project == 'global':
                    add(cwd, 'OpenCode')
        legacy = root / 'storage'
        for file in metadata.children(legacy / 'project', suffix='.json'):
            value = metadata.read_json(file, dict)
            if value is not None and value.get('id') == file.stem and file.stem != 'global':
                add(value.get('worktree'), 'OpenCode')
                sandboxes = value.get('sandboxes')
                if isinstance(sandboxes, list):
                    for sandbox in islice(sandboxes, metadata.limit):
                        add(sandbox, 'OpenCode')
        for file in metadata.children(legacy / 'session/global', suffix='.json'):
            value = metadata.read_json(file, dict)
            if value is not None and value.get('id') == file.stem and value.get('projectID') == 'global':
                add(value.get('directory'), 'OpenCode')
    caches = [metadata.home / '.cache']
    configured = metadata.configured('XDG_CACHE_HOME')
    if configured is not None:
        caches.append(configured)
    for directory in dict.fromkeys(caches):
        store('OpenCode', directory / 'opencode', 'OpenCode 缓存存储')


def _current(metadata, stamps, variables):
    if any(_variable_stamp(key) != digest for key, digest in variables.items()):
        return False
    for value, stamp in stamps.items():
        path = recorded_path(value)
        if (path is None or not metadata.allowed(path) or _stamp(path) != stamp
                or (len(stamp) == 1 and stamp[0] not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP))):
            return False
    return True


def discover(home: Path, quick: bool, *, excluded=(), issues=None, authorized=(), cache=None,
             adapters=None) -> tuple[list[tuple[str, Path, str]], dict[Path, set[str]]]:
    """Return readable Agent store locations and local project-path candidates."""
    metadata = _Metadata(home, quick, excluded, None, authorized)
    scope = [_CACHE_SCHEMA, str(home), quick, sorted(map(str, excluded)), sorted(map(str, authorized))]
    previous = cache.get('readers', {}) if cache is not None and cache.get('scope') == scope else {}
    current = {}
    all_stores = {}
    all_projects = defaultdict(set)

    def store(agent, path, evidence):
        if not metadata.directory(path):
            return False
        stores[agent, str(path)] = evidence
        return True

    def add(value, agent):
        path = recorded_path(value)
        if (path is not None and path != Path('/') and not metadata.hidden.intersection(path.parts)
                and not any(_inside(path, root) for root in metadata.excluded)):
            projects[str(path)].add(agent)

    readers = (_cursor, _gemini, _opencode, _cline, *agent_ide.ADAPTERS, *agent_local_tools.ADAPTERS)
    for adapter in readers if adapters is None else adapters:
        reader = adapter.__name__
        cached = previous.get(reader)
        budget = metadata.budget
        stores, projects = {}, defaultdict(set)
        if (cached and cached['budget'] == budget and
                _current(metadata, cached['stamps'], cached['variables'])):
            result = cached
            current[reader] = cached
            metadata.budget -= cached['spent']
        else:
            metadata.issues = set()
            metadata.watched = {} if cache is not None else None
            metadata.variables = {}
            adapter(metadata, store, add)
            result = dict(
                stores=[[name, path, evidence] for (name, path), evidence in stores.items()],
                projects={path: sorted(names) for path, names in projects.items()},
                issues=sorted(metadata.issues),
            )
            if (metadata.watched is not None and len(metadata.watched) <= 4096
                    and _current(metadata, metadata.watched, metadata.variables)):
                current[reader] = dict(result, budget=budget, spent=budget - metadata.budget,
                                       stamps=metadata.watched, variables=metadata.variables)
        for name, path, evidence in result['stores']:
            all_stores[name, Path(path)] = evidence
        for path, names in result['projects'].items():
            all_projects[Path(path)].update(names)
        if issues is not None:
            issues.update(result['issues'])
    if cache is not None:
        cache.clear()
        cache.update(scope=scope, readers=current)
    return [(agent, path, evidence) for (agent, path), evidence in all_stores.items()], dict(all_projects)
