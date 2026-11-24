#!/usr/bin/env python3
"""Optional Claude Code SessionEnd hook. The user installs this explicitly."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lumen.service import dispatch


def main():
    event = json.load(sys.stdin)
    state = Path(os.environ.get("LUMEN_STATE_DIR") or Path.home() / "Library/Application Support/org.lumen.cleaner")
    result = dispatch(state, {"cmd":"session_end", "event":event})
    print(json.dumps({"recorded":True, "duplicate":result.get("duplicate", False)}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
