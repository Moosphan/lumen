"""A repeatable small-file scan baseline. Run via test-isolated.sh --benchmark."""

from pathlib import Path
import resource
from statistics import median
import sys
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


def agent_inventory_benchmark():
    with TemporaryDirectory(prefix="lumen-agent-benchmark-") as directory:
        lab = Path(directory).resolve()
        home = lab / "home"
        projects = [home / "Documents/Codex" / f"project-{index:03d}" for index in range(100)]
        for project in projects:
            for group in range(10):
                folder = project / f"group-{group:02d}"
                folder.mkdir(parents=True)
                for item in range(100):
                    (folder / f"asset-{item:03d}.bin").touch()

        samples = []
        for run in range(3):
            started = monotonic()
            report = dispatch(lab / f"state-{run}", {"cmd": "scan_local", "startup": True}, local_home=home)
            samples.append(monotonic() - started)
            if len(report["files"]) != 100_000 or len(report["projects"]) != len(projects):
                raise RuntimeError("Incomplete agent inventory")
            del report
        print(f"startup: 100,000 files, {len(projects)} projects, median {median(samples):.3f}s")

        state = lab / "tree"
        root = dispatch(state, {"cmd": "size_tree"}, local_home=home)
        queries = []
        for project in projects:
            started = monotonic()
            level = dispatch(state, {"cmd": "size_tree", "path": str(project)}, local_home=home)
            queries.append((monotonic() - started) * 1000)
            if level["indexed_at"] != root["indexed_at"] or len(level["children"]) != 10:
                raise RuntimeError("Directory lookup did not reuse the inventory")
        print(f"directory index query: P95 {sorted(queries)[94]:.2f} ms")


if __name__ == "__main__":
    if sys.argv[1:] == ["--agent-inventory"]:
        agent_inventory_benchmark()
    else:
        main()
