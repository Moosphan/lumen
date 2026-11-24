"""Classify regenerable app files conservatively. Direct cleanup uses existing version rules."""
import re
from collections import defaultdict
from pathlib import Path


def available(identifier):
    from .scan import _registered_code

    try:
        active, _ = _registered_code(identifier)
        return not active
    except (OSError, ValueError, AttributeError):
        return False


def extensions(home, apps, requested, excluded):
    from .service import root_identity

    result = []
    for app, record in apps.items():
        identifier = record["bundle_id"]
        if str(app) not in requested or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}", identifier):
            continue
        container = home / "Library/Containers" / identifier / "Data"
        for directory in (container / "Library/Caches", container / "tmp"):
            if directory in excluded or any(parent in excluded for parent in directory.parents):
                continue
            identity = root_identity(str(directory))
            if identity:
                result.append(dict(path=str(directory), title="应用容器 · " + directory.name,
                                   group="资源库", identity=identity, agents=[], app_cleanup=True))
    return result


def classify(report, home):
    by_bundle = defaultdict(list)
    for app in report["apps"]:
        by_bundle[app["bundle_id"]].append(app)
    library = home / "Library"
    for row in report["files"]:
        if "Library" not in row["path"]:
            continue
        path = Path(row["path"])
        try:
            top, *parts = path.relative_to(library).parts
        except ValueError:
            continue
        if not parts:
            continue
        category = ""
        reason = ""
        identifier = row["bundle_id"]
        if top == "Caches" and identifier and parts[0] == identifier and len(parts) > 1:
            category, reason = "cache", "应用专属缓存。隔离后可能需要重新下载或生成缓存。"
        elif top == "Logs" and identifier and parts[0] == identifier and row["kind"] == "log":
            category, reason = "logs", "应用诊断日志。请保留排查问题所需的历史记录。"
        elif top == "Containers" and identifier and parts[0] == identifier:
            if parts[1:4] == ["Data", "Library", "Caches"] and len(parts) > 4:
                category, reason = "cache", "应用沙盒内的缓存。隔离后可能需要重新生成。"
            elif parts[1:3] == ["Data", "tmp"] and len(parts) > 3:
                category, reason = "temporary", "应用沙盒内的临时文件。请退出应用并确认任务已结束。"
        elif top == "Application Support" and parts[:1] == ["Code"] and len(parts) >= 3:
            if parts[1] in {"Cache", "Code Cache", "GPUCache", "CachedData"}:
                category, reason = "cache", "VS Code 的运行缓存。隔离后可能延长下次启动。"
            elif parts[1] == "logs" and row["kind"] == "log":
                category, reason = "logs", "VS Code 会话诊断日志。请保留仍需排查的会话。"
        elif top == "Developer" and parts[:2] == ["Xcode", "DerivedData"] and len(parts) >= 4:
            identifier = "com.apple.dt.Xcode"
            if (parts[2] == "ModuleCache.noindex" or parts[3] in {"Index.noindex", "Index"} or
                    parts[3:5] == ["Build", "Intermediates.noindex"]):
                category, reason = "build", "Xcode 生成的索引、模块缓存或编译中间文件。下次构建时可能重建。"
        if not category:
            continue
        matches = by_bundle.get(identifier, [])
        if len(matches) != 1 or row.get("shared_owners"):
            continue
        app = matches[0]
        if row["app_path"] and row["app_path"] != app["path"]:
            continue
        row.update(owner=app["name"], app_path=app["path"], bundle_id=identifier,
                   app_version=app["version"], cleanup_kind=category)
        personal = {"cookies", "login data", "local storage", "session storage", "indexeddb", "session",
                    "sessions", "preferences", "globalstorage", "workspacestorage", "service worker", "auth.json"}
        if row["decision"] == "review" and any(part.casefold() in personal for part in parts[1:]):
            row.update(decision="keep", reason="应用登录、配置或会话数据需要保留。")
        if row["decision"] == "review":
            row["reason"] = reason
