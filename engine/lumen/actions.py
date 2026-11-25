"""Local, same-volume file actions. Only enrolled regular files are eligible.

macOS Trash uses the current user's Finder-visible trash on the source volume.
Tests inject _trash_directory and never use the user's actual trash. An lsof
snapshot and advisory file lock detect observable use; other processes can
start using a file after either check. Local processes running as the same
user can modify their own files and the journal.
"""
import contextlib
import ctypes
import datetime
import errno
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid


def _path(value):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts:
        raise ValueError('需要无上级跳转的绝对路径')
    if sys.platform == 'darwin' and len(p.parts) > 1 and p.parts[1] in ('var', 'tmp', 'etc'):
        p = Path('/private').joinpath(*p.parts[1:])
    return p


@contextlib.contextmanager
def _directory(path, create=False):
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
    fd = os.open('/', flags)
    try:
        for part in _path(path).parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _private(path, create=True):
    with _directory(path, create) as fd:
        s = os.fstat(fd)
        if s.st_uid != os.getuid() or s.st_mode & 0o077:
            raise ValueError('存储目录必须为当前用户独占')


def _inside(path, root):
    return path == root or root in path.parents


def _eligible(p, roots, state):
    names = {v.casefold() for v in p.parts}
    protected = {'.git', '.ssh', '.gnupg', '.aws', '.azure', '.kube', '.continue', 'keychains',
                 'credentials', 'cookies', 'login data', 'local state', 'sessions',
                 'bookmarks', 'web data', 'places.sqlite', 'logins.json', 'key4.db',
                 'secrets', 'auth.json', '.netrc', '.env', 'wallet.dat',
                 '.trash', '.trashes', '.lumen-vault-' + str(os.getuid())}
    restricted = ('/System', '/Library', '/Applications', '/bin', '/sbin',
                  '/usr', '/dev', '/private/etc', '/private/var/db', '/private/var/root')
    if (_inside(Path(str(p).casefold()), Path(str(state).casefold())) or names & protected or
            any(v.startswith('.env.') or v.endswith(('.pem', '.key', '.p12', '.mobileprovision')) for v in names) or
            any(v.endswith(('.app', '.framework', '.bundle', '.keychain-db')) for v in names) or
            any(_inside(Path(str(p).casefold()), Path(v.casefold())) for v in restricted)):
        raise ValueError('受保护的位置')
    for root in roots:
        if root != Path('/') and root in p.parents:
            return root
    raise ValueError('原位置不在当前授权范围')


def _matches(s, r):
    return (stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and s.st_nlink == 1 and
            (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_size) ==
            (r['dev'], r['ino'], r['mtime_ns'], r['size']))


def _check(fd, name, rec):
    if not _matches(os.stat(name, dir_fd=fd, follow_symlinks=False), rec):
        raise ValueError('文件身份、内容或链接数已变化')


IMAGE_SUFFIXES = frozenset(('.dmg', '.sparseimage', '.iso', '.cdr', '.img'))


def check_image_idle(path, others=()):
    """A mounted disk image can remain in use even without a visible file opener."""
    paths = [p for p in (path, *others) if p.suffix.casefold() in IMAGE_SUFFIXES]
    if sys.platform != 'darwin' or not paths:
        return
    try:
        proc = subprocess.run(['/usr/bin/hdiutil', 'info', '-plist'], capture_output=True, timeout=10, check=True)
        images = plistlib.loads(proc.stdout)['images']
        if not isinstance(images, list):
            raise KeyError('images')
    except (OSError, KeyError, ValueError, TypeError, subprocess.SubprocessError, plistlib.InvalidFileException):
        raise ValueError('无法核对磁盘映像的挂载状态')
    identities = {(s.st_dev, s.st_ino) for s in (p.stat() for p in paths)}
    for image in images:
        mounted = image.get('image-path') if isinstance(image, dict) else None
        if not isinstance(mounted, str):
            raise ValueError('存在无法核对来源的挂载映像')
        if Path(mounted) in paths:
            raise ValueError('磁盘映像仍在挂载')
        try:
            backing = os.stat(mounted)
        except FileNotFoundError:
            continue
        if (backing.st_dev, backing.st_ino) in identities:
            raise ValueError('磁盘映像仍在挂载')


