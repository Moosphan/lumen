#!/bin/bash
# macOS kernel-enforced filesystem write boundary for engine and child tools.
set -euo pipefail
cd "$(dirname "$0")/.."
lab="$(mktemp -d "${TMPDIR:-/tmp}/lumen-verify.XXXXXXXX")"
lab="$(cd "$lab" && pwd -P)"
trap 'rmdir "$lab" 2>/dev/null || true' EXIT
mkdir -p "$lab/tmp"
export TMPDIR="$lab/tmp"
export PYTHONDONTWRITEBYTECODE=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
profile="$lab/verify.sb"
cat > "$profile" <<'PROFILE'
(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath (param "LAB")) (literal "/dev/null"))
(deny file-read-data (subpath (param "USERDIR")))
(allow file-read-data (subpath (param "REPO")) (subpath (param "LAB")))
PROFILE
sandbox=(/usr/bin/sandbox-exec -D "LAB=$lab" -D "REPO=$(pwd -P)" -D "USERDIR=$HOME" -f "$profile")
# Verify confinement without attempting to modify any real user document.
guard="$(mktemp "${TMPDIR%/tmp}/../lumen-guard.XXXXXXXX")"
trap 'rm -f "$guard"; /usr/bin/python3 -B -c "import shutil,sys;shutil.rmtree(sys.argv[1])" "$lab"' EXIT
"${sandbox[@]}" /usr/bin/python3 -B -c '
import sys
try:
    open(sys.argv[1], "ab").close()
except PermissionError:
    print("Write confinement verified")
else:
    raise SystemExit("sandbox failed to deny outside write")' "$guard"
case "${1:-}" in
    "") "${sandbox[@]}" /usr/bin/python3 -B -m unittest discover -s tests -v ;;
    --benchmark) "${sandbox[@]}" /usr/bin/python3 -B -m scripts.benchmark ;;
    --benchmark-agent-inventory) "${sandbox[@]}" /usr/bin/python3 -B -m scripts.benchmark --agent-inventory ;;
    *) echo "Usage: $0 [--benchmark | --benchmark-agent-inventory]" >&2; exit 2 ;;
esac
