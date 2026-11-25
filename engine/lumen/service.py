"""Authorize scopes, persist local state and revalidate previews before actions."""
import fcntl
import json
import os
import stat
import tempfile
import time
import uuid
from pathlib import Path

from . import actions, app_cleanup, integrations, local, macmanage, space_tree
from .fixture import create_fixture
from .scan import _under, scan


def inside(path, root):
    return path == root or root in path.parents


def paths(values):
    if not isinstance(values, list) or any(not isinstance(p, str) or not os.path.isabs(p) for p in values):
        raise ValueError("需要绝对路径列表")
    return list(dict.fromkeys(os.path.normpath(p) for p in values))


def load_config(state):
    file = state / "workspace.json"
    if file.exists():
        return json.loads(file.read_text())
    return {"roots": [], "deep": True, "keep": [], "exclude": [], "projects": {},
            "tasks": [], "budget": 0, "usage": {}, "report": {}}


def save_config(state, config):
    fd, tmp = tempfile.mkstemp(dir=state, prefix=".workspace-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps({k: value for k, value in config.items() if not k.startswith("_")},
                                    ensure_ascii=False, separators=(",", ":")))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, state / "workspace.json")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def root_identity(path):
    p = Path(path)
    try:
        if not p.is_dir() or p.resolve() != p:
            return None
        info = p.stat()
        return [info.st_dev, info.st_ino]
    except OSError:
        return None


def active_roots(config):
    grants = config.get("root_ids", {})
    return [r for r in config["roots"] if grants.get(r) and grants[r] == root_identity(r)]


def assign_roots(config, values, state, renew=()):
    new = paths(values)
    for value in new:
        p = Path(value)
        if p == Path("/") or p.is_symlink() or inside(p.resolve(), state.resolve()):
            raise ValueError("请选择具体的工作区或应用数据目录")
    invalidated = set(config["roots"]).difference(new).union(renew)
    previous = config.get("root_ids", {})
    config["root_ids"] = {r: (previous.get(r) if r in config["roots"] and r not in renew else root_identity(r)) for r in new}
    config["roots"] = new
    # Revocation also removes any cached asset paths and scope-specific settings.
    allowed = lambda p: any(inside(Path(p), Path(r)) for r in new)
    for key in ("keep", "exclude"):
        config[key] = [p for p in config[key] if allowed(p)]
    config["tasks"] = [task for task in config["tasks"] if allowed(task["root"])]
    for t in config["tasks"]:
        if "temporary_snapshots" in t:
            t["temporary_snapshots"] = {p: snap for p, snap in t["temporary_snapshots"].items()
                                        if allowed(p) and not any(inside(Path(p), Path(r)) for r in invalidated)}
        for field in ("inputs", "outputs", "temporary"):
            t[field] = [p for p in t[field] if allowed(p)]
    config["projects"] = {p: a for p, a in config["projects"].items() if allowed(p)}
    config["usage"] = {p: v for p, v in config["usage"].items() if allowed(p)}
    config["report"] = {}
    config.pop("trash_preview", None)
    config.pop("hash_cache", None)


def apply_settings(config, request, state):
    if "local_exclude" in request:
        config["local_exclude"] = paths(request["local_exclude"])
        config.pop("agent_metadata", None)
    if "roots" in request:
        assign_roots(config, request["roots"], state, request.get("renew_roots", ()))
    def require_scoped(value):
        if not any(inside(Path(value), Path(root)) for root in config["roots"]):
            raise ValueError("路径未获授权")
    for key in ("keep", "exclude"):
        if key in request:
            configured = paths(request[key])
            for value in configured:
                require_scoped(value)
            config[key] = configured
    if "projects" in request:
        projects = request["projects"]
        if not isinstance(projects, dict):
            raise ValueError("工程映射无效")
        for value in paths(list(projects)):
            require_scoped(value)
            if not isinstance(projects[value], list) or not all(isinstance(a, str) for a in projects[value]):
                raise ValueError("工程 Agent 列表无效")
        config["projects"] = projects
    if "automatic_rules" in request:
        rules = request["automatic_rules"]
        if not isinstance(rules, list) or any(r not in (*integrations.SAFE_RULE_IDS, integrations.TASK_TEMPORARY) for r in rules):
            raise ValueError("未知的自动清理规则")
        config["automatic_rules"] = sorted(set(rules))
    if "budget" in request:
        budget = int(request["budget"])
        if budget < 0:
            raise ValueError("预算不能为负")
        config["budget"] = budget
    if "snooze_until" in request:
        config["snooze_until"] = int(request["snooze_until"])


