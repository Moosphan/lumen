"""Create a synthetic user workspace with no dependencies on a real home."""
import json
import os
import plistlib
import tempfile
import time
from pathlib import Path


def create_fixture():
    root = Path(tempfile.mkdtemp(prefix="Lumen-Lab-")).resolve()

    def write(relative, value):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value if isinstance(value, bytes) else value.encode())
        return target

    write("Applications/Lumen Studio.app/Contents/Info.plist", plistlib.dumps({
        "CFBundleIdentifier": "com.lumen.fixture", "CFBundleName": "Lumen Studio",
        "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1.0",
        "CFBundlePackageType": "APPL"}))
    write("Applications/Lumen Studio.app/Contents/Resources/theme.dat", b"Z" * 4096)
    old_log = write("Library/Logs/com.lumen.fixture/expired-1.log", b"L" * 65536)
    expired = time.time() - 40 * 86400
    os.utime(old_log, (expired, expired))
    write("Library/Application Support/com.lumen.fixture/session.db", "private offline workspace")
    song = write("Projects/Trailer/media/theme.wav", b"WAVE" * 2048)
    write("Projects/Trailer/index.html", '<audio src="media/theme.wav"></audio>')
    write("Projects/Website/index.html", '<audio src="../Trailer/media/theme.wav"></audio>')
    write("Projects/Trailer/preview/rough.mp4", b"draft video")
    write("Downloads/theme-backup.wav", song.read_bytes())
    write("Downloads/draft-a.zip", b"archive fixture" * 97)
    write("Downloads/draft-b.zip", b"archive fixture" * 97)
    write(".cache/huggingface/hub/models--example--tiny/snapshots/v1/weights.safetensors", b"fixture")
    for agent, project in [("codex", "Trailer"), ("claude", "Trailer"), ("claude", "Website")]:
        cwd = str(root / "Projects" / project)
        if agent == "codex":
            name = ".codex/sessions/2026/11/24/rollout-sample.jsonl"
            data = {"type": "session_meta", "payload": {"cwd": cwd}}
        else:
            name = f".claude/projects/lumen-{project}/trace.jsonl"
            data = {"type": "user", "cwd": cwd}
        write(name, json.dumps(data) + "\n")
    return str(root)