def _busy(path):
    check_image_idle(path)
    exe = shutil.which('lsof', path='/usr/sbin:/usr/bin:/sbin:/bin')
    if not exe:
        raise ValueError('无法检查文件占用')
    proc = subprocess.run([exe, '-nP', '-t', '--', str(path)], capture_output=True, timeout=8)
    if proc.stdout:
        raise ValueError('文件正在使用')
    if proc.returncode != 1 or proc.stderr.strip():
        raise ValueError('无法确认文件空闲')


@contextlib.contextmanager
def _locked_file(parent, path, rec):
    _check(parent, path.name, rec)
    _busy(path)
    fd = os.open(path.name, os.O_NOFOLLOW | os.O_RDONLY | os.O_NONBLOCK, dir_fd=parent)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not _matches(os.fstat(fd), rec):
            raise ValueError('文件在执行前已变化')
        _check(parent, path.name, rec)
        yield
    finally:
        os.close(fd)


def _rename(src_fd, src, dst_fd, dst):
    """Atomic rename with exclusive destination on Darwin and Linux."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        call, flag = libc.renameatx_np, 4
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        call, flag = libc.renameat2, 1
    else:
        raise ValueError('该平台无法保证无覆盖移动')
    if call(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), flag):
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise ValueError('目标已有同名文件，两个文件均已保留')
        raise OSError(error, os.strerror(error))
    for directory in (src_fd, dst_fd):
        os.fsync(directory)


def _trash_directory(path):
    if sys.platform != 'darwin':
        raise ValueError('本机系统废纸篓暂不支持')
    if path.stat().st_dev == Path.home().stat().st_dev:
        return Path.home() / '.Trash'
    mount = path.parent
    while mount.parent != mount and mount.parent.stat().st_dev == mount.stat().st_dev:
        mount = mount.parent
    trash = mount / '.Trashes'
    with _directory(trash):
        return trash / str(os.getuid())


def _vault(state, root, dev):
    if state.stat().st_dev == dev:
        return state / 'vault'
    return root / ('.lumen-vault-' + str(os.getuid()))


@contextlib.contextmanager
def _journal(state_dir):
    state = _path(state_dir) / 'actions'
    _private(state)
    with _directory(state) as fd:
        # A concurrent exclusive create can race openat(O_CREAT|O_NOFOLLOW)
        # on APFS. Open the established file separately.
        try:
            lock = os.open('lock', os.O_NOFOLLOW | os.O_RDWR | os.O_CREAT | os.O_EXCL,
                           0o600, dir_fd=fd)
        except FileExistsError:
            lock = os.open('lock', os.O_NOFOLLOW | os.O_RDWR, dir_fd=fd)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            db = sqlite3.connect(str(state / 'journal.sqlite3'), isolation_level=None)
            try:
                db.row_factory = sqlite3.Row
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS batch (
                        id TEXT PRIMARY KEY, action TEXT NOT NULL, created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS item (
                        id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, path TEXT NOT NULL,
                        rec TEXT NOT NULL, stored TEXT, status TEXT NOT NULL, reason TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS restore_path (id TEXT PRIMARY KEY, path TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS staging (id TEXT PRIMARY KEY, target TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS task_closeout (batch_id TEXT PRIMARY KEY, closeout_id TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS item_batch ON item(batch_id);
                """)
                _recover(db)
                yield db, state
            finally:
                db.close()
        finally:
            os.close(lock)


def _set(db, item, status, reason=''):
    db.execute('UPDATE item SET status=?,reason=? WHERE id=?', (status, reason, item))


def _present(path, rec):
    with _directory(path.parent) as fd:
        try:
            return _matches(os.stat(path.name, dir_fd=fd, follow_symlinks=False), rec)
        except FileNotFoundError:
            return False



