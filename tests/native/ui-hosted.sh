#!/bin/bash
# Run after packaging on a GitHub-hosted macOS runner: tests/native/ui-hosted.sh dist/Lumen.app
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_ENVIRONMENT:-}" != github-hosted || "${RUNNER_OS:-}" != macOS || "$(uname -s)" != Darwin ]]; then
    echo "This test requires a GitHub-hosted macOS runner." >&2
    exit 2
fi
cd "$(dirname "$0")/../.."
app="$(cd "${1:-dist/Lumen.app}" && pwd -P)"
test -x "$app/Contents/MacOS/Lumen"
test -f "$app/Contents/Resources/engine/bridge.py"
/usr/bin/codesign --verify --strict "$app"
lab="$(mktemp -d "$RUNNER_TEMP/lumen-ui.XXXXXXXX")"
lab="$(cd "$lab" && pwd -P)"
pid=""
cleanup() {
    if [[ -n "$pid" ]]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi
    /usr/bin/python3 -B -c 'import shutil,sys;shutil.rmtree(sys.argv[1])' "$lab"
}
trap cleanup EXIT
mkdir -m 700 "$lab/tmp"
export LUMEN_STATE_DIR="$lab/workspace-state" TMPDIR="$lab/tmp"
export LUMEN_INVENTORY_HOME="$lab/home"
/usr/bin/python3 -B - <<'PY'
import os
import json
import hashlib
import plistlib
import sqlite3
import struct
from contextlib import closing
from pathlib import Path

