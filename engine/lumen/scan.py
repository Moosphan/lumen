"""Read-only inventory. All discovery starts inside caller-selected roots."""
import ctypes
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import time
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from xml.parsers.expat import ExpatError
from xml.etree.ElementTree import ParseError

from . import integrations


MODELS = {'.gguf', '.safetensors', '.ckpt', '.onnx', '.pt', '.pth', '.ggml'}
MEDIA = {
    'image': {'.png', '.jpg', '.jpeg', '.heic', '.gif', '.webp', '.svg', '.tiff'},
    'audio': {'.aiff', '.wav', '.mp3', '.flac', '.m4a', '.ogg', '.aac'},
    'video': {'.mp4', '.mov', '.webm', '.mkv', '.m4v', '.avi'},
}
DEEP_DIRS = {'Containers', 'Group Containers', 'Application Support', 'Developer'}
LIMIT = 4 * 1024 * 1024
SYSTEM_ROOTS = set(map(Path, ('/System', '/usr', '/bin', '/sbin', '/dev', '/private/etc',
                             '/private/var/db', '/private/var/root')))


def _paths(values):
    if isinstance(values, (str, os.PathLike)):
        values = [values]
    return {Path(os.path.abspath(p)) for p in (values or [])}


def _under(path, roots):
    return any(path == r or r in path.parents for r in roots)


def _agent(path):
    if '.codex' in path.parts:
        return 'Codex'
    if '.claude' in path.parts:
        return 'Claude Code'
    return ''


def _kind(path):
    suffix = path.suffix.lower()
    if suffix in MODELS or 'models--' in str(path) and 'blobs' in path.parts:
        return 'model'
    for kind, exts in MEDIA.items():
        if suffix in exts:
            return kind
    if suffix in {'.html', '.htm'}:
        return 'html'
    if suffix == '.log':
        return 'log'
    if set(path.parts) & {'build', 'dist', 'DerivedData', 'node_modules', 'target'}:
        return 'build'
    if set(path.parts) & {'Caches', '.cache', '__pycache__'}:
        return 'cache'
    return 'other'


# The default macOS data roots are part of the match. Ordinary project files
# named Cookies, Current Tabs or Bookmarks have no special meaning on their own.
CHROMIUM_DATA_ROOTS = {
    ('google', name) for name in ('chrome', 'chrome beta', 'chrome dev', 'chrome canary', 'chrome for testing')
} | {
    (name,) for name in ('chromium', 'microsoft edge', 'microsoft edge beta', 'microsoft edge dev', 'microsoft edge canary')
} | {
    ('bravesoftware', name) for name in ('brave-browser', 'brave-browser-beta', 'brave-browser-nightly')
}
PROFILE_FILES = {
    'bookmarks', 'reading list', 'login data', 'login data for account', 'web data', 'account web data',
    'cookies', 'bound session cookies', 'network persistent state', 'history', 'last session', 'current session', 'last tabs', 'current tabs',
    'preferences', 'secure preferences', 'favicons', 'top sites', 'affiliation database',
}
PROFILE_STORES = {
    'sessions', 'local storage', 'session storage', 'indexeddb', 'service worker', 'file system',
    'storage', 'webstorage', 'databases', 'application cache', 'sync data', 'local extension settings',
    'sync extension settings', 'extension state', 'extensions', 'sharedstorage', 'offline pages',
    'saved tab groups', 'web applications', 'blob_storage', 'network', 'shared dictionary',
    'shared_proto_db', 'shared_proto_db_metadata', 'platform notifications', 'background fetch',
}
SAFARI_IDS = {'com.apple.safari', 'com.apple.safaritechnologypreview'}
SIDECAR = re.compile(r'(-wal|-shm|-journal|\.bak|\.tmp)$')


