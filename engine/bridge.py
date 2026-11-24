#!/usr/bin/env python3
"""Local, one-shot JSON protocol used by the macOS app and isolated tests."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lumen.service import dispatch


def main():
    try:
        request = json.load(sys.stdin)
        state = Path(os.environ.get("LUMEN_STATE_DIR") or
                     (Path.home() / "Library/Application Support/org.lumen.cleaner"))
        result = {"ok": True, "data": dispatch(state, request)}
    except (ValueError, RuntimeError, KeyError, OSError, TypeError) as error:
        result = {"ok": False, "error": str(error)}
    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
