"""Read only the project metadata needed by the desktop inventory."""
import errno
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
from urllib.parse import unquote, urlsplit

from .actions import _directory
from .integrations import recorded_path


_MIB = 1024 * 1024
_SLUG = re.compile(r'[a-z0-9-]+')


def _inside(path, root):
    return path == root or root in path.parents


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

    def issue(self, path, message):
        self.issues.add(f'{path} · {message}')

    def allowed(self, path):
        return (any(_inside(path, root) for root in self.roots)
                and not any(_inside(path, root) for root in self.excluded)
                and not self.hidden.intersection(path.parts))

    def failed(self, path, error):
        if error.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            self.issue(path, '无法读取 Agent 元数据位置')

    def directory(self, path):
        if self.allowed(path):
            try:
                with _directory(path):
                    return True
            except OSError as error:
                self.failed(path, error)
        return False

    def children(self, path, *, directories=False, suffix=None, prefix=None):
        if not self.allowed(path):
            return []
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

    def configured(self, key):
        value = os.environ.get(key)
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
        uri = value.get('folder') if value is not None else None
        if not isinstance(uri, str) or not uri.startswith('file://') or len(uri) > 12288:
            continue
        try:
            parsed = urlsplit(uri)
            if (parsed.netloc.casefold() not in ('', 'localhost') or parsed.query or parsed.fragment
                    or not parsed.path or any(ord(ch) < 32 for ch in uri)
                    or re.search(r'%(?![\da-fA-F]{2})', parsed.path)):
                continue
            add(unquote(parsed.path, errors='strict'), 'Cursor')
        except (ValueError, UnicodeError):
            continue


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


def cline_installations(home: Path, excluded=(), *, authorized=()) -> list[tuple[Path, str]]:
    """Find Cline manifests in the local VS Code extension directories."""
    metadata = _Metadata(home, True, excluded, issues=None, authorized=authorized)
    identifier = 'saoudrizwan.claude-dev'
    roots = [home / '.vscode/extensions', home / '.vscode-insiders/extensions']
    configured = metadata.configured('VSCODE_EXTENSIONS')
    if configured is not None:
        roots.append(configured)
    found = []
    for root in dict.fromkeys(roots):
        obsolete = metadata.read_json(root / '.obsolete', dict) or {}
        for folder in metadata.children(root, directories=True, prefix=identifier + '-'):
            if obsolete.get(folder.name):
                continue
            manifest = metadata.read_json(folder / 'package.json', dict, limit=512 * 1024)
            if manifest is None or (manifest.get('publisher'), manifest.get('name')) != ('saoudrizwan', 'claude-dev'):
                continue
            version = manifest.get('version')
            if isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.+-]+)?', version):
                found.append((folder, version))
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


def discover(home: Path, quick: bool, *, excluded=(), issues=None,
             authorized=()) -> tuple[list[tuple[str, Path, str]], dict[Path, set[str]]]:
    """Return readable Agent store locations and local project-path candidates."""
    metadata = _Metadata(home, quick, excluded, issues, authorized)
    stores = {}
    projects = defaultdict(set)

    def store(agent, path, evidence):
        if not metadata.directory(path):
            return False
        stores[agent, path] = evidence
        return True

    def add(value, agent):
        path = recorded_path(value)
        if (path is not None and path != Path('/') and not metadata.hidden.intersection(path.parts)
                and not any(_inside(path, root) for root in metadata.excluded)):
            projects[path].add(agent)

    for adapter in (_cursor, _gemini, _opencode, _cline):
        adapter(metadata, store, add)
    return [(agent, path, evidence) for (agent, path), evidence in stores.items()], dict(projects)