SNAPSHOT_FIELDS = ("dev", "ino", "size", "mtime_ns", "ctime_ns")


def register_temporaries(config, task, prior):
    # Editing other task fields cannot renew old, missing or invalidated evidence.
    snapshot = {}
    project = Path(task["root"])
    project_id = root_identity(project)
    grants = active_roots(config)
    if not project_id or not any(inside(project, Path(r)) for r in grants):
        return snapshot
    old = prior.get("temporary_snapshots", {}) if prior else {}
    for name in task["temporary"]:
        path = Path(name)
        if prior and name in prior["temporary"]:
            if name in old:
                snapshot[name] = old[name]
            continue
        if task["closed"] or project not in path.parents:
            continue
        grant = max((r for r in grants if inside(path, Path(r))), key=len, default=None)
        if not grant:
            continue
        try:
            with actions._directory(path.parent) as parent:
                info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                    continue
        except OSError:
            continue
        identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        snapshot[name] = dict(identity=identity, project_id=project_id, grant=grant,
                              grant_id=config["root_ids"][grant])
    return snapshot


def remember_task(config, request):
    root = paths([request["root"]])[0]
    if not any(inside(Path(root), Path(r)) for r in config["roots"]):
        raise ValueError("请先授权任务目录")
    agent = str(request.get("agent", "")).strip()
    if not agent:
        raise ValueError("请填写 Agent")
    task = {"root": root, "agent": agent, "closed": request.get("closed") is True}
    for field in ("inputs", "outputs", "temporary"):
        task[field] = paths(request.get(field, []))
        if any(not any(inside(Path(p), Path(r)) for r in config["roots"]) for p in task[field]):
            raise ValueError("请先授权任务引用的外部目录")
    task["id"] = str(request.get("id") or root + "|" + agent)
    prior = next((t for t in config["tasks"] if t["id"] == task["id"]), None)
    if prior and (prior["root"], prior["agent"]) != (root, agent):
        raise ValueError("任务标识对应其他工程或 Agent")
    task["temporary_snapshots"] = register_temporaries(config, task, prior)
    config["tasks"] = [t for t in config["tasks"] if t["id"] != task["id"]] + [task]
    agents = config["projects"].setdefault(root, [])
    if agent not in agents:
        agents.append(agent)
    return task


def app_protected(config, record, report):
    bundle = Path(record["path"])
    special = set(config["keep"] + config["exclude"])
    for task in config["tasks"]:
        special.update(task["inputs"] + task["outputs"])
        if not task["closed"]:
            special.update(task["temporary"])
    if any(inside(Path(p), bundle) or inside(bundle, Path(p)) for p in special):
        return True
    if any(inside(Path(ref["target"]), bundle) and not inside(Path(ref["source"]), bundle)
           for ref in report.get("static_refs", [])):
        return True
    for entry in report["files"]:
        if not inside(Path(entry["path"]), bundle):
            continue
        for reference in entry.get("refs", []):
            source, separator, line = reference.rpartition(":")
            origin = Path(source if separator and line.isdigit() else reference)
            if not inside(origin, bundle):
                return True
    return False


