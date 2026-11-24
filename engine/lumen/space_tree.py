"""Read-only disk browsing. Index directory totals once and list only a viewed level."""
import json
import os
import sqlite3
import stat
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from .actions import _directory

INDEX_NAME = "space-index.sqlite3"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _hidden():
    return {".Trash", ".Trashes", f".lumen-vault-{os.getuid()}"}


def _identity(info):
    return info.st_dev, info.st_ino


def _sorted_children(rows):
    return sorted(rows, key=lambda row: (row["link"], -row["bytes"], row["name"].casefold(), row["name"]))


def _row(path, size, directory=False, partial=False, link=False):
    return dict(path=str(path), name=path.name, directory=directory, bytes=size, partial=partial, link=link)


def _measure_subtree(fd, path, parent, home_device, blocked, hidden, owners, directories, depth=0):
    info = os.fstat(fd)
    total, gaps = 0, 0
    excluded_names = blocked.get(path, ())
    try:
        with os.scandir(fd) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name in hidden or entry.name in excluded_names:
                    continue
                try:
                    file = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(file.st_mode):
                        if file.st_nlink > 1:
                            if _identity(file) in owners:
                                continue
                            owners[_identity(file)] = str(path / entry.name)
                        total += file.st_blocks * 512
                    elif stat.S_ISDIR(file.st_mode):
                        if file.st_dev != home_device or depth >= 512:
                            gaps += 1
                            continue
                        subfd = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=fd)
                        try:
                            if _identity(os.fstat(subfd)) != _identity(file):
                                gaps += 1
                                continue
                            occupied, missed = _measure_subtree(subfd, path / entry.name, path, home_device, blocked,
                                                               hidden, owners, directories, depth + 1)
                            total += occupied
                            gaps += missed
                        finally:
                            os.close(subfd)
                except OSError:
                    gaps += 1
    except OSError:
        gaps += 1
    directories.append((str(path), str(parent), *map(str, _identity(info)), info.st_mtime_ns, info.st_ctime_ns, total, gaps))
    return total, gaps


def invalidate(state):
    (state / INDEX_NAME).unlink(missing_ok=True)


def build_index(state, path, excluded=()):
    root = Path(path)
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("请选择有效目录")
    blocked = set(map(Path, excluded))
    hidden = _hidden()
    if any(p in blocked or p.name in hidden for p in (root, *root.parents)):
        raise ValueError("当前目录已排除或受到保护")
    excluded_names = defaultdict(set)
    for item in blocked:
        excluded_names[item.parent].add(item.name)

    # The completed index is swapped atomically. Cancelled scans leave the old snapshot available.
    staging = state / ".space-index-building"
    staging.unlink(missing_ok=True)
    os.close(os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    directories, owners = [], {}
    try:
        with _directory(root) as fd:
            _measure_subtree(fd, root, root.parent, os.fstat(fd).st_dev, excluded_names, hidden, owners, directories)
        with closing(sqlite3.connect(staging)) as db, db:
            db.execute("PRAGMA journal_mode=OFF")
            db.executescript("""
                CREATE TABLE directories(
                    path TEXT PRIMARY KEY, parent TEXT, device TEXT, inode TEXT, modified INTEGER, changed INTEGER,
                    occupied INTEGER, gaps INTEGER
                );
                CREATE INDEX children ON directories(parent);
                CREATE TABLE links(device TEXT, inode TEXT, owner TEXT, PRIMARY KEY(device, inode));
                CREATE TABLE metadata(root TEXT, excluded TEXT);
            """)
            db.executemany("INSERT INTO directories VALUES (?, ?, ?, ?, ?, ?, ?, ?)", directories)
            db.executemany("INSERT INTO links VALUES (?, ?, ?)",
                           ((str(dev), str(ino), owner) for (dev, ino), owner in owners.items()))
            db.execute("INSERT INTO metadata VALUES (?, ?)", (str(root), json.dumps(sorted(map(str, blocked)))))
        os.replace(staging, state / INDEX_NAME)
    finally:
        staging.unlink(missing_ok=True)


def read_level(state, path, *, validate=True):
    file = state / INDEX_NAME
    if not file.is_file():
        return None
    try:
        with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)) as db:
            stored = db.execute("SELECT root, excluded FROM metadata").fetchone()
            if stored is None:
                return None
            root, exclude = stored
            target = Path(path)
            if target != Path(root) and Path(root) not in target.parents:
                return None
            row = db.execute(
                "SELECT device,inode,modified,changed,gaps FROM directories WHERE path=?", (str(target),)
            ).fetchone()
            if row is None:
                return None
            device, inode, modified, changed, gaps = row
            child_dirs = {
                path: (size, misses) for path, size, misses in db.execute(
                    "SELECT path,occupied,gaps FROM directories WHERE parent=? AND path!=?", (str(target), str(target))
                )
            }
            blocked = set(json.loads(exclude))
            hidden = _hidden()
            children = []
            with _directory(target) as fd:
                now = os.fstat(fd)
                if (str(now.st_dev), str(now.st_ino)) != (device, inode):
                    return None
                if validate and (now.st_mtime_ns, now.st_ctime_ns) != (modified, changed):
                    return None
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if entry.name in hidden:
                            continue
                        child = target / entry.name
                        if str(child) in blocked:
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            gaps += 1
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            size, missing = child_dirs.get(str(child), (0, 1))
                            children.append(_row(child, size, directory=True, partial=missing > 0))
                        elif stat.S_ISREG(info.st_mode):
                            size = info.st_blocks * 512
                            if info.st_nlink > 1:
                                link = db.execute("SELECT owner FROM links WHERE device=? AND inode=?",
                                                  tuple(map(str, _identity(info)))).fetchone()
                                if link and link[0] != str(child):
                                    size = 0
                            children.append(_row(child, size))
                        else:
                            children.append(_row(child, 0, link=stat.S_ISLNK(info.st_mode)))
            return dict(path=str(target), bytes=sum(row["bytes"] for row in children), children=_sorted_children(children),
                        gaps=gaps, indexed_at=file.stat().st_mtime)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None
