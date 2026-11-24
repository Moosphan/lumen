"""Authorize scopes, persist local state and revalidate previews before actions."""
import fcntl
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from . import actions, macmanage
from .fixture import create_fixture
from .scan import scan


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
            json.dump(config, stream, ensure_ascii=False)
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
    previous = config.get("root_ids", {})
    config["root_ids"] = {r: (previous.get(r) if r in config["roots"] and r not in renew else root_identity(r)) for r in new}
    config["roots"] = new
    # Revocation also removes any cached asset paths and scope-specific settings.
    allowed = lambda p: any(inside(Path(p), Path(r)) for r in new)
    for key in ("keep", "exclude"):
        config[key] = [p for p in config[key] if allowed(p)]
    config["tasks"] = [task for task in config["tasks"] if allowed(task["root"])]
    for t in config["tasks"]:
        for field in ("inputs", "outputs", "temporary"):
            t[field] = [p for p in t[field] if allowed(p)]
    config["projects"] = {p: a for p, a in config["projects"].items() if allowed(p)}
    config["usage"] = {p: v for p, v in config["usage"].items() if allowed(p)}
    config["report"] = {}
    config.pop("trash_preview", None)
    config.pop("hash_cache", None)


def apply_settings(config, request, state):
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
        if not isinstance(rules, list) or any(r not in ("fixture.rotated-log.v1", "vscode.1.95.3.main-log.v1") for r in rules):
            raise ValueError("未知的自动清理规则")
        config["automatic_rules"] = sorted(set(rules))
    if "budget" in request:
        budget = int(request["budget"])
        if budget < 0:
            raise ValueError("预算不能为负")
        config["budget"] = budget
    if "snooze_until" in request:
        config["snooze_until"] = int(request["snooze_until"])


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
    config["tasks"] = [t for t in config["tasks"] if t["id"] != task["id"]] + [task]
    agents = config["projects"].setdefault(root, [])
    if agent not in agents:
        agents.append(agent)


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


def inventory(config, state, incremental=False, full=False):
    protected = set(config["keep"])
    for task in config["tasks"]:
        protected.update(task["inputs"] + task["outputs"])
        if not task["closed"]:
            protected.update(task["temporary"])
    exclusions = config["exclude"] + [str(state)]
    exclusions.extend(str(Path(r) / name) for r in config["roots"]
                      for name in (".Trash", f".lumen-vault-{os.getuid()}"))
    report = scan(active_roots(config), deep=full or config["deep"], projects=config["projects"],
                  keep=sorted(protected), exclude=exclusions,
                  hash_cache=config.setdefault("hash_cache", {}) if incremental else None)
    for lost in sorted(set(config["roots"])-set(active_roots(config))):
        report["issues"].append(lost + " · 授权目录离线或身份变化，请重新选择")
    for app in report["apps"]:
        if app["bundle_id"] in macmanage.SUPPORTED_BUNDLES and not app_protected(config, app, report):
            try:
                app["uninstall_snapshot"] = macmanage.app_snapshot(app["path"], fixture_roots=[t.parent for t in fixture_trash(config)])
            except (OSError, ValueError) as error:
                app["uninstall_reason"] = str(error)
    report["residuals"] = macmanage.residuals(state, config["roots"], report,
                                              fixture_roots=[t.parent for t in fixture_trash(config)])
    remnants = {item["path"]: item for item in report.get("residuals", [])}
    verified_apps = {app["path"] for app in report["apps"] if app.get("uninstall_snapshot")}
    task_paths = {}
    for task in config["tasks"]:
        for field in ("inputs", "outputs", "temporary"):
            for target in task[field]:
                task_paths.setdefault(target, []).append(task)
    lab_roots = [t.parent for t in fixture_trash(config)]
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
        if any(inside(Path(row["path"]), Path(p)) for p in protected):
            row.update(decision="keep", reason="用户标记保留或任务正在引用")
    return report


def refresh(config, state):
    report = inventory(config, state, incremental=True)
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


def perform(config, state, request):
    requested = paths(request.get("paths", []))
    action = request.get("action")
    if not requested or action not in ("quarantine", "clean", "trash"):
        raise ValueError("请选择文件和有效操作")
    preview = {r["path"]: r for r in config["report"].get("files", [])}
    fresh = inventory(config, state, full=True)
    current = {r["path"]: r for r in fresh["files"]}
    selected, skipped = [], []
    chosen = set(requested)
    for path in requested:
        old, row = preview.get(path), current.get(path)
        fields = ("dev", "ino", "size", "mtime_ns", "ctime_ns", "decision", "rule_id")
        if not old or not row or any(old.get(f) != row.get(f) for f in fields):
            skipped.append({"path": path, "status": "skipped", "reason": "预演已过期，请重新扫描"})
            continue
        group = row.get("duplicate_group")
        copies = fresh.get("duplicates", {}).get(group, []) if group else []
        if copies and all(p in chosen for p in copies):
            skipped.append({"path": path, "status": "skipped", "reason": "重复组必须保留至少一份"})
            continue
        selected.append(row)
    if selected and action == "trash":
        result = actions.execute(state, active_roots(config), selected, action,
                                 trash_override=lambda p: trash_destination(config, p))
    else:
        result = actions.execute(state, active_roots(config), selected, action) if selected else {"items": []}
    result.setdefault("items", []).extend(skipped)
    return result


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