home = Path(os.environ["LUMEN_INVENTORY_HOME"])
contents = home / "Applications/Sample Studio.app/Contents"
contents.mkdir(parents=True)
(contents / "Info.plist").write_bytes(plistlib.dumps({
    "CFBundleIdentifier": "org.lumen.sample-studio", "CFBundleName": "Sample Studio",
}))
chromium = home / "Applications/Chromium Fixture.app/Contents"
chromium.mkdir(parents=True)
(chromium / "Info.plist").write_bytes(plistlib.dumps({
    "CFBundleIdentifier": "org.chromium.Chromium", "CFBundleName": "Chromium Fixture",
}))
code_cache = home / "Library/Caches/Chromium/Default/Code Cache/js"
code_cache.mkdir(parents=True)
format_header = struct.pack("<QIIII", 0xfcfb6d1ba7725c30, 5, 3, 0, 0)
format_footer = struct.pack("<QIIII", 0xf4fa6f45970d41d8, 0, 0, 0, 0)
(code_cache / "ab1234567890cdef_0").write_bytes(format_header + b"key-fixture-code" + format_footer * 2)
(code_cache / "index").write_bytes(b"fixture index")
database = code_cache.parent / "pc" / "fixture.db"
database.parent.mkdir()
database.write_bytes(b"SQLite format 3\0fixture")
downloads = home / "Downloads"
downloads.mkdir()
(downloads / "archive.zip").write_bytes(b"sample" * 8192)
project = home / "Documents/Codex/Studio Project"
project.mkdir(parents=True)
(project / "notes.txt").write_text("Keep this.")
(project / "empty-marker").touch()
(project / "long-shoot-name-with-color-reference-and-export-notes-2026.txt").write_text("Small reference.")
(project / "render.mov").write_bytes(b"render" * 600_000)
gemini_project = home / "conductor/workspaces/lumen/Gemini Project"
gemini_project.mkdir(parents=True)
(gemini_project / "scene.txt").write_text("Keep this source.")
(gemini_project / ".git").write_text(f"gitdir: {home}/Projects/lumen/.git/worktrees/gemini\n")
(gemini_project / ".context").mkdir()
(gemini_project / ".context/notes.md").write_text("Keep this worktree handoff.")
gemini = home / ".gemini"
gemini.mkdir()
(gemini / "projects.json").write_text(json.dumps({"projects": {str(gemini_project): "gemini-project"}}))
continue_project = home / "Projects/Continue Project"
continue_project.mkdir(parents=True)
(continue_project / "notes.txt").write_text("Keep this local project.")
continue_sessions = home / ".continue/sessions"
continue_sessions.mkdir(parents=True)
continue_id = "5492b813-1f5f-42fa-8013-aa3e5584fd4c"
(continue_sessions / f"{continue_id}.json").write_text("fixture session bytes")
(continue_sessions / "sessions.json").write_text(json.dumps([
    {"sessionId": continue_id, "workspaceDirectory": continue_project.as_uri()},
]))
aider_project = home / "Projects/Aider Project"
aider_project.mkdir()
(aider_project / "notes.txt").write_text("Keep this Aider project source.")
(aider_project / ".aider.chat.history.md").write_text("\n# aider chat started at 2026-11-25 12:00:00\n\nSynthetic session.")
(aider_project / ".amp").mkdir()
(aider_project / ".amp/settings.jsonc").write_text('{ // workspace config\n "amp.showCosts": true,\n}')
amp_threads = home / ".local/share/amp/threads"
amp_threads.mkdir(parents=True)
amp_id = "T-0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
(amp_threads / f"{amp_id}.json").write_text(json.dumps({
    "id": amp_id, "v": 1, "created": 1768178184664,
    "env": {"initial": {"platform": {"client": "CLI"}, "trees": [{"uri": project.as_uri()}]}},
    "messages": [{"role": "user", "content": "Synthetic prompt"}],
}))
harness = home / "Applications/DeepSeek Harness.app/Contents"
harness.mkdir(parents=True)
(harness / "Info.plist").write_bytes(plistlib.dumps({
    "CFBundleIdentifier": "com.deepseek.dsh", "CFBundleName": "DeepSeek Harness",
    "CFBundleShortVersionString": "0.1.0",
}))
harness_project = home / "Projects/Harness Project"
harness_project.mkdir()
(harness_project / "scene.txt").write_bytes(b"scene" * 8192)
registry = home / ".dsh/storages"
registry.mkdir(parents=True)
harness_id = "49fb9c05-a1c4-419e-9254-a6379f88240a"
(registry / "workspace.json").write_text(json.dumps({
    "unit": {"name": "workspace", "version": 2},
    "global": {"initialized": True, "workspaceIds": [harness_id]},
    "tables": {"workspaces": {harness_id: {"path": str(harness_project)}}},
}))
cli_project = home / "Projects/Harness CLI Project"
cli_project.mkdir()
(cli_project / "scene.txt").write_bytes(b"cli scene" * 8192)
cli_id = "session-fixture-headless"
bucket = "--" + str(cli_project).replace("/", "-").replace(" ", "~0020").lstrip("-") + "--"
cli_session = home / ".dsh/sessions" / bucket / cli_id
cli_session.mkdir(parents=True)
header = (json.dumps({
    "type": "session", "version": 4, "id": cli_id, "cwd": str(cli_project),
    "createdAt": 1795610528154, "isSeeded": False, "delegationDepth": 0,
}) + "\n").encode()
frame = bytes.fromhex("28b52ffda0") + struct.pack("<I", len(header)) + (len(header) * 8 + 1).to_bytes(3, "little")
(cli_session / "session.v4.jsonl.zstd").write_bytes(frame + header + b"unread fixture body")
pi_project = home / "Projects/Pi Project"
pi_project.mkdir()
(pi_project / "edit.txt").write_bytes(b"draft" * 4096)
pi_sessions = home / ".pi/agent/sessions/--fixture--"
pi_sessions.mkdir(parents=True)
(pi_sessions / "example.jsonl").write_text(json.dumps({
    "type": "session", "version": 3, "id": "fixture-session", "cwd": str(pi_project),
}) + "\n")
devin = home / "Applications/Devin.app/Contents"
devin.mkdir(parents=True)
(devin / "Info.plist").write_bytes(plistlib.dumps({
    "CFBundleIdentifier": "com.exafunction.windsurf", "CFBundleName": "Devin",
    "CFBundleShortVersionString": "3.14.30833",
}))
windsurf_state = home / "Library/Application Support/Devin/User/workspaceStorage/fixture"
windsurf_state.mkdir(parents=True)
windsurf_manifest = home / "Documents/Editing.code-workspace"
windsurf_manifest.write_text('''{
    // A saved workspace can include projects already associated with other tools.
    "folders": [
        {"path": "../conductor/workspaces/lumen/Gemini Project"},
    ],
}''')
(windsurf_state / "workspace.json").write_text(json.dumps({"workspace": windsurf_manifest.as_uri()}))
(windsurf_state / "state.vscdb").write_bytes(b"protected editor state")
windsurf_untitled = windsurf_state.parents[2] / "Workspaces/1795610510123/workspace.json"
windsurf_untitled.parent.mkdir(parents=True)
windsurf_untitled.write_text(json.dumps({"folders": [{"path": str(project)}, {"path": str(gemini_project)}]}))
kiro = home / ".kiro/sessions"
kiro_cli = kiro / "cli"
kiro_cli.mkdir(parents=True)
(kiro_cli / "fixture.json").write_text(json.dumps({"cwd": str(continue_project), "session_state": {}}))
(kiro_cli / "fixture.jsonl").write_text("Protected legacy transcript.")
kiro_kas = kiro / "0123456789abcdef/sess_fixture"
kiro_kas.mkdir(parents=True)
(kiro_kas / "session.json").write_text(json.dumps({
    "id": "sess_fixture", "workspacePaths": [str(continue_project)],
}))
trae_bundle = home / "Applications/Trae.app/Contents"
trae_bundle.mkdir(parents=True)
(trae_bundle / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.trae.app"}))
trae_data = home / "Library/Application Support/Trae CN"
trae_workspace = trae_data / "User/workspaceStorage/fixture"
trae_workspace.mkdir(parents=True)
(trae_workspace / "workspace.json").write_text(json.dumps({"folder": aider_project.as_uri()}))
trae_snapshot = trae_data / "ModularData/ai-agent/snapshot/editor-changes.tmp"
trae_snapshot.parent.mkdir(parents=True)
trae_snapshot.write_text("Keep the editor snapshot.")
traework_bundle = home / "Applications/TRAE SOLO CN.app/Contents"
traework_bundle.mkdir(parents=True)
(traework_bundle / "Info.plist").write_bytes(plistlib.dumps({
    "CFBundleIdentifier": "cn.trae.solo.app", "CFBundleName": "TRAE SOLO CN",
}))
traework = home / "Library/Application Support/TRAE SOLO CN"
work_project = traework / "ModularData/ai-agent/work-mode-projects/6a4db2ef0f25b4742bf9cdc2"
work_project.mkdir(parents=True)
(work_project / "delivery.html").write_text("Protected delivery.")
work_runtime = traework / "ModularData/ai-agent/vm/vms/fixture.img"
work_runtime.parent.mkdir(parents=True)
work_runtime.write_bytes(b"Protected local runtime fixture.")
work_storage = traework / "User/workspaceStorage/fixture"
work_storage.mkdir(parents=True)
(work_storage / "workspace.json").write_text(json.dumps({"folder": continue_project.as_uri()}))
migrated_id = "6a4db2ef0f25b4742bf9cdc8"
work_relative = Path("ModularData/ai-agent/work-mode-projects") / migrated_id
migrated_project = trae_data / work_relative
migrated_project.mkdir(parents=True)
(migrated_project / "migrated-delivery.html").write_text("Protected migrated delivery.")
old_stub = traework / work_relative
old_stub.mkdir()
(old_stub / f"TRAE_PROJECT_MOVED_{migrated_id}.md").write_text(
    f"This project was migrated to:\n\n{migrated_project}\n\n"
    "Use the target directory above. This directory only records migration state.\n"
)
kilo_project = home / "Documents/Kilo Project"
kilo_project.mkdir(parents=True)
(kilo_project / "source.css").write_text("/* synthetic project */")
kilo_db = home / ".local/share/kilo/kilo.db"
kilo_db.parent.mkdir(parents=True)
with closing(sqlite3.connect(kilo_db)) as db:
    db.execute("CREATE TABLE session (project_id TEXT, directory TEXT, workspace_id TEXT)")
    db.execute("INSERT INTO session VALUES (?, ?, ?)", ("global", str(kilo_project), None))
    db.commit()
kilo_extension = home / ".vscode/extensions/kilocode.kilo-code-7.8.32"
kilo_extension.mkdir(parents=True, exist_ok=True)
(kilo_extension / "package.json").write_text(json.dumps({
    "publisher": "kilocode", "name": "kilo-code", "version": "7.8.32",
}))
crush_project = home / "Documents/Crush Project"
crush_database = crush_project / ".crush/crush.db"
crush_database.parent.mkdir(parents=True)
with closing(sqlite3.connect(crush_database)) as db:
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    db.commit()
(crush_project / "source.ts").write_text("/* synthetic project */")
trae_cli = home / ".trae/cli"
for index, (relative, workspace) in enumerate([
    ("sessions/2026/11/26", aider_project), ("archived_sessions", continue_project),
]):
    session_id = f"019fbcca-9fd4-7d20-83dc-0762b2f839b{index}"
    rollout = trae_cli / relative / f"rollout-2026-11-26T11-22-33-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    metadata_line = (json.dumps({"type": "session_meta", "payload": {
        "id": session_id, "cwd": str(workspace),
    }}) + "\n").encode()
    if relative == "archived_sessions":
        blocks = [metadata_line, *([b"keep archived transcript " * 4000] * 4)]
        frame = bytes.fromhex("28b52ffda0") + struct.pack("<I", sum(map(len, blocks)))
        for position, block in enumerate(blocks):
            frame += (len(block) * 8 + (position == len(blocks) - 1)).to_bytes(3, "little") + block
        rollout.with_suffix(".jsonl.zst").write_bytes(frame)
    else:
        rollout.write_bytes(metadata_line)
