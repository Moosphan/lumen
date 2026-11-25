"""Recognize supported developer caches without invoking the tools or reading payloads."""
import os
import re
import stat
import time
from collections import Counter, defaultdict
from pathlib import Path

from .actions import _directory, _inside
from .integrations import recorded_path
from .scan import SYSTEM_ROOTS


DEFAULTS = {
    '.npm/_cacache': 'npm',
    'Library/pnpm/store': 'pnpm',
    'Library/Caches/pip/http': 'pip_legacy',
    'Library/Caches/pip/http-v2': 'pip_http',
    '.cache/uv': 'uv',
    'Library/Caches/Homebrew/downloads': 'homebrew',
    'Library/Developer/Xcode/DerivedData': 'xcode',
    'Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw': 'docker',
}

CACHE_TYPES = {
    'npm': ('npm', 'npm 内容寻址缓存 _cacache', '保留 npm 索引、临时文件及未知结构。'),
    'pnpm': ('pnpm', 'pnpm 内容存储目录', '多个项目可能共享此存储中的硬链接或包数据。'),
    'pip_legacy': ('pip', 'pip 旧版单文件 HTTP 缓存', '旧版 HTTP 响应可重新请求。'),
    'pip_http': ('pip', 'pip http-v2 响应缓存', '响应元数据和正文成对保存。'),
    'uv': ('uv', 'uv 依赖缓存目录', 'Python 环境可能仍通过符号链接引用缓存。'),
    'homebrew': ('Homebrew', 'Homebrew 下载目录', '只审阅超过 120 天且符合下载命名的归档。'),
    'xcode': ('Xcode', 'Xcode DerivedData 的生成文件', '仅审阅已归属的索引、模块缓存和编译中间文件。'),
    'docker': ('Docker', 'Docker Desktop 默认虚拟磁盘 Docker.raw',
               '虚拟磁盘包含构建缓存、镜像、容器和卷，构建缓存的独立大小未知。'),
}

OVERRIDES = (
    ('npm_config_cache', '_cacache', 'npm'),
    ('NPM_CONFIG_CACHE', '_cacache', 'npm'),
    ('PNPM_HOME', 'store', 'pnpm'),
    ('XDG_DATA_HOME', 'pnpm/store', 'pnpm'),
    ('PIP_CACHE_DIR', 'http', 'pip_legacy'),
    ('PIP_CACHE_DIR', 'http-v2', 'pip_http'),
    ('XDG_CACHE_HOME', 'pip/http', 'pip_legacy'),
    ('XDG_CACHE_HOME', 'pip/http-v2', 'pip_http'),
    ('UV_CACHE_DIR', '', 'uv'),
    ('XDG_CACHE_HOME', 'uv', 'uv'),
    ('HOMEBREW_CACHE', 'downloads', 'homebrew'),
)

REVIEW_LAYOUTS = frozenset({'npm', 'pip_legacy', 'homebrew', 'xcode'})
# npm/cacache and pip/CacheControl document these sharded content paths in code.
# https://github.com/npm/cacache/blob/main/lib/content/path.js
# https://github.com/pypa/pip/blob/main/src/pip/_vendor/cachecontrol/caches/file_cache.py
NPM_CONTENT = re.compile(r'content-v2/sha512/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{124}')
PIP_HASH = re.compile(r'[0-9a-f]{56}')
# https://docs.brew.sh/Tips-and-Tricks#pre-download-a-file-for-a-formula
# https://docs.brew.sh/Manpage#cleanup-options-formulacask-
BREW_ARCHIVE = re.compile(r'[0-9a-f]{64}--[^/]+\.(?:tar\.(?:gz|bz2|xz|zst)|tgz|tbz2|zip|dmg|pkg|whl)')
BREW_STALE_NS = 120 * 86400 * 10**9
CACHE_TAG = b'Signature: 8a477f597d28d172789f06886806bc55d'
REVIEW_REASONS = {
    'npm': 'npm 的缓存内容，联网时可重新获取。隔离后可从操作记录恢复。',
    'pip_legacy': '旧版 pip 的单文件 HTTP 响应，联网时可重新请求。隔离后可从操作记录恢复。',
    'homebrew': '超过 120 天的 Homebrew 下载归档，隔离后可从操作记录恢复。',
}


