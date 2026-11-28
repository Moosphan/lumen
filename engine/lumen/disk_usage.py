"""Read-only startup disk accounting, independent of cleanup eligibility."""
import errno
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
ARCHIVES = {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".zst"}
INSTALLERS = {".dmg", ".pkg", ".mpkg", ".iso"}
VIRTUAL_DISKS = {".qcow2", ".vmdk", ".vdi", ".vhd", ".vhdx"}
LIBRARY_FOLDERS = {
    "caches": "caches", "logs": "logs", "application support": "app_support",
    "containers": "containers", "group containers": "shared_containers",
    "developer": "developer", "cloudstorage": "cloud", "mobile documents": "cloud",
    "preferences": "preferences",
}
DEPENDENCIES = {"node_modules", ".npm", ".gradle", ".pub-cache", ".cargo", ".venv"}
BUILD_FOLDERS = {"build", "dist", "deriveddata", ".build", ".dart_tool", ".next"}


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


def _detail_location(path, home):
    if home in path.parents:
        relative = path.relative_to(home)
        depth = 3 if relative.parts[0] == "Library" else 2
        return home.joinpath(*relative.parts[:min(depth, len(relative.parts) - 1)])
    depth = 3 if path.parts[1] in ("Users", "Library", "private") else 2
    return Path("/").joinpath(*path.parts[1:1 + min(depth, len(path.parts) - 2)])


def _detail_kind(path, kind, group, home):
    parts = path.parts
    folders = {part.lower() for part in parts[:-1]}
    suffix = path.suffix.lower()
    if suffix in VIRTUAL_DISKS or (path.name == "Docker.raw" and "com.docker.docker" in folders):
        return "virtual_disks"
    if group == "library":
        library = home / "Library"
        if library not in path.parents:
            library = Path("/").joinpath(*parts[1:4]) if parts[1] == "Users" else Path("/Library")
        section = path.relative_to(library).parts if library in path.parents else ()
        if section[:2] == ("Application Support", "MobileSync"):
            return "device_backups"
        if section[:2] == ("Developer", "CoreSimulator"):
            return "simulators"
        return LIBRARY_FOLDERS.get(section[0].lower(), "library_other") if len(section) > 1 else "library_other"
    if group == "apps":
        return "app_bundle"
    if group == "models":
        return "weights" if kind == "model" else "model_support"
    if ".git" in folders:
        return "git"
    if folders & DEPENDENCIES:
        return "dependencies"
    if suffix in ARCHIVES:
        return "archives"
    if suffix in INSTALLERS:
        return "installers"
    if folders & BUILD_FOLDERS:
        return "build"
    if kind in ("image", "audio", "video"):
        return kind
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".txt", ".md", ".rtf", ".csv", ".tsv"}:
        return "text"
    if suffix in DOCUMENTS:
        return "office"
    if suffix in {".sqlite", ".sqlite3", ".db"}:
        return "databases"
    if suffix == ".log":
        return "logs"
    if suffix == ".tmp" or folders & {".cache", "caches", "tmp"}:
        return "caches"
    return "other"


def _skip_reason(error):
    return {
        errno.ETIMEDOUT: "timeout", errno.EACCES: "permission", errno.EPERM: "permission",
        errno.ENOENT: "changed", errno.ENOTDIR: "changed", errno.ELOOP: "changed",
    }.get(error.errno, "unreadable")


def _walk_disk(directory, fd, device, blocked, missed, depth=0):
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                path = directory / entry.name
                if path in blocked:
                    missed(path, "excluded")
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if info.st_dev != device:
                        missed(path, "volume")
                    elif stat.S_ISREG(info.st_mode):
                        yield path, info
                    elif stat.S_ISDIR(info.st_mode):
                        if depth == 128:
                            missed(path, "depth")
                            continue
                        child_fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
                        try:
                            pinned = os.fstat(child_fd)
                            if (pinned.st_dev, pinned.st_ino) == (info.st_dev, info.st_ino):
                                yield from _walk_disk(path, child_fd, device, blocked, missed, depth + 1)
                            else:
                                missed(path, "changed")
                        finally:
                            os.close(child_fd)
                except OSError as error:
                    missed(path, _skip_reason(error))
    except OSError as error:
        missed(directory, _skip_reason(error))


def _add_amount(records, key, size):
    records[key][0] += size
    records[key][1] += 1


def _by_size(records):
    return sorted(records.items(), key=lambda item: (-item[1][0], str(item[0])))


def _location_rows(records):
    return [dict(path=str(path), bytes=size, count=count) for path, (size, count) in _by_size(records)]


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
    totals = defaultdict(int)
    found = defaultdict(lambda: [0, 0])
    locations = defaultdict(lambda: [0, 0])
    subdivisions = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    subdivision_locations = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    home = _data_path(home)
    links = set()
    skipped = defaultdict(int)
    examples = defaultdict(list)

    def missed(path, reason):
        skipped[reason] += 1
        if len(examples[reason]) < 6:
            examples[reason].append(dict(path=str(path), reason=reason))

    try:
        with _directory(root) as root_fd:
            device = os.fstat(root_fd).st_dev
            indexed = {(row["dev"], row["ino"]) for row in report.get("files", ()) if row["dev"] == device}
            indexed_apps = {_data_path(Path(app["path"])) for app in report.get("apps", ())
                            if app.get("inventory_device") == device and app.get("inventory_bytes", 0) > 0}
            for path, info in _walk_disk(_data_path(root), root_fd, device, blocked, missed):
                if not info.st_blocks:
                    continue
                key = info.st_dev, info.st_ino
                if info.st_nlink > 1:
                    if key in links:
                        continue
                    links.add(key)
                kind = _kind(path)
                group = classify(path, kind)
                size = info.st_blocks * 512
                totals[group] += size
                if key in indexed:
                    continue
                if group == "apps" and any(parent in indexed_apps for parent in path.parents):
                    continue
                subdivision = _detail_kind(path, kind, group, home)
                location = _detail_location(path, home)
                _add_amount(found, group, size)
                _add_amount(locations, location, size)
                _add_amount(subdivisions[group], subdivision, size)
                _add_amount(subdivision_locations[group, subdivision], location, size)
        total, free = _capacity(root)
    except OSError as error:
        raise ValueError("暂时无法读取启动盘数据卷，请稍后重新盘点。") from error
    if root == DATA_VOLUME:
        totals["macos"] = _system_used()
    result = _result(totals, total, free, coverage="disk", gaps=sum(skipped.values()))
    result["unclassified_details"] = dict(
        found_bytes=sum(size for size, _ in found.values()),
        remaining_bytes=max(0, result["used_bytes"] - result["classified_bytes"]),
        categories=[
            dict(id=group, bytes=size, count=count, subcategories=[
                dict(id=kind, bytes=amount, count=items,
                     locations=_location_rows(subdivision_locations[group, kind]))
                for kind, (amount, items) in _by_size(subdivisions[group])
            ]) for group, (size, count) in _by_size(found)
        ],
        locations=_location_rows(locations),
        skipped_reasons=dict(skipped),
        skipped_locations=[item for reason in sorted(examples) for item in examples[reason]],
    )
    return result
