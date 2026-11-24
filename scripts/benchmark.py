"""A repeatable small-file scan baseline. Run via test-isolated.sh --benchmark."""

from pathlib import Path
import resource
from tempfile import TemporaryDirectory
from time import monotonic

from engine.lumen.scan import scan
from engine.lumen.service import dispatch


def measure(root, name, deep, cache):
    started = monotonic()
    report = scan([root], deep=deep, hash_cache=cache)
    elapsed = monotonic() - started
    rows = report["files"]
    if len(rows) != 10_000:
        raise RuntimeError("Incomplete inventory")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    print(f"{name}: {len(rows)} files, {elapsed:.2f}s, {rss:.1f} MiB process high-water RSS, "
          f"{report['hash_reads']} hash reads, {report['hash_hits']} hash hits")


def main():
    with TemporaryDirectory(prefix="lumen-benchmark-") as directory:
        root = Path(directory)
        for group in range(100):
            folder = root / f"set-{group:03}"
            folder.mkdir()
            for item in range(100):
                # Every group repeats identical 64-byte samples in independent files.
                (folder / f"asset-{item:03}.bin").write_bytes(bytes([item]) * 64)
        cache = {}
        measure(root, "quick", False, cache)
        measure(root, "deep-first", True, cache)
        measure(root, "deep-repeat", True, cache)
    with TemporaryDirectory(prefix="lumen-startup-benchmark-") as directory:
        root = Path(directory).resolve()
        home = root / "home"
        for group in range(72):
            project = home / "Documents/Codex" / f"Project-{group}"
            for name in ("src", "build", "media", "node_modules"):
                folder = project / name
                folder.mkdir(parents=True)
                for item in range(64):
                    (folder / f"{item:03d}.bin").write_bytes(bytes([item]) * 128)
        started = monotonic()
        report = dispatch(root / "state", {"cmd": "scan_local", "startup": True}, local_home=home)
        if len(report["files"]) != 18_432 or len(report["projects"]) != 72:
            raise RuntimeError("Incomplete automatic inventory")
        print(f"startup: 18,432 files, 72 projects, {monotonic() - started:.2f}s")


if __name__ == "__main__":
    main()
