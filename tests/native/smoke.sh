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
(deny file-read-data (literal "/Applications") (literal "/System/Applications"))
(deny file-read-data (subpath "/Library/Application Support/com.apple.TCC"))
PROFILE
sandbox=(/usr/bin/sandbox-exec -D "LAB=$lab" -D "REPO=$(pwd -P)" -D "USERDIR=$HOME" -f "$lab/profile.sb")
"${sandbox[@]}" "$lab/smoke"

# Match the packaged bridge's native parent check with the same smoke executable.
bundle="$lab/PermissionSmoke.app/Contents"
mkdir -p "$bundle/MacOS" "$bundle/Resources"
cp "$lab/smoke" "$bundle/MacOS/Lumen"
cp -R engine "$bundle/Resources/engine"
cat > "$bundle/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>Lumen</string>
  <key>CFBundleIdentifier</key><string>org.lumen.permission-smoke</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
PLIST
LUMEN_STATE_DIR="$lab/permission-state" "${sandbox[@]}" "$bundle/MacOS/Lumen" --verify-disk-access-gate
mkdir "$lab/permission-home"
LUMEN_STATE_DIR="$lab/recovery-state" "${sandbox[@]}" /usr/bin/env HOME="$lab/permission-home" \
    "$bundle/MacOS/Lumen" --verify-disk-access-recovery