def followup(module, command, state, config, request):
    if command == "restore" and request.get("beside") is True:
        return module.restore(state, active_roots(config), request["batch_id"], beside=True)
    return getattr(module, command)(state, active_roots(config), request["batch_id"])


def require_preview(config, request):
    expected = config["report"].get("scan_id")
    if not expected or request.get("scan_id") != expected:
        raise ValueError("当前清单已改变，请重新扫描并确认")


def dispatch(state, request):
    state = Path(state).expanduser().absolute()
    if not isinstance(request, dict):
        raise ValueError("需要 JSON 对象")
    cmd = request.get("cmd")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise ValueError("状态目录必须为当前用户所有，仅本人可读写")
    with (state / ".service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = load_config(state)
        last = None
        if cmd == "fixture":
            root = Path(create_fixture())
            (root / ".Trash").mkdir(mode=0o700)
            (root / ".Trash" / "old-fixture.txt").write_text("synthetic trash")
            st = root.stat()
            config.setdefault("labs", {})[str(root)] = [st.st_dev, st.st_ino]
            save_config(state, config)
            return {"root": str(root)}
        before_space = volume_free(config["roots"]) if cmd in ("run","purge","restore","empty_trash","uninstall") else {}
        if cmd in ("run", "uninstall", "empty_trash"):
            require_preview(config, request)
        if cmd == "scan":
            if "roots" in request and (request["roots"] != config["roots"] or request.get("renew_roots")):
                assign_roots(config, request["roots"], state, request.get("renew_roots", ()))
            config["deep"] = bool(request.get("deep", config["deep"]))
        elif cmd == "settings":
            apply_settings(config, request, state)
        elif cmd == "task":
            remember_task(config, request)
        elif cmd == "run":
            last = perform(config, state, request)
        elif cmd == "session_end":
            from .integrations import claude_session_end, automatic_paths
            task = claude_session_end(request["event"], active_roots(config))
            prior = next((t for t in config["tasks"] if t["id"] == task["id"]), None)
            if prior:
                if (prior["root"], prior["agent"]) != (task["root"], task["agent"]):
                    raise ValueError("会话标识对应其他工程")
                if prior["closed"]:
                    return {"duplicate": True, "task_id": task["id"]}
                task.update({k: prior[k] for k in ("inputs", "outputs", "temporary")})
            remember_task(config, task)
            report = refresh(config, state)
            choices = automatic_paths(report, config.get("automatic_rules", []))
            scoped = [p for p in choices if inside(Path(p), Path(task["root"]))]
            if scoped:
                last = perform(config, state, {"paths":scoped, "action":"clean"})
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
        if last is not None:
            after_space = volume_free(config["roots"])
            last["measured_free_delta"] = sum(after_space[k] - v for k, v in before_space.items() if k in after_space)
            last["logical_processed_bytes"] = sum(i.get("size", 0) for i in last["items"]
                                                  if i["status"] not in ("skipped", "pending"))
            if cmd == "empty_trash":
                import datetime
                last.update(id="trash-" + uuid.uuid4().hex, action="empty_trash",
                            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                config.setdefault("trash_history", []).insert(0, last)
            batch = last.get("batch_id")
            if batch:
                config.setdefault("measurements", {})[batch] = {k:last[k] for k in ("logical_processed_bytes","measured_free_delta")}
        if cmd != "status":
            refresh(config, state)
            save_config(state, config)
        dashboard = dict(config.get("report", {}))
        for key, default in (("files", []), ("issues", []), ("projects", {}), ("apps", []), ("duplicates", {})):
            dashboard.setdefault(key, default)
        dashboard.update({k: config[k] for k in ("keep", "exclude", "roots", "tasks", "budget", "deep", "usage")})
        dashboard["configured_projects"] = config["projects"]
        dashboard["automatic_rules"] = config.get("automatic_rules", [])
        dashboard["trash_volumes"] = config.get("trash_preview", [])
        dashboard["history"] = actions.history(state)
        dashboard["history"].extend(macmanage.history(state))
        dashboard["history"].extend(config.get("trash_history", []))
        dashboard["history"].sort(key=lambda b:b.get("created_at",""), reverse=True)
        for batch in dashboard["history"]:
            batch.update(config.get("measurements", {}).get(batch["id"], {}))
        if last is not None:
            dashboard["last_action"] = last
        return dashboard
