"""Supported standalone app removal and current-user Trash operations for macOS."""
import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import uuid

from . import actions as a


SIGNING_TEAMS = {
    'com.microsoft.VSCode': 'UBF8T346G9',
    'org.videolan.vlc': '75GAHG3SZQ',
    'com.colliderli.iina': '67CQ77V27R',
}
SUPPORTED_BUNDLES = frozenset(SIGNING_TEAMS) | {'com.lumen.fixture'}
_DIR = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY


def _identity(s):
    return [s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_size, s.st_mtime_ns]


def _manifest(path):
    """Read directory entries relative to pinned handles. Never traverse a link."""
    result = {}
    def visit(parent, name, key, dev):
        s = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if s.st_dev != dev or s.st_uid != os.getuid():
            raise ValueError('跨卷内容或其他用户拥有的文件需要单独处理')
        if len(result) >= 100000 or key.count('/') > 200:
            raise ValueError('清单超过本地核对上限')
        result[key] = _identity(s)
        if stat.S_ISDIR(s.st_mode):
            child = os.open(name, _DIR, dir_fd=parent)
            try:
                if _identity(os.fstat(child)) != result[key]:
                    raise ValueError('目录已变化')
                for entry in sorted(os.listdir(child)):
                    visit(child, entry, key + '/' + entry if key else entry, dev)
                if _identity(os.fstat(child)) != result[key]:
                    raise ValueError('目录在盘点过程中变化')
            finally:
                os.close(child)
        elif stat.S_ISREG(s.st_mode):
            if s.st_nlink != 1:
                raise ValueError('遇到共享硬链接')
        elif not stat.S_ISLNK(s.st_mode):
            raise ValueError('包含特殊文件')
    with a._directory(path.parent) as fd:
        visit(fd, path.name, '', os.fstat(fd).st_dev)
    return result