def inventory(config, state, incremental=False, full=False, quick=False):
    deep = full or (config["deep"] and not quick)
    protected = set(config["keep"])
    for task in config["tasks"]:
        protected.update(task["inputs"] + task["outputs"])
        if not task["closed"]:
            protected.update(task["temporary"])
    exclusions = config["exclude"] + [str(state)]
    exclusions.extend(str(Path(r) / name) for r in config["roots"]
                      for name in (".Trash", f".lumen-vault-{os.getuid()}"))
    manual = active_roots(config)
    locations, detected, installed, discovery_issues, suggestions = [], {}, None, set(), set()
    extensions = []
    if config.get("local_enabled") and config.get("_local_home"):
        blocked = [Path(p) for p in exclusions + config.get("local_exclude", [])]
        locations, detected, installed, discovery_issues, suggestions, extensions = local.plan(
            Path(config["_local_home"]), config["_include_system"], blocked, quick=not deep,
            metadata_cache=config.setdefault("agent_metadata", {}) if incremental else None,
            authorized=tuple(map(Path, manual)))
        locations += app_cleanup.extensions(Path(config["_local_home"]), installed,
                                            config.get("app_analysis", ()), set(blocked))
        exclusions += config.get("local_exclude", [])
    projects = {p: list(a) for p, a in config["projects"].items()}
    for path, agents in detected.items():
        projects[str(path)] = sorted(set(projects.get(str(path), [])) | agents)
    automatic = [row["path"] for row in locations]
    exclusions += [str(Path(r) / name) for r in automatic
                   for name in (".Trash", f".lumen-vault-{os.getuid()}")]
    report = scan(manual + automatic, deep=deep, projects=projects,
                  keep=sorted(protected), exclude=exclusions,
                  hash_cache=config.setdefault("hash_cache", {}) if incremental else None, installed=installed,
                  agent_stores={Path(row["path"]): row["agent_store"] for row in locations if row.get("agent_store")})
    report["issues"].extend(sorted(discovery_issues))
    report["local_locations"] = locations
    if installed is not None:
        app_cleanup.classify(report, Path(config["_local_home"]))
    report["suggested_projects"] = sorted(set(report["suggested_projects"]) | suggestions)
    for lost in sorted(set(config["roots"])-set(active_roots(config))):
        report["issues"].append(lost + " · 授权目录离线或身份变化，请重新选择")
    log_owners = {r["app_path"] for r in report["files"] if r["decision"] == "safe"
                  and r.get("rule_id") == "vscode.1.95.3.main-log.v1"}
    manual_paths = set(map(Path, manual))
    for app in report["apps"]:
        can_manage = any(parent in manual_paths for parent in Path(app["path"]).parents)
        if (app["bundle_id"] in macmanage.SUPPORTED_BUNDLES and (can_manage or app["path"] in log_owners)
                and not app_protected(config, app, report)):
            try:
                proof = macmanage.app_snapshot(app["path"], fixture_roots=[t.parent for t in fixture_trash(config)])
                app["verified_install"] = True
                if can_manage:
                    app["uninstall_snapshot"] = proof
            except (OSError, ValueError) as error:
                app["uninstall_reason"] = str(error)
    report["residuals"] = macmanage.residuals(state, config["roots"], report,
                                              fixture_roots=[t.parent for t in fixture_trash(config)])
    remnants = {item["path"]: item for item in report.get("residuals", [])}
    verified_apps = {app["path"] for app in report["apps"] if app.get("verified_install")}
    task_paths = {}
    for task in config["tasks"]:
        for field in ("inputs", "outputs", "temporary"):
            for target in task[field]:
                task_paths.setdefault(target, []).append(task)
    lab_roots = [t.parent for t in fixture_trash(config)]
    protected_paths = set(map(Path, protected))
    review_roots = set(map(Path, manual))
    if installed is not None:
        review_roots.update(auto_review_roots(Path(config["_local_home"])))
        review_roots.update(Path(row["path"]) for row in locations if row.get("app_cleanup"))
    for row in report["files"]:
        related = task_paths.get(row["path"], ())
        if related:
            row["projects"] = sorted(set(row["projects"]).union(t["root"] for t in related))
            row["agents"] = sorted(set(row["agents"]).union(t["agent"] for t in related))
            row["task_ids"] = sorted(t["id"] for t in related)
        if (row["decision"] == "safe" and row.get("rule_id") == "vscode.1.95.3.main-log.v1"
                and row["app_path"] not in verified_apps):
            row.update(decision="review", reason="当前安装的版本和发行方未通过完整校验")
        if (row["decision"] == "safe" and row.get("rule_id") == "fixture.rotated-log.v1"
                and not any(inside(Path(row["path"]), lab) for lab in lab_roots)):
            row.update(decision="review", reason="演练规则仅在本应用生成的样本目录内生效")
        if row["path"] in remnants and row["decision"] == "review":
            row.update(residual=True, reason=remnants[row["path"]]["reason"])
        path = Path(row["path"])
        if _under(path, protected_paths):
            row.update(decision="keep", reason="用户标记保留或任务正在引用")
        if (installed is not None and row["decision"] == "review" and not _under(path, review_roots)
                and not row.get("cleanup_kind")):
            row.update(decision="keep", reason="自动发现的位置仅盘点，可在扫描范围添加后审阅")
    local.summarize(report)
    report["agent_inventory"] = local.agent_inventory(report, extensions)
    return report


