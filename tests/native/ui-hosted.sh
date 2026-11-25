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
import struct
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
gemini_project = home / "Projects/Gemini Project"
gemini_project.mkdir(parents=True)
(gemini_project / "scene.txt").write_text("Keep this source.")
gemini = home / ".gemini"
gemini.mkdir()
(gemini / "projects.json").write_text(json.dumps({"projects": {str(gemini_project): "gemini-project"}}))
continue_project = home / "Projects/Continue Project"
continue_project.mkdir()
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
kiro = home / ".kiro/sessions"
kiro.mkdir(parents=True)
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