(trae_cli / "history.jsonl").write_text("Keep the CLI history.")
with closing(sqlite3.connect(trae_cli / "state_5.sqlite")) as database:
    database.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT NOT NULL, rollout_path TEXT NOT NULL, "
        "source TEXT NOT NULL, updated_at INTEGER NOT NULL, title TEXT NOT NULL)"
    )
    database.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", (
        "019fbcca-9fd4-7d20-83dc-0762b2f839b2", str(pi_project), "", "cli", 1, "Private fixture title",
    ))
    database.commit()
(trae_cli / "thread_history_1.sqlite").write_bytes(b"Opaque fixture history.")
coco = home / "Library/Caches/coco"
legacy_id = "019fbcca-9fd4-7d20-83dc-0762b2f839b5"
legacy_session = coco / "sessions" / legacy_id
legacy_session.mkdir(parents=True)
(legacy_session / "session.json").write_text(json.dumps({
    "id": legacy_id, "metadata": {"cwd": str(gemini_project)},
}))
(legacy_session / "events.jsonl").write_text("Private CoCo session body.")
(coco / "history.jsonl").write_text("Private CoCo prompt history.")
models = home / ".lmstudio/models/Lumen/Sample Weight"
models.mkdir(parents=True)
(models / "sample-Q4_K_M.gguf").write_bytes(b"synthetic model metadata")
hub = home / ".cache/huggingface/hub/models--fixture--managed"
weight = b"isolated fictional model content"
blob = hub / "blobs" / hashlib.sha256(weight).hexdigest()
blob.parent.mkdir(parents=True)
blob.write_bytes(weight)
snapshot = hub / "snapshots" / ("a" * 40) / "example.safetensors"
snapshot.parent.mkdir(parents=True)
snapshot.symlink_to(os.path.relpath(blob, snapshot.parent))
for folder, name, size in [
    (".cache", "tool-cache.bin", 2_000_000),
    ("Desktop", "capture.png", 900_000),
    ("Downloads", "recording.mov", 1_360_000),
    ("Pictures", "photo-library.bin", 1_100_000),
    ("Movies", "studio-preview.mov", 1_850_000),
    (".pub-cache", "packages.bin", 1_600_000),
    (".gradle", "dependency-cache.bin", 760_000),
    ("Music", "audio-library.bin", 480_000),
    ("Library/Containers/org.lumen.sample-studio/Data/Library/Caches", "preview-cache.bin", 992_000),
    ("Library/Containers/org.lumen.sample-studio/Data/tmp", "render-preview.tmp", 560_000),
    ("Library/Caches/org.lumen.sample-studio", "renderer.bin", 752_000),
]:
    path = home / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
PY
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
/usr/bin/xcrun swiftc -parse-as-library -module-cache-path "$lab/modules" \
    tests/native/ui-hosted.swift -o "$lab/check-window"
(
    cd "$lab"
    exec "$app/Contents/MacOS/Lumen"
) >"$lab/application.log" 2>&1 &
pid=$!
if ! "$lab/check-window" "$pid" "$app" "$lab" >"$lab/result" 2>&1; then
    cat "$lab/result" >&2
    tail -n 25 "$lab/application.log" >&2
    exit 1
fi
cat "$lab/result"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    cat "$lab/result" >> "$GITHUB_STEP_SUMMARY"
fi

if [[ -f "$lab/Lumen-overview.png" && -n "${GITHUB_OUTPUT:-}" ]]; then
    preview_dir="$(mktemp -d "$RUNNER_TEMP/lumen-preview.XXXXXXXX")"
    for screenshot in "$lab"/Lumen-*.png; do
        /usr/bin/install -m 600 "$screenshot" "$preview_dir/$(basename "$screenshot")"
    done
    echo "preview=$preview_dir" >> "$GITHUB_OUTPUT"
fi