def auto_review_roots(home):
    return [home / p for p in ("Downloads", "Library/Caches", "Library/Logs", "Library/Developer/Xcode/DerivedData")]


def operation_roots(config, report):
    roots = active_roots(config)
    if not config.get("local_enabled") or not config.get("_local_home"):
        return roots
    home = Path(config["_local_home"])
    supported = auto_review_roots(home) + [home / "Library/Application Support/Code"]
    supported += [Path(row["path"]) for row in report.get("local_locations", []) if row.get("app_cleanup")]
    indexed = {row["path"]: row["identity"] for row in report.get("local_locations", [])}
    return roots + [str(root) for root in supported if str(root) in indexed and
                    indexed[str(root)] == root_identity(root) and
                    not any(inside(root, Path(p)) for p in config.get("local_exclude", []))]


def refresh(config, state, quick=False):
    report = inventory(config, state, incremental=True, quick=quick)
    usage = {}
    identities = {}
    for item in report["files"]:
        identity = (item["dev"], item["ino"])
        memberships, _ = identities.setdefault(identity, (set(), item.get("allocated_bytes", item["size"])))
        memberships.update(item.get("projects", []))
    for memberships, size in identities.values():
        if memberships:
            key = next(iter(memberships)) if len(memberships) == 1 else "共享工作区"
            usage[key] = usage.get(key, 0) + size
    baseline = config.get("usage", {})
    report["growth"] = {p: value - baseline[p] for p, value in usage.items() if p in baseline}
    report["scan_id"] = uuid.uuid4().hex
    report["alerts"] = [p for p, v in usage.items() if config["budget"] and v > config["budget"]
                        and time.time() >= config.get("snooze_until", 0)]
    config.update(usage=usage, report=report)
    return report


