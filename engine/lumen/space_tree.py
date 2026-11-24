"""Allocated space for one directory level. Traversal reads metadata only."""
import os
import stat
from pathlib import Path

from .actions import _directory


def measure(path, excluded=()):
    root = Path(path)
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("请选择有效目录")
    blocked = set(map(Path, excluded))
    hidden = {".Trash", ".Trashes", f".lumen-vault-{os.getuid()}"}
    if any(p in blocked or p.name in hidden for p in (root, *root.parents)):
        raise ValueError("当前目录已排除或受到保护")
    failures = 0
    children = []
    hardlinks = set()

    def count(info):
        if not stat.S_ISREG(info.st_mode):
            return 0
        if info.st_nlink > 1:
            identity = info.st_dev, info.st_ino
            if identity in hardlinks:
                return 0
            hardlinks.add(identity)
        return info.st_blocks * 512

    def failed(_):
        nonlocal failures
        failures += 1

    def subtree(name, parent, device, base):
        total = 0
        for folder, dirs, files, fd in os.fwalk(name, onerror=failed, follow_symlinks=False, dir_fd=parent):
            current = base if folder == name else base.parent / folder
            dirs[:] = [part for part in sorted(dirs) if part not in hidden and current / part not in blocked]
            for part in dirs[:]:
                try:
                    if os.stat(part, dir_fd=fd, follow_symlinks=False).st_dev != device:
                        dirs.remove(part)
                        failed(None)
                except OSError as error:
                    dirs.remove(part)
                    failed(error)
            for part in sorted(files):
                if part in hidden or (blocked and current / part in blocked):
                    continue
                try:
                    total += count(os.stat(part, dir_fd=fd, follow_symlinks=False))
                except OSError as error:
                    failed(error)
        return total

    with _directory(root) as fd:
        device = os.fstat(fd).st_dev
        with os.scandir(fd) as all_entries:
            names = sorted(entry.name for entry in all_entries)
        for name in names:
            child = root / name
            if name in hidden or child in blocked:
                continue
            previous = failures
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                directory = stat.S_ISDIR(info.st_mode)
                if directory and info.st_dev == device:
                    amount = subtree(name, fd, device, child)
                else:
                    amount = count(info)
                    if directory:
                        failed(None)
                children.append(dict(path=str(child), name=name, directory=directory, bytes=amount,
                                     partial=failures > previous, link=stat.S_ISLNK(info.st_mode)))
            except OSError as error:
                failed(error)
    children.sort(key=lambda row: (not row["directory"] and row["link"], -row["bytes"], row["name"].casefold()))
    return dict(path=str(root), bytes=sum(row["bytes"] for row in children), children=children, gaps=failures)
