"""Read-only startup disk accounting, independent of cleanup eligibility."""
import os
import stat
import subprocess
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from .actions import _directory
from .scan import _kind


DATA_VOLUME = Path("/System/Volumes/Data")
ORDER = ("apps", "agent", "documents", "media", "downloads", "library", "models",
         "trash", "macos", "other", "unclassified")
DOCUMENTS = {".txt", ".md", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
             ".pages", ".numbers", ".key", ".rtf", ".odt", ".epub"}


def _data_path(path):
    if path.parts[:len(DATA_VOLUME.parts)] == DATA_VOLUME.parts:
        return Path("/").joinpath(*path.parts[len(DATA_VOLUME.parts):])
    return path


def _classifier(home, scopes):
    roots = {_data_path(Path(path)): kind for path, kind in scopes.items()}
    home = _data_path(home)

    def personal_folder(folder):
        return folder.parent == home or folder.parent.parent == Path("/Users")

    @lru_cache(maxsize=8192)
    def location(folder):
        if folder in roots:
            return roots[folder]
        if folder == folder.parent:
            return ""
        if folder.name in (".Trash", ".Trashes"):
            return "trash"
        if folder.name.lower().endswith(".app") and "Applications" in folder.parts:
            return "apps"
        if folder.name == "Downloads" and personal_folder(folder):
            return "downloads"
        if folder.name == "Documents" and personal_folder(folder):
            return "documents"
        if folder.name == "Library" and (folder == Path("/Library") or Path("/Users") in folder.parents
                                         or home in folder.parents):
            return "library"
        return location(folder.parent)

    def classify(path, kind, agent=False):
        path = _data_path(path)
        group = location(path.parent)
        if group in ("trash", "apps"):
            return group
        if kind == "model" or group == "models":
            return "models"
        if agent or group == "agent":
            return "agent"
        if group and group != "documents":
            return group
        if kind in ("image", "audio", "video"):
            return "media"
        if group == "documents" or path.suffix.lower() in DOCUMENTS:
            return "documents"
        return "other"

    return classify


def _result(totals, total, free, *, coverage, gaps=0):
    free = min(total, max(0, free))
    used = total - free
    classified = sum(totals.values())
    totals = {**totals, "unclassified": max(0, used - classified)}
    return dict(total_bytes=total, free_bytes=free, used_bytes=used, classified_bytes=classified,
                allocation_overlaps=classified > used, coverage=coverage, gaps=gaps,
                categories=[dict(id=kind, bytes=totals[kind]) for kind in ORDER if totals.get(kind, 0) > 0])


def reconcile(files, apps, home, devices, scopes, *, total, free, system_bytes=0):
    classify = _classifier(home, scopes)
    identities = {}
    for row in files:
        if row["dev"] not in devices:
            continue
        key = row["dev"], row["ino"]
        if key not in identities:
            group = classify(Path(row["path"]), row["kind"], bool(row["projects"] or row["agents"]))
            identities[key] = group, row["allocated_bytes"]
    totals = defaultdict(int)
    for group, size in identities.values():
        totals[group] += size
    totals["apps"] += sum(row.get("inventory_bytes", 0) for row in apps
                          if row.get("inventory_device") in devices and not (system_bytes and row.get("system")))
    totals["macos"] = system_bytes
    return _result(totals, total, free, coverage="indexed")


def _system_used():
    # df reports the usage of the mounted system volume, whose free space is
    # shared with the Data volume. Do not sum volume capacities.
    try:
        output = subprocess.check_output(["/bin/df", "-kP", "/"], text=True, timeout=3,
                                         stderr=subprocess.DEVNULL, env={"LC_ALL": "C"})
        fields = output.splitlines()[-1].split()
        return int(fields[2]) * 1024 if fields[0].startswith("/dev/") and fields[-1] == "/" else 0
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0


def _capacity(root):
    volume = os.statvfs(root)
    return volume.f_blocks * volume.f_frsize, volume.f_bavail * volume.f_frsize


def _scopes(report):
    scopes = {row["path"]: ("models" if row.get("model_store") else "agent")
              for row in report.get("local_locations", []) if row.get("model_store") or row.get("agent_store")}
    for project in report.get("projects", {}):
        scopes.setdefault(project, "agent")
    return scopes


def _root(home, system):
    if system:
        try:
            if os.statvfs(DATA_VOLUME).f_fsid != os.statvfs("/").f_fsid:
                return DATA_VOLUME
        except OSError:
            pass
        return Path("/")
    return home


def overview(report, home, system):
    root = _root(home, system)
    total, free = _capacity(root)
    devices = {root.stat().st_dev}
    if root == DATA_VOLUME:
        devices.add(Path("/").stat().st_dev)
    system_bytes = _system_used() if root == DATA_VOLUME else 0
    return reconcile(report.get("files", ()), report.get("apps", ()), home, devices, _scopes(report),
                     total=total, free=free, system_bytes=system_bytes)


def scan_disk(home, system, report, excluded):
    root = _root(home, system)
    if root == Path("/"):
        raise ValueError("无法定位启动盘数据卷，当前仅可查看已盘点的分类")
    classify = _classifier(home, _scopes(report))
    blocked = {_data_path(Path(path)) for path in excluded} | {
        Path("/Volumes"), Path("/dev"), Path("/private/var/run"),
        Path("/private/var/db"), Path("/private/var/root"),
        Path("/Library/Application Support/com.apple.TCC"),
    }
    device = root.stat().st_dev
    totals = defaultdict(int)
    links = set()
    gaps = 0

    def missed(_):
        nonlocal gaps
        gaps += 1

    with _directory(root) as root_fd:
        for folder, dirs, names, fd in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False, onerror=missed):
            directory = root / folder
            retained = []
            for name in dirs:
                child = directory / name
                if _data_path(child) in blocked:
                    gaps += 1
                    continue
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and info.st_dev == device:
                        retained.append(name)
                    elif not stat.S_ISLNK(info.st_mode):
                        gaps += 1
                except OSError:
                    gaps += 1
            dirs[:] = retained
            for name in names:
                path = directory / name
                if _data_path(path) in blocked:
                    gaps += 1
                    continue
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    gaps += 1
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_dev != device or not info.st_blocks:
                    continue
                if info.st_nlink > 1:
                    key = info.st_dev, info.st_ino
                    if key in links:
                        continue
                    links.add(key)
                totals[classify(path, _kind(path))] += info.st_blocks * 512
    if root == DATA_VOLUME:
        totals["macos"] = _system_used()
    total, free = _capacity(root)
    return _result(totals, total, free, coverage="disk", gaps=gaps)