def perform(config, state, request, temporary_task=None, planned_paths=()):
    requested = paths(request.get("paths", []))
    action = request.get("action")
    if not requested or action not in ("quarantine", "clean", "trash"):
        raise ValueError("请选择文件和有效操作")
    preview = {r["path"]: r for r in config["report"].get("files", [])}
    fresh = inventory(config, state, full=True)
    current = {r["path"]: r for r in fresh["files"]}
    selected, skipped = [], []
    chosen = set(requested).union(planned_paths)
    manual = list(map(Path, active_roots(config)))
    manual_or_review = manual + (auto_review_roots(Path(config["_local_home"])) if config.get("_local_home") else [])
    previous_roots = set(operation_roots(config, config["report"]))
    roots = [root for root in operation_roots(config, fresh) if root in previous_roots]
    idle_apps = {}
    for path in requested:
        old, row = preview.get(path), current.get(path)
        fields = ("dev", "ino", "size", "mtime_ns", "ctime_ns", "decision", "rule_id", "cleanup_kind", "app_path")
        if not old or not row or any(old.get(f) != row.get(f) for f in fields):
            skipped.append({"path": path, "status": "skipped", "reason": "预演已过期，请重新扫描"})
            continue
        if (action != "clean" and not any(inside(Path(path), root) for root in manual_or_review)
                and not row.get("cleanup_kind")):
            skipped.append({"path": path, "status": "skipped", "reason": "此位置仅支持低影响清理"})
            continue
        if temporary_task and not eligible_temporary(config, temporary_task, row):
            skipped.append({"path": path, "status":"skipped", "reason":"任务登记身份或保护状态已改变"})
            continue
        if row.get("cleanup_kind") and row["decision"] == "review":
            identifier = row["bundle_id"]
            if identifier not in idle_apps:
                idle_apps[identifier] = app_cleanup.available(identifier)
            if not idle_apps[identifier]:
                skipped.append({"path": path, "status": "skipped", "reason": "请先退出应用，既白需要确认它已停止运行"})
                continue
        group = row.get("duplicate_group")
        copies = fresh.get("duplicates", {}).get(group, []) if group else []
        if copies and all(p in chosen for p in copies):
            skipped.append({"path": path, "status": "skipped", "reason": "重复组必须保留至少一份"})
            continue
        selected.append(row)
    if selected and action == "trash":
        result = actions.execute(state, roots, selected, action,
                                 trash_override=lambda p: trash_destination(config, p))
    else:
        result = actions.execute(state, roots, selected, action) if selected else {"items": []}
    result.setdefault("items", []).extend(skipped)
    return result


def eligible_temporary(config, task, row):
    proof = task.get("temporary_snapshots", {}).get(row["path"])
    return bool(proof and Path(task["root"]) in Path(row["path"]).parents
                and proof["grant"] in active_roots(config)
                and proof["grant_id"] == config["root_ids"].get(proof["grant"])
                and proof["project_id"] == root_identity(task["root"])
                and proof["identity"] == [row.get(k) for k in SNAPSHOT_FIELDS]
                and row["decision"] in ("review", "safe") and row["nlink"] == 1
                and row["kind"] not in ("model", "symlink") and not row.get("refs")
                and not row.get("shared_owners") and not set(row.get("projects", [])).difference([task["root"]]))