def _profile_protection(path):
    parts = tuple(part.casefold() for part in path.parts)
    for index, part in enumerate(parts):
        if part != 'library':
            continue
        rel = parts[index + 1:]
        if not rel:
            continue
        if rel[0] == 'group containers' and len(rel) >= 3:
            return 'shared_container'
        if rel[0] in ('safari', 'safari technology preview', 'keychains', 'cookies'):
            return 'browser_data'
        if rel[0] == 'saved application state' and len(rel) >= 3 and rel[1].removesuffix('.savedstate') in SAFARI_IDS:
            return 'browser_data'
        if len(rel) >= 2 and rel[0] in ('webkit', 'httpstorages') and (
                rel[1] in SAFARI_IDS or rel[1].removesuffix('.binarycookies') in SAFARI_IDS):
            return 'browser_data'
        if (len(rel) >= 5 and rel[0] == 'containers' and rel[1] in SAFARI_IDS
                and rel[2:4] == ('data', 'library')
                and rel[4] in ('safari', 'cookies', 'webkit', 'httpstorages', 'saved application state')):
            return 'browser_data'
        if rel[0] != 'application support':
            continue
        product = rel[1:]
        if product[:2] == ('firefox', 'profiles') or product in {
                ('firefox', 'profiles.ini'), ('firefox', 'installs.ini')}:
            return 'browser_data'
        prefix = next((root for root in (product[:2], product[:1]) if root in CHROMIUM_DATA_ROOTS), None)
        if prefix:
            relative = product[len(prefix):]
            # The Chromium profile directory can have a custom name. Require an
            # immediate profile child with the exact store/file layout instead.
            if len(relative) == 1 and SIDECAR.sub('', relative[0]) == 'local state':
                return 'browser_data'
            if len(relative) >= 2 and relative[1] in PROFILE_STORES:
                return 'browser_data'
            if len(relative) == 2 and SIDECAR.sub('', relative[1]) in PROFILE_FILES:
                return 'browser_data'
    return ''