def _restore_target(db, uid, parent, path, beside):
    candidate = path
    if beside:
        for number in range(101):
            try:
                os.stat(candidate.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                break
            tail = ' (Lumen restored)' if number == 0 else f' (Lumen restored {number + 1})'
            candidate = path.with_name(path.stem + tail + path.suffix)
        else:
            raise ValueError('未找到可用的同目录恢复名称')
    db.execute('INSERT OR REPLACE INTO restore_path VALUES (?,?)', (uid, str(candidate)))
    return candidate


def _restored_to(db, uid, original):
    row = db.execute('SELECT path FROM restore_path WHERE id=?', (uid,)).fetchone()
    return row[0] if row else original


def _recover(db, uid=None):
    query = ("SELECT item.*,batch.action FROM item JOIN batch ON batch.id=item.batch_id "
                      "WHERE status IN ('pending','moving','restoring','purging','cleaning','securing')")
    rows = db.execute(query + (" AND item.id=?" if uid else ""), (uid,) if uid else ()).fetchall()
    for r in rows:
        phase = r['status']
        if phase == 'pending':
            _set(db, r['id'], 'skipped', '操作中断，尚未开始移动')
            continue
        rec = json.loads(r['rec'])
        staging = db.execute('SELECT target FROM staging WHERE id=?', (r['id'],)).fetchone()
        secured = staging and r['stored'] == staging[0]
        held = 'trashed' if r['action'] == 'trash' and not secured else 'quarantined'
        try:
            stored = Path(r['stored'])
            if phase == 'securing':
                staged = db.execute('SELECT target FROM staging WHERE id=?', (r['id'],)).fetchone()
                if staged and _present(Path(staged[0]), rec):
                    db.execute("UPDATE item SET stored=?,status='quarantined',reason=? WHERE id=?",
                               (staged[0], '清空中断，文件仍在隔离区', r['id']))
                    continue
            if _present(stored, rec):
                _set(db, r['id'], held, '已核对暂存文件')
            elif phase == 'restoring' and _present(Path(_restored_to(db, r['id'], r['path'])), rec):
                _set(db, r['id'], 'restored')
            elif phase in ('purging', 'cleaning') and not os.path.lexists(stored):
                _set(db, r['id'], 'purged' if phase == 'purging' else 'cleaned')
            else:
                _set(db, r['id'], 'skipped', '操作中断，未确认已暂存文件')
        except (OSError, ValueError):
            # Keep intent for retry when a disconnected volume is available.
            db.execute('UPDATE item SET reason=? WHERE id=?', ('暂时无法核对存储位置', r['id']))


def _result(db, batch):
    row = db.execute('SELECT * FROM batch WHERE id=?', (batch,)).fetchone()
    if row is None:
        raise ValueError('找不到此批次')
    items = []
    linked = db.execute('SELECT closeout_id FROM task_closeout WHERE batch_id=?', (batch,)).fetchone()
    for it in db.execute('SELECT * FROM item WHERE batch_id=? ORDER BY rowid', (batch,)):
        record = json.loads(it['rec'])
        items.append(dict(path=it['path'], status=it['status'], reason=it['reason'],
                          size=record.get('size', 0)))
        if linked and 'dev' in record and 'ino' in record:
            items[-1]['identity'] = [record['dev'], record['ino']]
        if it['status'] == 'restored':
            items[-1]['restored_to'] = _restored_to(db, it['id'], it['path'])
    result = dict(id=batch, batch_id=batch, action=row['action'], created_at=row['created_at'], items=items)
    if linked:
        result['closeout_id'] = linked[0]
    return result


def history(state_dir):
    with _journal(state_dir) as (db, _):
        return [_result(db, r[0]) for r in db.execute('SELECT id FROM batch ORDER BY rowid DESC')]


def _execute_one(db, state, roots, action, uid, rec, trash_override):
    p = _path(rec['path'])
    root = _eligible(p, roots, state.parent)
    if rec.get('decision') not in (('safe',) if action == 'clean' else ('safe', 'review')):
        raise ValueError('处置结论不允许该操作')
    with _directory(p.parent) as src, _locked_file(src, p, rec):
        dest = (trash_override or _trash_directory)(p) if action == 'trash' else _vault(state, root, rec['dev'])
        _private(dest)
        name = uid + p.suffix[:24] if action != 'trash' else uid[:12] + '-' + p.name
        with _directory(dest) as target:
            if os.fstat(target).st_dev != rec['dev']:
                raise ValueError('同卷存储不可用')
            db.execute("UPDATE item SET stored=?,status='moving' WHERE id=?", (str(dest / name), uid))
            _check(src, p.name, rec)
            if 'ctime_ns' in rec and os.stat(p.name, dir_fd=src, follow_symlinks=False).st_ctime_ns != rec['ctime_ns']:
                raise ValueError('执行前文件状态已变化')
            _rename(src, p.name, target, name)
            try:
                _check(target, name, rec)
            except (OSError, ValueError):
                _rename(target, name, src, p.name)
                raise
            if action == 'clean':
                _set(db, uid, 'cleaning')
                os.unlink(name, dir_fd=target)
                os.fsync(target)
                _set(db, uid, 'cleaned')
            else:
                _set(db, uid, 'trashed' if action == 'trash' else 'quarantined')


def execute(state_dir, roots, records, action, trash_override=None, *, closeout_id=None):
    if action not in ('clean', 'trash', 'quarantine'):
        raise ValueError('未知操作')
    roots = sorted((_path(r) for r in roots), key=lambda p: -len(p.parts))
    with _journal(state_dir) as (db, state):
        batch = uuid.uuid4().hex
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        staged = [(uuid.uuid4().hex, {k: r[k] for k in ('path','dev','ino','size','mtime_ns','ctime_ns','decision') if k in r})
                  for r in records]
        db.execute('BEGIN')
        db.execute('INSERT INTO batch VALUES (?,?,?)', (batch, action, now))
        if closeout_id:
            db.execute('INSERT INTO task_closeout VALUES (?,?)', (batch, closeout_id))
        for uid, rec in staged:
            rec['path'] = str(rec.get('path', ''))
            db.execute("INSERT INTO item VALUES (?,?,?,?,NULL,'pending','')",
                       (uid, batch, rec['path'], json.dumps(rec)))
        db.commit()
        for uid, rec in staged:
            try:
                _execute_one(db, state, roots, action, uid, rec, trash_override)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                _failed(db, uid, str(exc))
        return _result(db, batch)


def _failed(db, uid, reason):
    phase = db.execute('SELECT status FROM item WHERE id=?', (uid,)).fetchone()[0]
    if phase == 'pending':
        _set(db, uid, 'skipped', reason)
    else:
        _recover(db, uid)
        db.execute('UPDATE item SET reason=? WHERE id=?', (reason, uid))


def _followup(state_dir, roots, batch, operation, beside=False):
    roots = [_path(r) for r in roots]
    with _journal(state_dir) as (db, state):
        rows = db.execute("SELECT * FROM item WHERE batch_id=? AND status IN ('quarantined','trashed')", (batch,)).fetchall()
        for row in rows:
            uid = row['id']
            try:
                p, stored = _path(row['path']), _path(row['stored'])
                root = _eligible(p, roots, state.parent)
                rec = json.loads(row['rec'])
                # Original directory must still be reachable and on the same volume.
                with _directory(p.parent) as src, _directory(stored.parent) as target:
                    if os.fstat(src).st_dev != rec['dev']:
                        raise ValueError('原磁盘不在线')
                    with _locked_file(target, stored, rec):
                        if operation == 'restore':
                            p = _restore_target(db, uid, src, p, beside)
                            _set(db, uid, 'restoring')
                            _rename(target, stored.name, src, p.name)
                            try:
                                _check(src, p.name, rec)
                            except (OSError, ValueError):
                                _rename(src, p.name, target, stored.name)
                                raise
                            _set(db, uid, 'restored')
                        elif row['status'] == 'trashed':
                            vault = _vault(state, root, rec['dev'])
                            _private(vault)
                            name = uid + '.discard'
                            with _directory(vault) as private:
                                db.execute('INSERT OR REPLACE INTO staging VALUES (?,?)', (uid, str(vault / name)))
                                _set(db, uid, 'securing')
                                _rename(target, stored.name, private, name)
                                try:
                                    _check(private, name, rec)
                                except (OSError, ValueError):
                                    _rename(private, name, target, stored.name)
                                    raise
                                db.execute("UPDATE item SET stored=?,status='purging' WHERE id=?",
                                           (str(vault / name), uid))
                                os.unlink(name, dir_fd=private)
                                os.fsync(private)
                                _set(db, uid, 'purged')
                        else:
                            _set(db, uid, 'purging')
                            _check(target, stored.name, rec)
                            os.unlink(stored.name, dir_fd=target)
                            os.fsync(target)
                            _set(db, uid, 'purged')
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                _failed(db, uid, str(exc))
        return _result(db, batch)


def restore(state_dir, roots, batch_id, beside=False):
    return _followup(state_dir, roots, batch_id, 'restore', beside)


def purge(state_dir, roots, batch_id):
    return _followup(state_dir, roots, batch_id, 'purge')
