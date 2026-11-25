#!/usr/bin/env python3
"""Local, one-shot JSON protocol used by the macOS app and isolated tests."""
import ctypes
import json
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lumen.service import dispatch


def native_caller():
    """Trust the OS-reported immediate executable, not caller-provided JSON or environment."""
    if sys.platform != "darwin":
        return False
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lookup = library.proc_pidpath
        lookup.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        lookup.restype = ctypes.c_int
        path = ctypes.create_string_buffer(4096)
        if lookup(os.getppid(), path, ctypes.sizeof(path)) <= 0:
            return False
        parent = Path(os.fsdecode(path.value)).resolve(strict=True)
        bridge = Path(__file__).resolve(strict=True)
        resources = bridge.parent.parent
        if (resources.name == "Resources" and resources.parent.name == "Contents"
                and resources.parent.parent.suffix == ".app"):
            return parent == resources.parent / "MacOS" / "Lumen"
        return any(parent == (resources / ".build" / profile / "Lumen").resolve()
                   for profile in ("debug", "release"))
    except (OSError, ValueError, RuntimeError, AttributeError):
        return False


def main():
    cancelled = False

    def cancel_recovery(_, __):
        nonlocal cancelled
        cancelled = True

    try:
        request = json.load(sys.stdin)
        if isinstance(request, dict) and (
                request.get("cmd") in ("mcp_pending", "mcp_decline", "mcp_approve",
                                       "model_pin", "model_recover", "model_forget", "model_recovery_options")
                or request.get("cmd") == "settings" and "mcp_enabled" in request):
            if not native_caller():
                raise ValueError("请在既白窗口管理草案或模型恢复")
        if isinstance(request, dict) and request.get("cmd") == "model_recover":
            signal.signal(signal.SIGTERM, cancel_recovery)
            signal.signal(signal.SIGINT, cancel_recovery)
        state = Path(os.environ.get("LUMEN_STATE_DIR") or
                     (Path.home() / "Library/Application Support/org.lumen.cleaner"))
        fixture_home = os.environ.get("LUMEN_INVENTORY_HOME") if os.environ.get("LUMEN_STATE_DIR") else None
        home = Path(fixture_home).resolve() if fixture_home else Path.home()
        result = {"ok": True, "data": dispatch(state, request, local_home=home,
                                                include_system=fixture_home is None,
                                                cancel=lambda: cancelled)}
    except (ValueError, RuntimeError, KeyError, OSError, TypeError) as error:
        result = {"ok": False, "error": str(error)}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
