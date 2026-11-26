"""Read-only discovery for the Mac desktop's default inventory."""
import os
import plistlib
import re
import stat
from collections import defaultdict
from heapq import nlargest
from pathlib import Path
from xml.parsers.expat import ExpatError

from . import agent_adapters, integrations, model_inventory
from .actions import _directory


SESSION_STORES = {
    '.codex/sessions': ('Codex', 'Codex 会话', 'Codex 会话工作目录'),
    '.claude/projects': ('Claude Code', 'Claude Code 会话', 'Claude Code 会话工作目录'),
}
AGENT_BUNDLES = {
    'com.openai.codex': 'Codex',
    'com.todesktop.230313mzl4w4u92': 'Cursor',
    'co.anysphere.cursor.nightly': 'Cursor',
    'com.exafunction.windsurf': 'Windsurf',
    'com.anthropic.claudefordesktop': 'Claude Desktop',
    'dev.kiro.desktop': 'Kiro',
    'ai.openclaw.mac': 'OpenClaw',
    'ai.openclaw.mac.debug': 'OpenClaw',
    'com.deepseek.dsh': 'DeepSeek Harness',
    'com.conductor.app': 'Conductor',
    'com.ampcode.amp.macos': 'Amp',
    'com.trae.app': 'Trae',
}
AGENT_KINDS = {
    'Claude Desktop': 'desktop',
    'Cursor': 'ide', 'Windsurf': 'ide', 'Kiro': 'ide', 'Trae': 'ide',
    'Cline': 'extension', 'Roo Code': 'extension', 'GitHub Copilot VS Code': 'extension',
    'Continue': 'extension',
    'Claude Code': 'cli', 'OpenCode': 'cli', 'Gemini CLI': 'cli', 'Aider': 'cli',
    'GitHub Copilot CLI': 'cli', 'OpenClaw': 'cli', 'Qwen Code': 'cli', 'goose': 'cli',
    'DeepSeek Harness': 'desktop', 'Pi': 'cli',
    'Conductor': 'workspace-manager',
    'Amp': 'cli', 'Kiro CLI': 'cli',
}
AGENT_FAMILIES = {
    'GitHub Copilot CLI': 'GitHub Copilot', 'GitHub Copilot VS Code': 'GitHub Copilot',
    'Kiro CLI': 'Kiro',
}
HIDDEN_SCOPE_NAMES = {'.git', '.Trash', '.Trashes', f'.lumen-vault-{os.getuid()}'}


def within(path, root):
    return path == root or root in path.parents


def session_stores(home, issues, authorized):
    stores = {home / rel: source for rel, source in SESSION_STORES.items()}
    for rel, variable in (('.codex/sessions', 'CODEX_HOME'), ('.claude/projects', 'CLAUDE_CONFIG_DIR')):
        value = os.environ.get(variable)
        if not value:
            continue
        custom_home = integrations.recorded_path(value)
        if custom_home is None:
            continue
        index = custom_home / Path(rel).name
        if custom_home == home or not any(within(index, root) for root in (home, *authorized)):
            issues.add(variable + ' · 自定义位置不在本机自动识别范围内')
            continue
        agent, title, evidence = SESSION_STORES[rel]
        stores.setdefault(index, (agent, title, evidence + ' · ' + variable))
    return stores


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
    blocked = set(excluded)

    def failed(_):
        nonlocal partial
        partial = True

    for folder, names, files, fd in os.fwalk(root, onerror=failed, follow_symlinks=False):
        directory = Path(folder)
        included = [name for name in names if directory / name not in blocked]
        partial |= len(names) != len(included)
        names[:] = included
        for name in files:
            if directory / name in blocked:
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


