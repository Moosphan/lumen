"""A repeatable small-file scan baseline. Run via test-isolated.sh --benchmark."""

from pathlib import Path
import resource
from tempfile import TemporaryDirectory
from time import monotonic

from engine.lumen.scan import scan


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


if __name__ == "__main__":
    main()