def finish_task(config, state, task, previous_temporaries=()):
    enabled = config.get("automatic_rules", [])
    if task["id"].startswith("claude-session:"):
        seen = config.setdefault("finished_session_ids", [])
        if task["id"] not in seen:
            seen.append(task["id"])
    if not enabled:
        return []
    # Persist the closed state before the first move. Replayed hooks cannot start
    # another batch after a process interruption. The action journal covers moves.
    save_config(state, config)
    report = inventory(config, state, full=True)
    config["report"] = report
    declared = {Path(p) for t in config["tasks"] for p in t["temporary"]}
    declared.update(Path(p) for p in previous_temporaries)
    disposable = [p for p in integrations.automatic_paths(report, enabled)
                  if inside(Path(p), Path(task["root"])) and not any(inside(Path(p), d) for d in declared)]
    temporary, skips = [], []
    if integrations.TASK_TEMPORARY in enabled:
        current = {row["path"]: row for row in report["files"]}
        for path in task["temporary"]:
            row = current.get(path)
            if path not in task.get("temporary_snapshots", {}):
                reason = "登记时缺少文件身份快照或原授权已撤回"
            elif not row:
                reason = "文件已缺失、排除或所属卷不在线"
            elif row["decision"] not in ("review", "safe"):
                reason = row["reason"]
            elif not eligible_temporary(config, task, row):
                reason = "文件身份、归属或引用与登记条件不符"
            else:
                temporary.append(path)
                continue
            skips.append({"path":path, "status":"skipped", "reason":reason})
    # Both rules must jointly leave a copy even when they create different batches.
    planned = set(disposable + temporary)
    blocked = {p for group in report["duplicates"].values() if set(group) <= planned for p in group}
    results = []
    for action, chosen in (("quarantine", temporary), ("clean", disposable)):
        paths_to_run = [p for p in chosen if p not in blocked]
        skipped = [{"path":p, "status":"skipped", "reason":"重复组必须保留至少一份"} for p in chosen if p in blocked]
        if action == "quarantine":
            skipped.extend(skips)
        if not paths_to_run and not skipped:
            continue
        locations = {Path(p).parent for p in paths_to_run}
        before = volume_free(locations)
        result = {"action":action, "items":[]}
        if paths_to_run:
            result = perform(config, state, {"paths":paths_to_run, "action":action},
                             temporary_task=task if action == "quarantine" else None, planned_paths=planned)
        after = volume_free(locations)
        result.setdefault("action", action)
        result["items"].extend(skipped)
        measure_batch(config, result, before, after)
        results.append(result)
    return results


def fixture_trash(config):
    found = []
    for name, identity in config.get("labs", {}).items():
        root = Path(name)
        try:
            info = root.lstat()
        except FileNotFoundError:
            continue
        if (not root.is_symlink() and [info.st_dev, info.st_ino] == identity
                and any(inside(root, Path(r)) for r in active_roots(config))):
            found.append(root / ".Trash")
    return found


def trash_destination(config, path):
    for name in config.get("labs", {}):
        if inside(Path(path), Path(name)):
            expected = Path(name) / ".Trash"
            if expected not in fixture_trash(config):
                raise ValueError("演练根目录身份已改变")
            return expected
    return actions._trash_directory(Path(path))


def trash_roots(config):
    labs = fixture_trash(config)
    explicit = [Path(r) for r in active_roots(config) if Path(r).name == ".Trash"
                or Path(r).parent.name == ".Trashes"]
    return list(dict.fromkeys(explicit + labs)), labs


def volume_free(roots):
    free = {}
    for path in roots:
        try:
            dev = os.stat(path).st_dev
            stat = os.statvfs(path)
            free[dev] = stat.f_bavail * stat.f_frsize
        except OSError:
            continue
    return free


def measure_batch(config, result, before, after):
    processed = [i for i in result["items"] if i["status"] in
                 ("cleaned", "quarantined", "trashed", "restored", "purged")]
    delta = sum(after[k] - v for k, v in before.items()) if processed and before and before.keys() == after.keys() else None
    metrics = {"logical_processed_bytes":sum(i.get("size", 0) for i in processed), "measured_free_delta":delta}
    result.update(metrics)
    if result.get("batch_id"):
        config.setdefault("measurements", {})[result["batch_id"]] = metrics


def followup(module, command, state, config, request):
    roots = active_roots(config) if module is macmanage else operation_roots(config, config["report"])
    if command == "restore" and request.get("beside") is True:
        return module.restore(state, roots, request["batch_id"], beside=True)
    return getattr(module, command)(state, roots, request["batch_id"])


def require_preview(config, request):
    expected = config["report"].get("scan_id")
    if not expected or request.get("scan_id") != expected:
        raise ValueError("当前清单已改变，请重新扫描并确认")


