#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
lab="$(mktemp -d -t lumen-store)"
lab="$(cd "$lab" && pwd -P)"
trap '/usr/bin/python3 -B -c "import shutil,sys;shutil.rmtree(sys.argv[1])" "$lab"' EXIT
mkdir -p "$lab/tmp"
export TMPDIR="$lab/tmp" LUMEN_STATE_DIR="$lab/state"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
/usr/bin/xcrun swiftc -module-cache-path "$lab/modules" \
  Sources/Lumen/Store.swift Sources/Lumen/Ranking.swift tests/native/StoreSmoke.swift -o "$lab/smoke"
cat > "$lab/profile.sb" <<'PROFILE'
(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath (param "LAB")) (literal "/dev/null"))
(deny file-read-data (subpath (param "USERDIR")))
(allow file-read-data (subpath (param "REPO")) (subpath (param "LAB")))
PROFILE
/usr/bin/sandbox-exec -D "LAB=$lab" -D "REPO=$(pwd -P)" -D "USERDIR=$HOME" \
  -f "$lab/profile.sb" "$lab/smoke"