def project_locations(home, sources, excluded, issues, quick=False, metadata_cache=None, extra_projects=None,
                      authorized=()):
    # Import the existing transcript reader to retain the same metadata semantics.
    from .scan import _cwd_records, _read

    projects = defaultdict(set)
    skipped = set()
    broad_folders = {home / p for p in ('Documents', 'Desktop', 'Downloads', 'Movies', 'Music', 'Pictures')}
    old_metadata = metadata_cache or {}
    new_metadata = {}

    def add(path, agent):
        if (path.is_absolute() and path != home
                and path not in broad_folders
                and not HIDDEN_SCOPE_NAMES.intersection(path.parts)
                and not any(within(path, root) for root in excluded)):
            parts = path.relative_to(home).parts if within(path, home) else ()
            openclaw_workspace = (agent == 'OpenClaw' and len(parts) >= 2
                                  and re.fullmatch(r'\.openclaw(?:-[\w-]+)?', parts[0])
                                  and re.fullmatch(r'workspace(?:-[\w-]+)?', parts[1]))
            checked = parts[1:] if openclaw_workspace else parts
            scoped_home = bool(parts) and not any(
                p.casefold() in {'library', 'applications', '.git', 'node_modules'} or p.startswith('.')
                for p in checked)
            if not (scoped_home or any(within(path, root) for root in authorized)):
                if path != Path('/') and not within(path, home) and not str(path).startswith(
                        ('/System/', '/Library/', '/private/', '/var/', '/tmp/', '/usr/')):
                    skipped.add(str(path))
                return
            try:
                if path.resolve(strict=True) == path and path.is_dir():
                    projects[path].add(agent)
            except (OSError, RuntimeError):
                pass

    for agent, shortcut in (('Codex', 'Documents/Codex'), ('Claude Code', 'Documents/Claude')):
        base = home / shortcut
        if any(within(base, p) for p in excluded):
            continue
        for folder in directories(base):
            if any(within(folder, p) for p in excluded):
                continue
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', folder.name):
                add(folder, agent)
                continue
            for thread in directories(folder):
                if thread in excluded:
                    continue
                work_path = thread / 'work'
                work = [] if work_path in excluded else directories(work_path)
                # Desktop-created checkouts often live one level below work.
                for project in work or [thread]:
                    add(project, agent)

    for index, (agent, _, _) in sources.items():
        sessions = []
        if any(within(index, p) for p in excluded) or HIDDEN_SCOPE_NAMES.intersection(index.parts):
            continue
        try:
            if index.resolve(strict=True) != index:
                continue
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError):
            issues.add(str(index) + ' · 无法读取会话位置')
            continue
        for folder, children, names, fd in os.fwalk(index, follow_symlinks=False,
                                             onerror=lambda _: issues.add(str(index) + ' · 无法读取部分位置')):
            directory = Path(folder)
            children[:] = [name for name in children if name not in HIDDEN_SCOPE_NAMES and directory / name not in excluded]
            for name in names:
                if not name.endswith('.jsonl'):
                    continue
                path = directory / name
                if path in excluded:
                    continue
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    row = dict(dev=info.st_dev, ino=info.st_ino, size=info.st_size,
                               mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        sessions.append((info.st_mtime_ns, path, row))
                except OSError:
                    continue
        budget = 128 * 1024 if quick else None
        sampled = False
        for _, path, row in nlargest(120, sessions):
            if row['size'] > 32 * 1024 * 1024:
                continue
            sampled |= budget is not None and row['size'] > budget
            try:
                identity = [row[key] for key in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')]
                cache_key = f'{agent}:{path}'
                previous = old_metadata.get(cache_key, {})
                entry_budget = budget if budget is not None and row['size'] > budget else None
                if (previous.get('identity') == identity and 'paths' in previous and
                        previous.get('budget') == entry_budget):
                    directories_found = _read(path, row, lambda _: previous['paths'])
                else:
                    directories_found = sorted(_read(
                        path, row, lambda stream: _cwd_records(stream, agent, max_bytes=budget)))
                new_metadata[cache_key] = dict(identity=identity, budget=entry_budget, paths=directories_found)
                for cwd in directories_found:
                    candidate = integrations.recorded_path(cwd)
                    if candidate is not None:
                        add(candidate, agent)
            except (OSError, ValueError, RecursionError):
                issues.add(str(index) + ' · 部分会话位置未能读取')
        if sampled:
            issues.add(str(index) + ' · 快速扫描读取会话开头，可用深度扫描查找后续工作目录')
    for path, owners in (extra_projects or {}).items():
        for agent in owners:
            add(Path(path), agent)
    if metadata_cache is not None:
        metadata_cache.clear()
        metadata_cache.update(new_metadata)
    return projects, skipped


def plan(home, system, excluded, quick=False, metadata_cache=None, authorized=(), adapter_cache=None):
    from .scan import SYSTEM_ROOTS

    issues = set()
    excluded = set(excluded) | SYSTEM_ROOTS
    sources = session_stores(home, issues, authorized)
    other_stores, other_projects = agent_adapters.discover(
        home, quick, excluded=excluded, issues=issues, authorized=authorized, cache=adapter_cache)
    projects, suggested = project_locations(home, sources, excluded, issues, quick=quick,
                                            metadata_cache=metadata_cache, extra_projects=other_projects,
                                            authorized=authorized)
    for agent, root, evidence in other_stores:
        sources.setdefault(root, (agent, evidence, evidence))
    common = {
        'Downloads': ('下载', '文件'),
        'Library/Caches': ('用户缓存', '资源库'),
        'Library/Logs': ('应用日志', '资源库'),
        'Library/Application Support/Code': ('VS Code 数据', '资源库'),
        'Library/Developer/Xcode/DerivedData': ('Xcode 构建缓存', '资源库'),
        '.cache/huggingface': ('Hugging Face 模型', '资源库'),
    }
    model_roots = {
        path: tool for path, tool in model_inventory.locations(home, os.environ)
        if path != home and any(within(path, root) for root in (home, *authorized))
    }
    model_metadata = set(model_inventory.metadata_locations(home))
    private = HIDDEN_SCOPE_NAMES | {'.ssh', '.aws', '.gnupg', '.kube', 'Keychains'}
    broad_scopes = {home, *(home / name for name in ('Library', 'Documents', 'Desktop', 'Downloads',
                                                   'Movies', 'Music', 'Pictures'))}

    def model_scope(path):
        return (path not in broad_scopes and any(within(path, root) for root in (home, *authorized))
                and not any(within(path, root) for root in excluded) and not private.intersection(path.parts))

    configured_comfy = model_inventory.configured_comfy_locations(home, os.environ, model_scope, issues)
    model_roots.update({path: 'ComfyUI' for path in sorted(configured_comfy) if model_scope(path)})
    model_roots = {path: tool for path, tool in model_roots.items() if model_scope(path)}
    candidates = {home / rel: group for rel, group in common.items()}
    candidates.update({path: (path.name, 'Agent 工程') for path in sorted(projects)})
    candidates.update({path: (title, 'Agent 数据') for path, (_, title, _) in sources.items()})
    candidates.update({path: (tool + ' 模型', '模型存储') for path, tool in model_roots.items()})
    candidates.update({path: (path.name, '模型存储') for path in sorted(model_metadata)})
    locations = []
    for path, (title, group) in candidates.items():
        if any(within(path, p) for p in excluded) or HIDDEN_SCOPE_NAMES.intersection(path.parts):
            continue
        try:
            info = path.lstat()
            if (stat.S_ISDIR(info.st_mode) or path in model_metadata and stat.S_ISREG(info.st_mode)
                    and info.st_nlink == 1) and path.resolve(strict=True) == path:
                location = dict(path=str(path), title=title, group=group,
                                agents=sorted(projects.get(path, ())), identity=[info.st_dev, info.st_ino],
                                directory=stat.S_ISDIR(info.st_mode))
                if path in sources:
                    location.update(agent_store=sources[path][0], evidence=sources[path][2])
                if group == '模型存储':
                    location['model_store'] = True
                locations.append(location)
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError):
            issues.add(str(path) + ' · macOS 未开放读取权限')
    found_extensions = agent_adapters.extension_installations(home, excluded=excluded, authorized=authorized)
    extensions = [dict(agent=agent, path=str(path), version=version, kind='extension', evidence='VS Code 扩展清单')
                  for agent, path, version in found_extensions]
    return locations, projects, installed_apps(home, system, excluded), issues, suggested, extensions


def agent_inventory(report, extensions=()):
    """Summarize ownership once per inode, including cross-Agent hard links."""
    stores = {row['path']: row for row in report['local_locations'] if row.get('agent_store')}
    agents = {}

    def agent(name):
        if name not in agents:
            agents[name] = dict(name=name, installed=False, projects=[], stores=[],
                                supports_events=name in ('Claude Code', 'Codex', 'Gemini CLI'),
                                has_verified_cleanup=False,
                                bytes=0, count=0, shared_bytes=0)
            agents[name]['family'] = AGENT_FAMILIES.get(name, name)
            agents[name]['kind'] = AGENT_KINDS.get(name, '')
        return agents[name]

    def count(entry, size, shared):
        entry['bytes'] += size
        entry['count'] += 1
        if shared:
            entry['shared_bytes'] += size

    for app in report['apps']:
        name = AGENT_BUNDLES.get(app['bundle_id'])
        if name:
            entry = agent(name)
            entry['installed'] = True
            entry.setdefault('installations', []).append(dict(path=app['path'], version=app['version'],
                                                               kind='app', evidence='macOS 应用包标识符'))
    for installation in extensions:
        name = installation['agent']
        entry = agent(name)
        entry['installed'] = True
        entry.setdefault('installations', []).append({key: installation[key] for key in
                                                      ('path', 'version', 'kind', 'evidence')})
    for path, names in report['projects'].items():
        for name in names:
            if name:
                agent(name)['projects'].append(path)
    indexed_stores = {}
    issue_paths = [Path(text.split(' · ', 1)[0]) for text in report['issues'] if text.startswith('/')]
    for path, row in stores.items():
        root = Path(path)
        partial = any(within(failed, root) or within(root, failed) for failed in issue_paths)
        item = dict(path=path, title=row['title'], evidence=row['evidence'], bytes=0, count=0,
                    shared_bytes=0, partial=partial)
        entry = agent(row['agent_store'])
        entry['stores'].append(item)
        if partial:
            entry['partial'] = True
        indexed_stores[path] = item

    linked = {}
    for row in report['files']:
        owners = row['agents']
        if not owners:
            continue
        store = row.get('agent_store_path')
        size = row['allocated_bytes']
        if row['nlink'] > 1:
            paths, names, _ = linked.setdefault((row['dev'], row['ino']), (set(), set(), size))
            if store:
                paths.add(store)
            names.update(owners)
            continue
        shared = (len(owners) > 1 or len(row['projects']) > 1 or row.get('shared_container')
                  or row.get('shared_owners') or len(row.get('model_versions', ())) > 1)
        if store:
            count(indexed_stores[store], size, shared)
        for name in owners:
            count(agent(name), size, shared)
    for paths, owners, size in linked.values():
        for path in paths:
            count(indexed_stores[path], size, True)
        for name in owners:
            count(agent(name), size, True)
    return [agents[name] for name in sorted(agents, key=str.casefold)]


def summarize(report):
    """One category per indexed inode, with application bodies already aggregated."""
    priority = ('apps', 'agent', 'library', 'downloads', 'media', 'models', 'other')
    rank = {key: position for position, key in enumerate(priority)}
    identities = {}
    for row in report['files']:
        kind = row['kind']
        if kind == 'model':
            category = 'models'
        elif row['projects'] or row['agents']:
            category = 'agent'
        else:
            path = Path(row['path'])
            if row.get('app_path') and within(path, Path(row['app_path'])):
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
        if previous is None or rank[category] < rank[previous[0]]:
            identities[identity] = category, row['allocated_bytes']
    totals = defaultdict(int)
    for category, size in identities.values():
        totals[category] += size
    totals['apps'] += sum(a.get('inventory_bytes', 0) for a in report['apps'])
    report['categories'] = [dict(id=key, bytes=totals[key]) for key in priority if totals[key]]
    report['indexed_bytes'] = sum(totals.values())