def _digest(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _summary(path, entries):
    dev, ino, mode, _, _, mtime = entries['']
    return dict(path=str(path), dev=dev, ino=ino, mtime_ns=mtime, manifest_digest=_digest(entries),
                size=sum(row[4] for row in entries.values() if stat.S_ISREG(row[2])),
                kind='directory' if stat.S_ISDIR(mode) else 'file')


def _app(path, fixture_roots):
    path = a._path(path)
    if path.suffix != '.app' or path.parent.name != 'Applications' or any(
            s.casefold().endswith('.app') for s in path.parent.parts):
        raise ValueError('仅支持 Applications 内独立安装的应用')
    system = ('/System', '/Library', '/usr', '/private/var/db', '/bin', '/sbin')
    if any(a._inside(Path(str(path).casefold()), Path(p.casefold())) for p in system):
        raise ValueError('系统安装位置受保护')
    entries = _manifest(path)
    key = 'Contents/Info.plist'
    info = entries.get(key)
    if not info or not stat.S_ISREG(info[2]) or info[4] > 2_000_000:
        raise ValueError('缺少可验证的安装信息')
    with a._directory(path / 'Contents') as fd:
        raw = os.open('Info.plist', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(raw, 'rb') as stream:
            if _identity(os.fstat(raw)) != info:
                raise ValueError('安装信息发生变化')
            values = plistlib.loads(stream.read(2_000_001))
    bundle = values.get('CFBundleIdentifier')
    if bundle not in SUPPORTED_BUNDLES or values.get('CFBundlePackageType') != 'APPL':
        raise ValueError('安装包无已验证的独立应用卸载规则')
    if bundle == 'com.lumen.fixture':
        if path.parent.parent not in {a._path(root) for root in fixture_roots}:
            raise ValueError('演练应用未在此工作区登记')
    else:
        team = SIGNING_TEAMS[bundle]
        requirement = f'=anchor apple generic and identifier "{bundle}" and certificate leaf[subject.OU] = "{team}"'
        try:
            signature = subprocess.run(['/usr/bin/codesign', '--verify', '--strict', '-R', requirement, str(path)],
                                       capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('无法验证应用签名') from exc
        if signature.returncode:
            raise ValueError('安装包签名、发行团队或签名标识无法核实')
    version = values.get('CFBundleShortVersionString')
    if not isinstance(version, str) or not version:
        raise ValueError('应用版本未知')
    return dict(_summary(path, entries), bundle_id=bundle, version=version), entries


def app_snapshot(path, fixture_roots=()):
    return _app(path, fixture_roots)[0]


def _idle(path, tree, entries=None):
    if not tree:
        return a._busy(path)
    if entries:
        images = [path / key for key, row in entries.items()
                  if stat.S_ISREG(row[2]) and Path(key).suffix.casefold() in a.IMAGE_SUFFIXES]
        if images:
            a.check_image_idle(images[0], images[1:])
    exe = shutil.which('lsof', path='/usr/sbin:/usr/bin:/sbin:/bin')
    if not exe:
        raise ValueError('无法检查应用或目录是否在使用')
    proc = subprocess.run([exe, '-nP', '-t', '+D', str(path)], capture_output=True, timeout=20)
    if proc.stdout or proc.returncode != 1 or proc.stderr.strip():
        raise ValueError('应用或目录正在使用，或无法核实占用情况')


def _scope(path, roots):
    for root in sorted((a._path(r) for r in roots), key=lambda p: -len(p.parts)):
        if root in path.parents and root != Path('/'):
            return root
    raise ValueError('原始安装目录未获授权')


def _table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS app_op (
        id TEXT PRIMARY KEY, created TEXT, path TEXT, stored TEXT, snapshot TEXT,
        manifest TEXT, status TEXT, reason TEXT
    )""")


def _status(db, uid, value, reason=''):
    db.execute('UPDATE app_op SET status=?,reason=? WHERE id=?', (value, reason, uid))


def _where(path, snapshot):
    try:
        with a._directory(path.parent) as fd:
            s = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        return (s.st_dev, s.st_ino) == (snapshot['dev'], snapshot['ino'])
    except FileNotFoundError:
        return False


def _row(db, uid):
    _table(db)
    r = db.execute('SELECT * FROM app_op WHERE id=?', (uid,)).fetchone()
    if not r:
        raise ValueError('未知卸载批次')
    if r['status'] in ('moving', 'restoring'):
        snap = json.loads(r['snapshot'])
        p, stored = Path(r['path']), Path(r['stored'])
        if _where(stored, snap):
            _status(db, uid, 'quarantined')
        elif _where(Path(a._restored_to(db, uid, str(p))), snap):
            _status(db, uid, 'restored' if r['status'] == 'restoring' else 'skipped')
        else:
            return r
        r = db.execute('SELECT * FROM app_op WHERE id=?', (uid,)).fetchone()
    return r


def _result(db, r):
    result = dict(id=r['id'], batch_id=r['id'], action='uninstall', created_at=r['created'],
                items=[dict(path=r['path'], size=json.loads(r['snapshot'])['size'],
                            status=r['status'], reason=r['reason'])])
    if r['status'] == 'restored':
        result['items'][0]['restored_to'] = a._restored_to(db, r['id'], r['path'])
    return result


def history(state_dir):
    with a._journal(state_dir) as (db, _):
        _table(db)
        return [_result(db, _row(db, r[0])) for r in db.execute('SELECT id FROM app_op ORDER BY rowid DESC')]


def uninstall(state_dir, roots, record, fixture_roots=()):
    p = a._path(record['path'])
    root = _scope(p, roots)
    with a._journal(state_dir) as (db, state):
        _table(db)
        snap, entries = _app(p, fixture_roots)
        if any(record.get(k) != snap[k] for k in ('dev','ino','mtime_ns','bundle_id','version','manifest_digest')):
            raise ValueError('安装快照已过期，请重新扫描')
        if a._inside(p, state.parent) or a._inside(state.parent, p):
            raise ValueError('存储目录受保护')
        _idle(p, True, entries)
        dest = a._vault(state, root, snap['dev'])
        a._private(dest)
        uid = 'app-' + uuid.uuid4().hex
        stored = dest / uid
        with a._directory(p.parent) as src, a._directory(dest) as target:
            if os.fstat(target).st_dev != snap['dev'] or _manifest(p) != entries:
                raise ValueError('应用已变化或同卷存储不可用')
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            db.execute('INSERT INTO app_op VALUES (?,?,?,?,?,?,?,?)',
                       (uid, now, str(p), str(stored), json.dumps(snap), json.dumps(entries), 'moving', ''))
            a._rename(src, p.name, target, uid)
            if _manifest(stored) != entries:
                a._rename(target, uid, src, p.name)
                _status(db, uid, 'skipped', '应用移动期间发生变化')
            else:
                _status(db, uid, 'quarantined')
        return _result(db, _row(db, uid))


def _remove_recorded(path, expected, partial=False):
    actual = _manifest(path)
    def matches(key, entry):
        saved = expected.get(key)
        if not saved:
            return False
        if partial and stat.S_ISDIR(saved[2]):
            return entry[:4] == saved[:4]
        return entry == saved
    if any(not matches(k, v) for k, v in actual.items()) or (not partial and actual.keys() != expected.keys()):
        raise ValueError('实际内容与确认清单不同，已停止清空')
    # Remove only verified entries. rmdir fails if any unlisted child appears.
    for key in sorted(actual, key=lambda k: len(Path(k).parts), reverse=True):
        child = path / key if key else path
        entry = actual[key]
        with a._directory(child.parent) as parent:
            s = os.stat(child.name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(entry[2]):
                if _identity(s)[:4] != entry[:4]:
                    raise ValueError('目录身份变化')
                os.rmdir(child.name, dir_fd=parent)
            else:
                if _identity(s) != entry or (stat.S_ISREG(s.st_mode) and s.st_nlink != 1):
                    raise ValueError('文件身份变化')
                os.unlink(child.name, dir_fd=parent)


def _followup(state_dir, roots, uid, operation, beside=False):
    with a._journal(state_dir) as (db, _):
        r = _row(db, uid)
        if r['status'] != 'quarantined' and not (operation == 'purge' and r['status'] == 'purging'):
            return _result(db, r)
        path, stored = Path(r['path']), Path(r['stored'])
        _scope(path, roots)
        entries = json.loads(r['manifest'])
        try:
            with a._directory(stored.parent) as target:
                if r['status'] == 'purging' and not os.path.lexists(stored):
                    _status(db, uid, 'purged')
                else:
                    _idle(stored, True, _manifest(stored) if r['status'] == 'purging' else entries)
                    if operation == 'restore':
                        if _manifest(stored) != entries:
                            raise ValueError('暂存应用已变化')
                        with a._directory(path.parent) as src:
                            path = a._restore_target(db, uid, src, path, beside)
                            _status(db, uid, 'restoring')
                            a._rename(target, stored.name, src, path.name)
                        _status(db, uid, 'restored')
                    else:
                        partial = r['status'] == 'purging'
                        if not partial and _manifest(stored) != entries:
                            raise ValueError('暂存应用已变化')
                        _status(db, uid, 'purging', '清空中，可重试')
                        _remove_recorded(stored, entries, partial)
                        _status(db, uid, 'purged')
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            row = _row(db, uid)
            _status(db, uid, row['status'], str(exc))
        return _result(db, _row(db, uid))


def restore(state_dir, roots, batch_id, beside=False):
    return _followup(state_dir, roots, batch_id, 'restore', beside)


def purge(state_dir, roots, batch_id):
    return _followup(state_dir, roots, batch_id, 'purge')


def _trash_root(root, fixture_trash):
    root = a._path(root)
    fixtures = {a._path(v) for v in fixture_trash}
    if root != a._path(Path.home() / '.Trash') and root not in fixtures:
        volume = root.parent.parent
        if not (root.name == str(os.getuid()) and root.parent.name == '.Trashes' and
                volume.parent == Path('/Volumes')):
            raise ValueError('请选择当前用户的卷废纸篓')
        if not os.path.ismount(volume):
            raise FileNotFoundError(errno.ENODEV, '外置卷已离线', str(volume))
    a._private(root, create=False)
    return root


def trash_inventory(roots, fixture_trash=()):
    result = []
    for value in roots:
        row = dict(root=str(value), items=[], status='ready')
        result.append(row)
        try:
            root = _trash_root(value, fixture_trash)
            with a._directory(root) as fd:
                for name in sorted(os.listdir(fd)):
                    p = root / name
                    try:
                        row['items'].append(_summary(p, _manifest(p)))
                    except (ValueError, OSError) as exc:
                        row['items'].append(dict(path=str(p), size=0, reason=str(exc)))
        except FileNotFoundError:
            row.update(status='offline', error='卷或已授权废纸篓离线')
        except (OSError, ValueError) as exc:
            row.update(status='unavailable', error=str(exc))
    return result


def empty_trash(roots, records, fixture_trash=()):
    allowed = {a._path(r) for r in roots}
    results = []
    for record in records:
        item = dict(path=str(record.get('path', '')), status='skipped', reason='')
        results.append(item)
        try:
            p = a._path(item['path'])
            if p.parent not in allowed:
                raise ValueError('所选项目不在已授权废纸篓的顶层')
            root = _trash_root(p.parent, fixture_trash)
            entries = _manifest(p)
            snap = _summary(p, entries)
            if any(record.get(k) != snap[k] for k in ('dev','ino','mtime_ns','manifest_digest')):
                raise ValueError('废纸篓项目已变化，请重新预览')
            if stat.S_ISLNK(entries[''][2]):
                raise ValueError('顶层链接受保护')
            _idle(p, snap['kind'] == 'directory', entries)
            # Move to an exclusive private directory before deleting descendants.
            # Interrupted entries stay in Trash and can be inventoried again.
            with a._directory(root) as fd:
                stage = '.Lumen-' + uuid.uuid4().hex
                os.mkdir(stage, 0o700, dir_fd=fd)
                try:
                    with a._directory(root / stage) as dst:
                        a._rename(fd, p.name, dst, p.name)
                        try:
                            _remove_recorded(root / stage / p.name, entries)
                        except (OSError, ValueError):
                            a._rename(dst, p.name, fd, p.name)
                            raise
                finally:
                    os.rmdir(stage, dir_fd=fd)
            item.update(status='purged', size=snap['size'])
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            item['reason'] = str(exc)
    return {'items': results}


def _registration_absent(bundle_id):
    """Ask LaunchServices about all registered installations, including other volumes."""
    script = """ObjC.import('AppKit');
    function run(argv) {
        const urls = $.NSWorkspace.sharedWorkspace.URLsForApplicationsWithBundleIdentifier(argv[0]);
        return Number(urls.count) === 0 ? 'absent' : 'present';
    }"""
    try:
        result = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script, bundle_id],
                                capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and result.stdout.strip() == 'absent'
    except (OSError, subprocess.SubprocessError):
        return False


def residuals(state_dir, roots, report, fixture_roots=()):
    """Review-only files from verified namespaces of installations removed here."""
    installed = {app.get('bundle_id') for app in report.get('apps', [])}
    roots = [a._path(r) for r in roots]
    labs = {a._path(p) for p in fixture_roots}
    candidates = {}
    checked = {}
    with a._journal(state_dir) as (db, state):
        _table(db)
        for row in db.execute("SELECT * FROM app_op WHERE status IN ('quarantined','purged')"):
            app = Path(row['path'])
            snap = json.loads(row['snapshot'])
            bundle = snap['bundle_id']
            if bundle in installed or bundle not in SUPPORTED_BUNDLES:
                continue
            try:
                _scope(app, roots)
                with a._directory(app.parent) as parent:
                    if os.fstat(parent).st_dev != snap['dev']:
                        continue
                    try:
                        os.stat(app.name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        continue
            except (OSError, ValueError):
                continue
            if app.parent.parent in labs and bundle == 'com.lumen.fixture':
                library = app.parent.parent / 'Library'
            else:
                if bundle not in checked:
                    checked[bundle] = _registration_absent(bundle)
                if not checked[bundle]:
                    continue
                library = Path.home() / 'Library'
            for category in ('Caches', 'Logs'):
                candidates[library / category / bundle] = (app, bundle)
    result = []
    for record in report.get('files', []):
        if (record.get('decision') not in ('safe','review') or record.get('refs') or
                record.get('nlink') != 1 or record.get('kind') == 'symlink' or record.get('projects')):
            continue
        path = a._path(record['path'])
        try:
            a._eligible(path, roots, state.parent)
        except ValueError:
            continue
        match = next((candidates[parent] for parent in path.parents if parent in candidates), None)
        if match:
            app, bundle = match
            result.append(dict(path=str(path), size=record['size'], bundle_id=bundle, app_path=str(app),
                               decision='review', reason='已移出的应用在该用户资源库下的专属缓存或日志，需逐项审阅'))
    return result