def dispatch(state, request, local_home=None, include_system=False):
    state = Path(state).expanduser().absolute()
    if not isinstance(request, dict):
        raise ValueError("需要 JSON 对象")
    cmd = request.get("cmd")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise ValueError("状态目录必须为当前用户所有，仅本人可读写")
    with (state / ".service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if cmd == "size_tree" and request.get("refresh") is not True:
            cached = space_tree.read_level(state, request.get("path") or local_home or Path.home())
            if cached is not None:
                return cached
        config = load_config(state)
        if local_home is not None:
            config.update(_local_home=str(local_home), _include_system=include_system)
        last = None
        completed_batches = []
        duplicate = False
        startup = cmd == "scan_local" and request.get("startup") is True
        if cmd == "size_tree":
            labs = list(map(Path, config.get("labs", {})))
            if labs:
                target = Path(request.get("path") or labs[0])
                if target.resolve(strict=True) != target or not any(inside(target, root) for root in labs):
                    raise ValueError("演练只读取样本目录")
            else:
                target = Path(request.get("path") or config.get("_local_home") or Path.home())
            space_tree.build_index(state, target, [state, *config["exclude"], *config.get("local_exclude", [])])
            level = space_tree.read_level(state, target, validate=False)
            if level is None:
                raise ValueError("目录在盘点时发生变化，请重新盘点")
            return level
        if cmd == "fixture":
            space_tree.invalidate(state)
            root = Path(create_fixture())
            (root / ".Trash").mkdir(mode=0o700)
            (root / ".Trash" / "old-fixture.txt").write_text("synthetic trash")
            st = root.stat()
            config.setdefault("labs", {})[str(root)] = [st.st_dev, st.st_ino]
            save_config(state, config)
            return {"root": str(root)}
        measures_space = cmd in ("run","purge","restore","empty_trash","uninstall")
        if (measures_space or cmd in ("task", "session_end") or
                any(key in request for key in ("exclude", "local_exclude", "roots"))):
            space_tree.invalidate(state)
        before_space = volume_free(operation_roots(config, config["report"])) if measures_space else {}
        if cmd in ("run", "uninstall", "empty_trash"):
            require_preview(config, request)
        if cmd == "scan_local":
            if not config.get("_local_home") or config.get("labs"):
                raise ValueError("当前工作区不支持本机盘点")
            config["local_enabled"] = True
            if not startup:
                config["deep"] = bool(request.get("deep", False))
        elif cmd == "scan_app":
            if not config.get("local_enabled"):
                raise ValueError("请先完成本机盘点")
            app = request["path"]
            if not any(item["path"] == app for item in config["report"].get("apps", [])):
                raise ValueError("当前应用清单已变化，请重新扫描")
            config["app_analysis"] = sorted(set(config.get("app_analysis", ())) | {app})
        elif cmd == "scan":
            if "roots" in request and (request["roots"] != config["roots"] or request.get("renew_roots")):
                assign_roots(config, request["roots"], state, request.get("renew_roots", ()))
            config["deep"] = bool(request.get("deep", config["deep"]))
        elif cmd == "settings":
            apply_settings(config, request, state)
        elif cmd == "task":
            identifier = request.get("id") or request["root"] + "|" + request["agent"]
            prior = next((t for t in config["tasks"] if t["id"] == identifier), None)
            if prior and request.get("closed") is True:
                if (request["root"], request["agent"]) != (prior["root"], prior["agent"]):
                    raise ValueError("任务标识对应其他工程或 Agent")
                if prior["closed"]:
                    duplicate = True
                else:
                    # A close-only request keeps all registered protections.
                    closing = {**prior, **request}
                    for key in ("inputs", "outputs"):
                        closing[key] = list(dict.fromkeys(prior[key] + request.get(key, [])))
                    task = remember_task(config, closing)
                    completed_batches = finish_task(config, state, task, previous_temporaries=prior["temporary"])
            else:
                remember_task(config, request)
        elif cmd == "run":
            last = perform(config, state, request)
        elif cmd == "session_end":
            task = integrations.claude_session_end(request["event"], active_roots(config))
            prior = next((t for t in config["tasks"] if t["id"] == task["id"]), None)
            if prior and (prior["root"], prior["agent"]) != (task["root"], task["agent"]):
                raise ValueError("会话标识对应其他工程")
            if (prior and prior["closed"]) or task["id"] in config.get("finished_session_ids", []):
                return {"duplicate": True, "task_id": task["id"]}
            if prior:
                prior["closed"] = True
                task = prior
            else:
                task = remember_task(config, task)
            completed_batches = finish_task(config, state, task)
        elif cmd in ("trash_list", "empty_trash", "uninstall"):
            if cmd == "uninstall":
                target = request["path"]
                old = next((a.get("uninstall_snapshot") for a in config["report"]["apps"] if a["path"] == target), None)
                if not old:
                    raise ValueError("该应用缺少可验证的卸载规则")
                current = inventory(config, state, full=True)
                verified = next((app for app in current["apps"] if app["path"] == target), None)
                if not verified or verified.get("uninstall_snapshot") != old:
                    raise ValueError("应用有保留项、引用或快照变化，请重新扫描")
                last = macmanage.uninstall(state, active_roots(config), old, fixture_roots=[t.parent for t in fixture_trash(config)])
            else:
                allowed, labs = trash_roots(config)
                if cmd == "empty_trash":
                    selected = set(paths(request.get("paths", [])))
                    previous = [r for vol in config.get("trash_preview", []) for r in vol["items"] if r["path"] in selected]
                    if not selected or selected != {r["path"] for r in previous}:
                        raise ValueError("请先查看废纸篓并选择已预览的条目")
                    current = inventory(config, state, full=True)
                    ready = [r for r in previous if not app_protected(config, r, current)]
                    last = macmanage.empty_trash(allowed, ready, fixture_trash=labs)
                    last["items"].extend({"path":r["path"],"status":"skipped","reason":"用户保留、排除或工程引用"}
                                         for r in previous if r not in ready)
                config["trash_preview"] = macmanage.trash_inventory(allowed, fixture_trash=labs)
        elif cmd in ("restore", "purge") and str(request["batch_id"]).startswith("app-"):
            last = followup(macmanage, cmd, state, config, request)
        elif cmd in ("restore", "purge"):
            last = followup(actions, cmd, state, config, request)
        elif cmd != "status":
            raise ValueError("未知操作")
        if completed_batches:
            last = completed_batches[-1]
        if last is not None:
            if not completed_batches:
                measure_batch(config, last, before_space, volume_free(operation_roots(config, config["report"])))
            if cmd == "empty_trash":
                import datetime
                last.update(id="trash-" + uuid.uuid4().hex, action="empty_trash",
                            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                config.setdefault("trash_history", []).insert(0, last)
        if cmd != "status":
            refresh(config, state, quick=startup or cmd == "scan_app")
            save_config(state, config)
        dashboard = dict(config.get("report", {}))
        for key, default in (("files", []), ("issues", []), ("projects", {}), ("apps", []), ("duplicates", {})):
            dashboard.setdefault(key, default)
        dashboard.update({k: config[k] for k in ("keep", "exclude", "roots", "tasks", "budget", "deep", "usage")})
        dashboard["configured_projects"] = config["projects"]
        dashboard["local_exclude"] = config.get("local_exclude", [])
        dashboard["automatic_rules"] = config.get("automatic_rules", [])
        dashboard["trash_volumes"] = config.get("trash_preview", [])
        dashboard["history"] = actions.history(state)
        dashboard["history"].extend(macmanage.history(state))
        dashboard["history"].extend(config.get("trash_history", []))
        dashboard["history"].sort(key=lambda b:b.get("created_at",""), reverse=True)
        for batch in dashboard["history"]:
            batch.update(config.get("measurements", {}).get(batch["id"], {}))
        if completed_batches:
            dashboard["last_actions"] = completed_batches
        if duplicate:
            dashboard["duplicate"] = True
        if last is not None:
            dashboard["last_action"] = last
        return dashboard