def _identity(path, directory, tagged=False):
    try:
        if directory:
            with _directory(path) as fd:
                info = os.fstat(fd)
                if tagged:
                    if not stat.S_ISDIR(os.stat('archive-v0', dir_fd=fd, follow_symlinks=False).st_mode):
                        return None
                    tag = os.open('CACHEDIR.TAG', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        if not stat.S_ISREG(os.fstat(tag).st_mode) or os.read(tag, len(CACHE_TAG) + 1) != CACHE_TAG + b'\n':
                            return None
                    finally:
                        os.close(tag)
        else:
            with _directory(path.parent) as fd:
                info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                return None
        return [info.st_dev, info.st_ino]
    except (OSError, ValueError):
        return None


def _grant_spelling(path, grant):
    """Propose the cache root spelling for a grant that may share an ancestor."""
    root_parts = tuple(part.casefold() for part in path.parts)
    grant_parts = tuple(part.casefold() for part in grant.parts)
    if root_parts[:len(grant_parts)] == grant_parts:
        return grant.joinpath(*path.parts[len(grant_parts):])
    if grant_parts[:len(root_parts)] == root_parts:
        return Path(*grant.parts[:len(root_parts)])
    return None


def locations(home, excluded=(), *, authorized=(), env=None, scoped_only=False):
    """Verify known roots by descriptor. In scoped mode use these only for attribution."""
    home = Path(home)
    if not home.is_absolute() or home == Path('/') or '..' in home.parts:
        return []
    grants = {Path(path) for path in authorized if Path(path) != Path('/')}
    blocked = SYSTEM_ROOTS | {Path(path) for path in excluded}
    candidates = {home / relative: (layout, '') for relative, layout in DEFAULTS.items()}
    for key, suffix, layout in OVERRIDES:
        base = recorded_path((env or {}).get(key))
        if base is not None and base != Path('/'):
            path = base / suffix
            if path != home:
                candidates.setdefault(path, (layout, key))

    result = []
    for path, (layout, override) in candidates.items():
        if any(_inside(path, root) for root in blocked):
            continue
        spellings = {_grant_spelling(path, grant) for grant in grants
                     if not override or len(grant.parts) <= len(path.parts)} - {None}
        require_grant = scoped_only or bool(override)
        if require_grant and not spellings:
            continue
        directory = layout != 'docker'
        tagged = bool(override and layout == 'uv')
        identity = _identity(path, directory, tagged)
        if identity is None:
            continue
        aliases = sorted(str(alias) for alias in spellings - {path} if _identity(alias, directory) == identity)
        if require_grant and path not in spellings and not aliases:
            continue
        tool, evidence, _ = CACHE_TYPES[layout]
        if override:
            evidence += ' · ' + override + ' 与已授权目录'
        result.append(dict(path=str(path), aliases=aliases, title=evidence, group='开发缓存', identity=identity,
                           directory=directory, agents=[], developer_cache=tool, evidence=evidence,
                           cache_layout=layout, cache_review_root=layout in REVIEW_LAYOUTS, cache_tagged=tagged))
    return result


def _xcode_cache(parts):
    return len(parts) >= 2 and (parts[0] == 'ModuleCache.noindex' or
                               len(parts) >= 3 and parts[1] in {'Index.noindex', 'Index'} or
                               len(parts) >= 4 and parts[1:3] == ('Build', 'Intermediates.noindex'))


def _review(layout, relative, row, now):
    if layout == 'npm':
        return bool(NPM_CONTENT.fullmatch(relative.as_posix()))
    if layout == 'pip_legacy':
        return bool(PIP_HASH.fullmatch(relative.name) and relative.parts[:-1] == tuple(relative.name[:5]))
    if layout == 'homebrew':
        return (len(relative.parts) == 1 and bool(BREW_ARCHIVE.fullmatch(relative.name)) and
                now - row['mtime_ns'] >= BREW_STALE_NS)
    return layout == 'xcode' and row.get('cleanup_kind') == 'build' and row.get('bundle_id') == 'com.apple.dt.Xcode'


def classify(report, home, *, discovered=None, excluded=(), authorized=(), env=None):
    """Attribute scan rows and preserve unsupported entries before the action preview."""
    if discovered is None:
        discovered = locations(home, excluded, authorized=authorized, env=env)
    if not discovered:
        return []
    by_path = {}
    by_case = defaultdict(list)
    spellings = {}
    groups = {}
    physical = {}
    verified = {}
    for location in discovered:
        layout = location['cache_layout']
        root = Path(location['path'])
        tool, _, reason = CACHE_TYPES[layout]
        by_path[root] = location
        by_case[str(root).casefold()].append(root)
        spellings.update((Path(alias), root) for alias in location.get('aliases', ()))
        physical[root] = set()
        verified[root] = _identity(root, location['directory'], location.get('cache_tagged', False)) == location['identity']
        groups[root] = dict(id=f'{layout}:{root}', tool=tool, path=str(root), evidence=location['evidence'],
                            allocated_bytes=0, logical_bytes=0, review_bytes=0, files=0, review_paths=[],
                            sizing='vm_image' if layout == 'docker' else 'indexed_files', reason=reason)
        if layout == 'docker':
            groups[root]['isolated_build_cache_bytes'] = None

    spellings.update((root, root) for root in by_path)
    matched = []
    for row in report.get('files', ()):
        path = Path(row['path'])
        for candidate in (path, *path.parents):
            alternatives = by_case.get(str(candidate).casefold()) if candidate not in spellings else None
            if alternatives:
                spellings[candidate] = next((root for root in alternatives if verified[root] and
                    _identity(candidate, by_path[root]['directory']) == by_path[root]['identity']), None)
            root = spellings.get(candidate)
            if root is not None:
                matched.append((row, path, candidate, root))
                break

    # Case aliases can repeat a single-link file in the snapshot. Preserve both
    # spellings if either one carries protection or could create a second move.
    occurrences = Counter((root, (row['dev'], row['ino'])) for row, _, _, root in matched)
    static_targets = {Path(ref['target']) for ref in report.get('static_refs', ())}
    now = time.time_ns()
    for row, path, spelling, root in matched:
        location = by_path[root]
        layout = location['cache_layout']
        group = groups[root]
        relative = path.relative_to(spelling)
        identified = layout != 'xcode' or _xcode_cache(relative.parts)
        identity = row['dev'], row['ino']
        candidate = (identified and verified[root] and spelling == root and occurrences[root, identity] == 1
                     and row['decision'] == 'review' and row['nlink'] == 1
                     and row['kind'] != 'symlink' and not row.get('refs') and not row.get('projects')
                     and not row.get('shared_container') and not row.get('shared_owners')
                     and not any(p in static_targets for p in (path, *path.parents))
                     and (layout == 'xcode' or not row.get('app_path')) and _review(layout, relative, row, now))
        if not candidate and row['decision'] in ('safe', 'review'):
            reason = ('开发缓存根目录的身份已变化，请重新扫描。' if not verified[root] else
                      group['reason'] if identified else '保留 Xcode 项目的依赖、产物和其他数据。')
            row.update(decision='keep', reason=reason)
        if not identified:
            continue
        row.update(developer_cache=group['tool'], developer_cache_root=str(root),
                   developer_cache_evidence=group['evidence'], developer_cache_review=bool(candidate))
        if not row.get('owner'):
            row['owner'] = group['tool']
        if candidate:
            row['reason'] = REVIEW_REASONS.get(layout, row['reason'])
            group['review_paths'].append(row['path'])
            group['review_bytes'] += row['allocated_bytes']
        group['files'] += 1
        if identity not in physical[root]:
            physical[root].add(identity)
            group['allocated_bytes'] += row['allocated_bytes']
            group['logical_bytes'] += row['size']
    return sorted((group for group in groups.values() if group['files']), key=lambda group: (group['tool'], group['path']))
