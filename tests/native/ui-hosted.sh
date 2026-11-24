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
