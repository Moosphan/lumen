"""Read-only discovery for the Mac desktop's default inventory."""
import os
import plistlib
import re
import stat
from collections import defaultdict
from pathlib import Path
from xml.parsers.expat import ExpatError

from .actions import _directory


def within(path, root):
    return path == root or root in path.parents


def directories(folder, limit=250):
    try:
        if folder.resolve(strict=True) != folder:
            return []
        with os.scandir(folder) as entries:
            found = [Path(e.path) for e in entries if not e.name.startswith('.') and e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    return sorted(found, key=lambda path: path.name, reverse=True)[:limit]


def installed_apps(home, system, excluded):
    folders = [home / 'Applications']
    if system:
        folders.extend((Path('/Applications'), Path('/System/Applications')))
    apps = {}
    seen = set()
    for folder in folders:
        for sub in [folder] + [p for p in directories(folder) if p.suffix.casefold() != '.app']:
            for app in directories(sub, 1000):
                if app.suffix.casefold() != '.app' or any(within(app, p) for p in excluded):
                    continue
                try:
                    with _directory(app / 'Contents') as fd:
                        handle = os.open('Info.plist', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                        with os.fdopen(handle, 'rb') as plist:
                            if not stat.S_ISREG(os.fstat(plist.fileno()).st_mode):
                                continue
                            metadata = plistlib.loads(plist.read(2 * 1024 * 1024))
                    identifier = metadata.get('CFBundleIdentifier')
                    if not isinstance(identifier, str) or not identifier:
                        continue
                    size, partial = bundle_size(app, seen, excluded)
                except (OSError, ValueError, TypeError, AttributeError, OverflowError, ExpatError, plistlib.InvalidFileException):
                    continue
                apps[app] = {
                    'path': str(app), 'name': str(metadata.get('CFBundleDisplayName') or metadata.get('CFBundleName') or app.stem),
                    'bundle_id': identifier, 'version': str(metadata.get('CFBundleShortVersionString') or ''),
                    'inventory_bytes': size, 'size_partial': partial, 'system': str(app).startswith('/System/'),
                }
    return apps


def bundle_size(root, linked, excluded):
    size, partial = 0, False

    def failed(_):
        nonlocal partial
        partial = True

    for folder, names, files, fd in os.fwalk(root, onerror=failed, follow_symlinks=False):
        names[:] = [name for name in names if not any(within(Path(folder) / name, p) for p in excluded)]
        for name in files:
            if any(within(Path(folder) / name, p) for p in excluded):
                partial = True
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                partial = True
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_nlink > 1:
                identity = (info.st_dev, info.st_ino)
                if identity in linked:
                    continue
                linked.add(identity)
            size += info.st_blocks * 512
    return size, partial


def project_locations(home, excluded, issues):
    # Import the existing transcript reader to retain the same metadata semantics.
    from .scan import _cwd_records, _read

    projects = defaultdict(set)
    skipped = set()

    def add(path, agent):
        if (path.is_absolute() and path != home and within(path, home)
                and path not in {home / p for p in ('Documents', 'Desktop', 'Downloads', 'Movies', 'Music', 'Pictures')}
                and not any(within(path, root) for root in excluded)
                and not any(p.casefold() in {'library', 'applications', '.git', 'node_modules'} or p.startswith('.')
                            for p in path.relative_to(home).parts)):
            try:
                if path.resolve(strict=True) == path and path.is_dir():
                    projects[path].add(agent)
            except (OSError, RuntimeError):
                pass
        elif path.is_absolute() and path != Path('/') and not within(path, home) and not str(path).startswith(('/System/', '/Library/', '/private/', '/var/', '/tmp/', '/usr/')):
            skipped.add(str(path))

    for agent, shortcut in (('Codex', 'Documents/Codex'), ('Claude Code', 'Documents/Claude')):
        for folder in directories(home / shortcut):
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', folder.name):
                add(folder, agent)
                continue
            for thread in directories(folder):
                work = directories(thread / 'work')
                # Desktop-created checkouts often live one level below work.
                for project in work or [thread]:
                    add(project, agent)

    index_roots = (('Codex', '.codex/sessions'), ('Claude Code', '.claude/projects'))
    for agent, shortcut in index_roots:
        index = home / shortcut
        sessions = []
        if any(within(index, p) for p in excluded):
            continue
        try:
            if index.resolve(strict=True) != index:
                continue
        except FileNotFoundError:
            continue
        except OSError:
            issues.add(str(index) + ' · 无法读取会话位置')
            continue
        for folder, _, names, fd in os.fwalk(index, follow_symlinks=False,
                                             onerror=lambda _: issues.add(str(index) + ' · 无法读取部分位置')):
            for name in names:
                if not name.endswith('.jsonl'):
                    continue
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    row = dict(dev=info.st_dev, ino=info.st_ino, size=info.st_size,
                               mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
                    path = Path(folder) / name
                    if stat.S_ISREG(info.st_mode) and not any(within(path, p) for p in excluded):
                        sessions.append((info.st_mtime_ns, path, row))
                except OSError:
                    continue
        for _, path, row in sorted(sessions, reverse=True)[:120]:
            if row['size'] > 32 * 1024 * 1024:
                continue
            try:
                for cwd in _read(path, row, lambda stream: _cwd_records(stream, agent)):
                    candidate = Path(cwd)
                    if candidate.is_absolute() and '..' not in candidate.parts:
                        add(candidate, agent)
            except (OSError, ValueError, RecursionError):
                issues.add(str(index) + ' · 部分会话位置未能读取')
    return projects, skipped


def plan(home, system, excluded):
    issues = set()
    projects, suggested = project_locations(home, excluded, issues)
    common = {
        'Downloads': ('下载', '文件'),
        'Library/Caches': ('用户缓存', '资源库'),
        'Library/Logs': ('应用日志', '资源库'),
        'Library/Application Support/Code': ('VS Code 数据', '资源库'),
        'Library/Developer/Xcode/DerivedData': ('Xcode 构建缓存', '资源库'),
        '.cache/huggingface': ('Hugging Face 模型', '资源库'),
        '.ollama/models': ('Ollama 模型', '资源库'),
    }
    locations = []
    for path, (title, group) in [(home / rel, meta) for rel, meta in common.items()] + [
            (p, (p.name, 'Agent 工程')) for p in sorted(projects)]:
        if any(within(path, p) for p in excluded):
            continue
        try:
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) and path.resolve(strict=True) == path:
                locations.append(dict(path=str(path), title=title, group=group,
                                      agents=sorted(projects.get(path, ())), identity=[info.st_dev, info.st_ino]))
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError):
            issues.add(str(path) + ' · macOS 未开放读取权限')
    return locations, projects, installed_apps(home, system, excluded), issues, suggested


def summarize(report):
    """One category per indexed inode, with application bodies already aggregated."""
    priority = ('apps', 'agent', 'library', 'downloads', 'media', 'models', 'other')
    identities = {}
    for row in report['files']:
        path = Path(row['path'])
        kind = row['kind']
        if kind == 'model':
            category = 'models'
        elif row['projects'] or row['agents']:
            category = 'agent'
        elif row.get('app_path') and within(path, Path(row['app_path'])):
            category = 'apps'
        elif row['location'] == 'Library' or 'Library' in path.parts:
            category = 'library'
        elif 'Downloads' in path.parts:
            category = 'downloads'
        elif kind in ('video', 'image', 'audio'):
            category = 'media'
        else:
            category = 'other'
        identity = row['dev'], row['ino']
        previous = identities.get(identity)
        if previous is None or priority.index(category) < priority.index(previous[0]):
            identities[identity] = category, row['allocated_bytes']
    totals = defaultdict(int)
    for category, size in identities.values():
        totals[category] += size
    totals['apps'] += sum(a.get('inventory_bytes', 0) for a in report['apps'])
    report['categories'] = [dict(id=key, bytes=totals[key]) for key in priority if totals[key]]
    report['indexed_bytes'] = sum(totals.values())
