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
import plistlib
from pathlib import Path

home = Path(os.environ["LUMEN_INVENTORY_HOME"])
contents = home / "Applications/Sample Studio.app/Contents"
contents.mkdir(parents=True)
(contents / "Info.plist").write_bytes(plistlib.dumps({
    "CFBundleIdentifier": "org.lumen.sample-studio", "CFBundleName": "Sample Studio",
}))
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