def _signature(s):
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _read(path, row, consumer):
    # Reopen by descriptor through each ancestor so a directory swapped for a
    # symlink during analysis cannot redirect a content read.
    flags = os.O_RDONLY | os.O_NOFOLLOW
    parent = os.open(path.anchor, flags | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    expected = tuple(row[k] for k in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _signature(before) != expected:
            raise OSError('文件身份或大小已改变')
        result = consumer(stream)
        if _signature(os.fstat(fd)) != expected:
            raise OSError('读取时文件发生变化')
        return result


def _small(stream):
    data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('超过单文件解析上限 4 MiB')
    return data


def _digest(stream):
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(chunk)
    return h.hexdigest()


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.base = None
        self.links = []
        self.partial = False

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if value is None:
                continue
            if tag == 'base' and key == 'href':
                if self.base is None:
                    self.base = value
            elif key in ('href', 'src'):
                self.links.append((value, self.getpos()[0]))
            elif key in ('style', 'srcset') or key.startswith('on'):
                self.partial = True
        if tag in ('script', 'style'):
            self.partial = True


def _cwd_records(stream, agent):
    dirs = set()
    # A transcript can be large. Bounded physical lines avoid loading messages
    # wholesale while still reading later records that may change directories.
    for line in iter(lambda: stream.readline(LIMIT + 1), b''):
        if len(line) > LIMIT:
            raise ValueError('会话记录行超过解析上限')
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            continue
        if agent == 'Codex':
            if obj.get('type') not in ('session_meta', 'turn_context'):
                continue
            obj = obj.get('payload')
        if isinstance(obj, dict) and isinstance(obj.get('cwd'), str):
            dirs.add(obj['cwd'])
    return dirs


def _idle(path):
    # lsof returns 1 with no diagnostics when no process holds the path.
    try:
        result = subprocess.run(['/usr/sbin/lsof', '-nP', '-F', 'p', '--', str(path)],
                                capture_output=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 1 and not result.stderr and not result.stdout


def _registered_code():
    """Ask AppKit about stable Code bundle IDs only, without reading process arguments."""
    ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
    objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    ptr = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ptr
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ptr
    selector = objc.sel_registerName
    msg = ('objc_msgSend', objc)
    send = ctypes.CFUNCTYPE(ptr, ptr, ptr)(msg)
    send_arg = ctypes.CFUNCTYPE(ptr, ptr, ptr, ptr)(msg)
    count = ctypes.CFUNCTYPE(ctypes.c_ulong, ptr, ptr)(msg)
    at = ctypes.CFUNCTYPE(ptr, ptr, ptr, ctypes.c_ulong)(msg)
    get_path = ctypes.CFUNCTYPE(ctypes.c_char_p, ptr, ptr)(msg)
    pool = send(objc.objc_getClass(b'NSAutoreleasePool'), selector(b'new'))
    try:
        workspace = send(objc.objc_getClass(b'NSWorkspace'), selector(b'sharedWorkspace'))
        runner = objc.objc_getClass(b'NSRunningApplication')
        if not workspace or not runner:
            raise ValueError('AppKit 注册服务不可用')
        bundle_id = None
        for suffix in ('', '.helper', '.helper.GPU', '.helper.Plugin', '.helper.Renderer'):
            name = ctypes.c_char_p(('com.microsoft.VSCode' + suffix).encode())
            string = send_arg(objc.objc_getClass(b'NSString'), selector(b'stringWithUTF8String:'), name)
            running = send_arg(runner, selector(b'runningApplicationsWithBundleIdentifier:'), string)
            if not running:
                raise ValueError('无法查询 VS Code 应用或辅助进程')
            if count(running, selector(b'count')):
                return True, []
            if not suffix:
                bundle_id = string
        urls = send_arg(workspace, selector(b'URLsForApplicationsWithBundleIdentifier:'), bundle_id)
        if not urls:
            raise ValueError('无法查询 VS Code 安装位置')
        apps = []
        for index in range(count(urls, selector(b'count'))):
            url = at(urls, selector(b'objectAtIndex:'), index)
            path = get_path(url, selector(b'fileSystemRepresentation'))
            if not path:
                raise ValueError('无法解析注册安装位置')
            apps.append(Path(os.fsdecode(path)))
        return False, apps
    finally:
        ctypes.CFUNCTYPE(None, ptr, ptr)(msg)(pool, selector(b'drain'))


def _code_stopped(app):
    """Return (stopped, reason). A failed probe never authorizes log removal."""
    try:
        active, registered = _registered_code()
        if active:
            return False, 'VS Code 编辑器或辅助进程仍在运行'
        images = set()
        for bundle in set(registered) | {app}:
            try:
                bundle.stat()
            except FileNotFoundError:
                if bundle == app:
                    return False, '已确认的 VS Code 安装已移动'
                continue  # LaunchServices can retain obsolete installation URLs.
            fw = bundle / 'Contents/Frameworks/Electron Framework.framework'
            framework = fw / 'Electron Framework'
            # Electron maps this image in its main, GPU, renderer and plugin
            # processes even when a helper is absent from LaunchServices.
            framework.stat()
            images.add(framework)
            for file in (bundle / 'Contents/MacOS/Electron', fw / 'Helpers/chrome_crashpad_handler'):
                try:
                    file.stat()
                except FileNotFoundError:
                    continue
                images.add(file)
        # Query only executable images belonging to the confirmed/registered
        # applications. No recursive walk of other installations is needed.
        check = subprocess.run(['/usr/sbin/lsof', '-nP', '-t', '--', *sorted(map(str, images))],
                               capture_output=True, timeout=12)
        if check.stdout:
            return False, 'VS Code 编辑器、辅助进程或崩溃报告进程仍在使用程序文件'
        if check.returncode != 1 or check.stderr.strip():
            return False, f'无法核对 VS Code 程序文件占用（lsof {check.returncode}）'
        return True, ''
    except OSError as exc:
        return False, f'查询 VS Code 安装或进程失败（errno {exc.errno}）'
    except (ValueError, AttributeError, subprocess.TimeoutExpired) as exc:
        return False, f'无法确认 VS Code 退出状态（{type(exc).__name__}）'


def _space(files):
    physical = {}
    for row in files.values():
        key = (row['dev'], row['ino'])
        physical.setdefault(key, []).append(row)
    allocated = shared = logical = 0
    for paths in physical.values():
        first = paths[0]
        logical += first['size']
        allocated += first['allocated_bytes']
        if (first['nlink'] > 1 or any(row.get('shared_container') for row in paths) or
                len({p for row in paths for p in row['projects']}) > 1 or
                any(row.get('shared_owners') or len(row.get('model_versions', [])) > 1 for row in paths)):
            shared += first['allocated_bytes']
    return dict(logical_bytes=logical, allocated_bytes=allocated,
                shared_allocated_bytes=shared, unique_files=len(physical))


def scan(roots, deep=True, projects=None, keep=None, exclude=None, hash_cache=None):
    root_set, exclusions, kept = _paths(roots), _paths(exclude), _paths(keep)
    issues, files, directories, repositories = set(), {}, set(), set()
    apps, app_ids, duplicates = {}, defaultdict(list), {}
    link_targets, volumes = {}, {}
    static_refs = set()
    suggested_projects = set()
    project_agents = {Path(os.path.abspath(p)): set([a] if isinstance(a, str) else a)
                      for p, a in (projects or {}).items()}

    def issue(p, why):
        issues.add(f'{p} · {why}')

    def defer(p, why):
        files[p].update(decision='defer', reason=why)
        issue(p, why)

    def protect(p, why):
        if files[p]['decision'] != 'defer':
            files[p].update(decision='keep', reason=why)

    def register(p, s):
        link = stat.S_ISLNK(s.st_mode)
        if not link and not stat.S_ISREG(s.st_mode):
            issue(p, '特殊文件，仅跳过')
            return
        kind = 'symlink' if link else _kind(p)
        owner_agent = _agent(p)
        files[p] = dict(path=str(p), dev=s.st_dev, ino=s.st_ino, size=s.st_size,
                        mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns, nlink=s.st_nlink, kind=kind, allocated_bytes=s.st_blocks * 512,
                        owner='', app_path='', bundle_id='', duplicate_group='',
                        agents=[owner_agent] if owner_agent else [], projects=[], refs=[],
                        location='Library' if 'Library' in p.parts else '其他位置',
                        decision='review', reason='需要核对用途，当前证据不足以直接清理')
        if link or s.st_nlink > 1:
            protect(p, '符号链接，仅盘点链接本身' if link else '硬链接共享底层数据')
        elif kind == 'model':
            protect(p, '离线模型，移除可能需要重新下载')
            parts = p.parts
            version = parts.index('snapshots') + 1 if 'snapshots' in parts else len(parts)
            files[p]['model_version'] = parts[version] if version < len(parts)-1 else ''
        elif owner_agent or p.suffix.lower() in ('.sqlite', '.sqlite3', '.db', '.plist', '.keychain-db') or '.git' in p.parts:
            protect(p, '配置、会话或数据库需要保留')
        elif p.suffix.lower() in {'.part', '.download', '.crdownload', '.partial'}:
            defer(p, '可能正在下载，完成后重扫')
        elif kind == 'build':
            files[p]['reason'] = '可能是构建产物，移除后可能需要重新构建'
        if any(parent.suffix.lower() == '.app' for parent in p.parents) or _under(p, [Path('/Library')]):
            protect(p, '应用本体或机器级资源库，仅盘点')
        protection = _profile_protection(p)
        if protection:
            files[p][protection] = True
            reason = ('共享容器的使用方尚未核实，保留数据' if protection == 'shared_container'
                      else '浏览器配置、登录凭据、会话或离线资料需要保留')
            protect(p, reason)
        if _under(p, kept):
            protect(p, '用户已标记保留')

    # fwalk uses directory descriptors and does not descend through symlinks.
    for root in sorted(root_set):
        if root == Path('/') or _under(root, exclusions | SYSTEM_ROOTS) or '.git' in root.parts:
            issue(root, '用户排除或系统保护')
            continue
        try:
            if root.parent.resolve() != root.parent:
                issue(root, '上级路径包含符号链接，请选择实际目录')
                continue
            s = root.lstat()
            if s.st_dev not in volumes and not stat.S_ISLNK(s.st_mode):
                v = os.statvfs(root)
                volumes[s.st_dev] = dict(dev=s.st_dev, path=str(root), total_bytes=v.f_blocks * v.f_frsize,
                                         available_bytes=v.f_bavail * v.f_frsize,
                                         used_bytes=(v.f_blocks - v.f_bfree) * v.f_frsize)
            if not stat.S_ISDIR(s.st_mode):
                register(root, s)
                if stat.S_ISLNK(s.st_mode):
                    link_targets[root] = Path(os.path.normpath(root.parent / os.readlink(root)))
                continue
            def walk_error(exc):
                issue(exc.filename or root, str(exc))
            for folder, names, leaves, fd in os.fwalk(root, onerror=walk_error, follow_symlinks=False):
                directory = Path(folder)
                directories.add(directory)
                if directory.suffix.lower() == '.app' and not any(a.suffix.lower() == '.app' for a in directory.parents):
                    apps[directory] = dict(name=directory.stem, path=str(directory), bundle_id='', version='')
                if '.git' in names or '.git' in leaves:
                    repositories.add(directory)
                for name in sorted(names + leaves):
                    p = directory / name
                    skip = (_under(p, exclusions) or name == '.git' or p in SYSTEM_ROOTS
                            or not deep and directory.name == 'Library' and name in DEEP_DIRS)
                    if skip:
                        if name in names:
                            names.remove(name)
                        if name != '.git':
                            issue(p, '用户排除、系统保护或不在快速扫描范围')
                        continue
                    try:
                        child = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if not stat.S_ISDIR(child.st_mode):
                            register(p, child)
                            if stat.S_ISLNK(child.st_mode):
                                dest = os.readlink(name, dir_fd=fd)
                                link_targets[p] = Path(os.path.normpath(directory / dest))
                            if name in names:
                                names.remove(name)
                    except OSError as exc:
                        issue(p, str(exc))
        except (OSError, RuntimeError) as exc:
            issue(root, str(exc))

    for app, info in apps.items():
        plist = app / 'Contents/Info.plist'
        if plist not in files:
            issue(app, '缺少可读的 Info.plist，应用版本未知')
            continue
        try:
            raw = plistlib.loads(_read(plist, files[plist], _small))
            info.update(name=str(raw.get('CFBundleDisplayName') or raw.get('CFBundleName') or app.stem),
                        bundle_id=str(raw.get('CFBundleIdentifier') or ''),
                        version=str(raw.get('CFBundleShortVersionString') or ''))
            if info['bundle_id']:
                app_ids[info['bundle_id']].append(app)
        except (OSError, ValueError, TypeError, AttributeError, OverflowError, ExpatError, plistlib.InvalidFileException) as exc:
            issue(plist, f'应用元数据无法解析：{exc}')

    for p, row in files.items():
        if row['kind'] == 'symlink':
            continue
        agent = _agent(p)
        if p.suffix == '.jsonl' and ('.codex/sessions/' in p.as_posix() or '.claude/projects/' in p.as_posix()):
            try:
                for cwd in _read(p, row, lambda stream: _cwd_records(stream, agent)):
                    proj = Path(cwd)
                    if proj in directories:
                        project_agents.setdefault(proj, set()).add(agent)
                    else:
                        if proj.is_absolute() and proj != Path('/') and '..' not in proj.parts:
                            suggested_projects.add(str(proj))
                        issue(p, f'会话工程未覆盖：{cwd}')
            except (OSError, ValueError, RecursionError) as exc:
                issue(p, f'会话记录未完整解析：{exc}')
    for repo in repositories:
        project_agents.setdefault(repo, set())
        marker = repo / '.git'
        try:
            if not stat.S_ISDIR(marker.lstat().st_mode):
                raise ValueError('外部 Git 工作树索引未验证')
            if os.path.realpath(marker / 'index') != str(marker / 'index'):
                raise ValueError('Git 索引路径包含链接')
            res = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(repo), 'ls-files', '-z', '--cached'],
                                 env={'PATH': os.defpath, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_CONFIG_NOSYSTEM': '1',
                                      'GIT_CONFIG_GLOBAL': os.devnull}, capture_output=True, check=True, timeout=20)
            for rel in res.stdout.split(b'\0'):
                tracked = repo / os.fsdecode(rel)
                if tracked in files:
                    protect(tracked, 'Git 已跟踪')
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            issue(repo, f'Git 工作区读取不完整：{exc}')
            for p in files:
                if repo in p.parents:
                    defer(p, 'Git 索引无法核对')
    # VS Code keeps its current session and nine previous sessions:
    # https://github.com/microsoft/vscode/blob/1.95.3/src/vs/code/electron-utility/sharedProcess/contrib/logsDataCleaner.ts#L27-L40
    # Scope is narrower here (main.log only, idle editor, aged regular file).
    # The hosted editor regression exercises this rule against the signed release.
    session_names = defaultdict(list)
    for directory in directories:
        if (directory.parent.parts[-3:] == ('Application Support', 'Code', 'logs') and
                re.fullmatch(r'[0-9]{8}T[0-9]{6}', directory.name)):
            session_names[directory.parent].append(directory)
    expired_sessions = {p for paths in session_names.values() for p in sorted(paths)[:-10]}
    editor_state = {}
    for p, row in files.items():
        # Nearest declared project controls path ownership; references can
        # subsequently add projects outside the containing directory.
        proj = next((parent for parent in p.parents if parent in project_agents), None)
        if proj:
            row.update(projects=[str(proj)], agents=sorted(set(row['agents']) | project_agents[proj]), location='项目内')
        if row.get('shared_container'):
            # A group identifier alone cannot establish any exclusive owner.
            continue
        app = next((parent for parent in p.parents if parent in apps), None)
        if app:
            protect(p, '应用本体')
        else:
            for lib in (ancestor for ancestor in p.parents if ancestor.name == 'Library'):
                parts = p.relative_to(lib).parts
                preference = len(parts) == 2 and parts[0] == 'Preferences' and p.suffix == '.plist'
                if not preference and (len(parts) < 3 or parts[0] not in {'Logs', 'Caches', 'Containers', 'Application Support'}):
                    continue
                valid_install_dirs = {lib.parent / 'Applications'}
                if lib == Path.home() / 'Library':
                    valid_install_dirs.add(Path('/Applications'))
                is_vscode = parts[:2] == ('Application Support', 'Code')
                bundle = p.stem if preference else parts[1]
                if is_vscode:
                    bundle = 'com.microsoft.VSCode'
                installs = [a for a in app_ids.get(bundle, []) if a.parent in valid_install_dirs]
                if len(installs) > 1:
                    row['shared_owners'] = sorted(str(a) for a in installs)
                    protect(p, '多个安装副本共享数据')
                    break
                if len(installs) != 1:
                    continue
                app = installs[0]
                if (parts == ('Logs', 'com.lumen.fixture', 'expired-1.log') and apps[app]['version'] == '1.0'
                        and row['decision'] == 'review' and time.time_ns() - row['mtime_ns'] >= 30 * 86400 * 10**9):
                    row.update(decision='safe', rule_id=integrations.FIXTURE_LOG, reason='测试应用 1.0 的废弃轮转日志已超过 30 天')
                if (is_vscode and apps[app]['version'] == '1.95.3' and p.parent in expired_sessions
                        and len(parts) == 5 and parts[2] == 'logs' and p.name == 'main.log'
                        and row['decision'] == 'review' and time.time_ns() - row['mtime_ns'] >= 30 * 86400 * 10**9):
                    if app not in editor_state:
                        editor_state[app] = _code_stopped(app)
                    stopped, status_reason = editor_state[app]
                    if stopped:
                        row.update(decision='safe', rule_id=integrations.VSCODE_LOG, reason='VS Code 1.95.3 已退出，旧会话在十次保留范围外，仅移除主进程诊断日志')
                    else:
                        defer(p, status_reason)
                break
        if app:
            row.update(owner=apps[app]['name'], app_path=str(app), bundle_id=apps[app]['bundle_id'],
                       app_version=apps[app]['version'])

    for p, row in files.items():
        if p.suffix.lower() in {'.prproj', '.drp', '.fcpbundle', '.aep'}:
            issue(p, '此工程格式的素材引用尚未解析')
        parser = None
        if p.name == 'manifest.json' and p.parent.name == '.vite':
            parser = integrations.vite_outputs
        elif p.suffix.lower() == '.fcpxml':
            parser = integrations.fcpxml_sources
        if parser and row['kind'] != 'symlink':
            try:
                for target, label in parser(p, _read(p, row, _small)):
                    static_refs.add((str(p), str(target), str(label)))
                    if target not in files:
                        issue(p, f'{label} 所指文件未覆盖：{target}')
                        continue
                    asset = files[target]
                    if parser == integrations.vite_outputs:
                        asset['kind'] = 'build'
                    asset['refs'] = sorted(set(asset['refs']) | {f'{p}#{label}'})
                    asset['projects'] = sorted(set(asset['projects']) | set(row['projects']))
                    asset['agents'] = sorted(set(asset['agents']) | set(row['agents']))
                    protect(target, 'Vite 构建清单或 Final Cut Pro 工程存在引用')
            except (ValueError, RecursionError, ParseError, OSError) as exc:
                issue(p, f'清单或工程引用未完整解析：{exc}')
        if row['kind'] != 'html':
            continue
        try:
            html = _HTML()
            html.feed(_read(p, row, _small).decode('utf-8-sig'))
            html.close()
            if html.partial:
                issue(p, '脚本、样式或响应式资源引用未解析')
            base = urljoin(p.as_uri(), html.base or '')
            for raw, line in html.links:
                if not raw or raw.startswith('#'):
                    continue
                if any(t in raw for t in ('${', '{{', '<%')):
                    issue(p, f'第 {line} 行存在动态引用')
                    continue
                url = urlsplit(urljoin(base, raw))
                if url.scheme != 'file' or url.netloc not in ('','localhost'):
                    continue
                target = Path(os.path.normpath(unquote(url.path)))
                static_refs.add((str(p), str(target), str(line)))
                if target not in files:
                    if target not in directories:
                        issue(p, f'第 {line} 行本地资源未覆盖：{target}')
                    continue
                asset = files[target]
                asset['refs'] = sorted(set(asset['refs']) | {f'{p}:{line}'})
                asset['projects'] = sorted(set(asset['projects']) | set(row['projects']))
                asset['agents'] = sorted(set(asset['agents']) | set(row['agents']))
                protect(target, '本地 HTML 工程存在静态引用')
        except (OSError, ValueError, AssertionError, RecursionError) as exc:
            issue(p, f'HTML 引用未完整解析：{exc}')

    # A link in the inventory is a known consumer of its target. Never
    # follow it to discover new files. Model snapshot links commonly share
    # a single blob across several revisions.
    linked_directories = set()
    for link, target in link_targets.items():
        if target in files:
            protect(target, '有已扫描的符号链接指向此文件')
            files[target]['refs'] = sorted(set(files[target]['refs']) | {str(link)})
            if 'snapshots' in link.parts and files[target]['kind'] == 'model':
                index = link.parts.index('snapshots') + 1
                if index < len(link.parts) - 1:
                    versions = files[target].setdefault('model_versions', [])
                    files[target]['model_versions'] = sorted(set(versions) | {link.parts[index]})
        elif target in directories:
            linked_directories.add(target)
    for p, row in files.items():
        if linked_directories and any(parent in linked_directories for parent in p.parents):
            protect(p, '已扫描的符号链接指向上级目录')
        if row['decision'] == 'safe' and not _idle(p):
            defer(p, '文件正在使用或无法确认空闲状态')

    hub_refs = defaultdict(set)
    for p, row in files.items():
        ref = integrations.hub_ref(p)
        if ref and row['kind'] != 'symlink':
            protect(p, '模型仓库修订引用')
            try:
                revision = _read(p, row, _small).decode('ascii').strip()
                if not re.fullmatch(r'[0-9a-f]{40}', revision):
                    raise ValueError('未识别的修订 ID')
                repo, name = ref
                if repo / 'snapshots' / revision not in directories:
                    issue(p, f'模型修订目录未覆盖：{revision}')
                hub_refs[repo, revision].add(name)
            except (ValueError, OSError) as exc:
                issue(p, f'模型引用未解析：{exc}')
    for p, row in files.items():
        repo = next((a for a in p.parents if a.name.startswith('models--')), None)
        if repo and row['kind'] == 'model':
            versions = row.get('model_versions', []) + [row.get('model_version', '')]
            row['model_refs'] = sorted({name for v in versions for name in hub_refs.get((repo, v), [])})

    scope = [sorted(str(p) for p in root_set), sorted(str(p) for p in exclusions), bool(deep)]
    previous = hash_cache.get('digests', {}) if hash_cache and hash_cache.get('scope') == scope else {}
    next_digests = {}
    hash_hits = hash_reads = 0
    if deep:
        sizes = defaultdict(list)
        for p, row in files.items():
            if row['kind'] != 'symlink' and row['size']:
                sizes[row['size']].append(p)
        for paths in sizes.values():
            if len({(files[p]['dev'], files[p]['ino']) for p in paths}) < 2:
                continue
            groups = defaultdict(list)
            by_inode = {}
            for p in paths:
                row = files[p]
                ident = row['dev'], row['ino']
                try:
                    if ident not in by_inode:
                        signature = [row[k] for k in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')]
                        old = previous.get(str(p)) if isinstance(previous, dict) else None
                        if (isinstance(old, list) and len(old) == 2 and old[0] == signature and
                                isinstance(old[1], str) and re.fullmatch('[0-9a-f]{64}', old[1])):
                            by_inode[ident] = _read(p, row, lambda stream: old[1])
                            hash_hits += 1
                        else:
                            by_inode[ident] = _read(p, row, _digest)
                            hash_reads += 1
                        if hash_cache is not None:
                            next_digests[str(p)] = [signature, by_inode[ident]]
                    groups[by_inode[ident]].append(p)
                except OSError as exc:
                    defer(p, f'内容无法核对：{exc}')
            for digest, group in groups.items():
                if len({(files[p]['dev'], files[p]['ino']) for p in group}) > 1:
                    duplicates[digest] = sorted(str(p) for p in group)
                    for p in group:
                        files[p]['duplicate_group'] = digest
    if hash_cache is not None:
        hash_cache.clear()
        hash_cache.update(scope=scope, digests=next_digests)
    references = [dict(source=source, target=target, location=location)
                  for source, target, location in sorted(static_refs)]
    return dict(suggested_projects=sorted(suggested_projects), hash_reads=hash_reads, hash_hits=hash_hits, files=[files[p] for p in sorted(files)],
                issues=sorted(issues), static_refs=references,
                projects={str(p): sorted(a) for p,a in sorted(project_agents.items())},
                apps=[apps[p] for p in sorted(apps)], duplicates=duplicates,
                volumes=list(volumes.values()), space=_space(files))
