#!/usr/bin/env python3
"""Build a local development bundle; all intermediate build output is temporary."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

REPO = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO / "dist/Lumen.app")
    args = parser.parse_args()
    dest = args.output.absolute()
    if dest.exists() or dest.is_symlink():
        parser.error("目标已存在。请选择新的 --output 路径。")
    dest.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(prefix="lumen-build-") as temp:
        scratch = Path(temp)
        env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(scratch / "modules"),
                   SWIFTPM_MODULECACHE_OVERRIDE=str(scratch / "modules"))
        flags = ["swift", "build", "--package-path", str(REPO), "-c", "release",
                 "--scratch-path", str(scratch/"build"), "--cache-path", str(scratch/"cache"),
                 "--config-path", str(scratch/"config"), "--security-path", str(scratch/"security"),
                 "--manifest-cache", "none"]
        subprocess.run(flags, env=env, check=True)
        bin_path = Path(subprocess.check_output(flags + ["--show-bin-path"], env=env, text=True).strip())
        with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".package-") as stage:
            app = Path(stage) / "Lumen.app"
            content = app / "Contents"
            exe = content / "MacOS"
            res = content / "Resources"
            frameworks = content / "Frameworks"
            exe.mkdir(parents=True)
            res.mkdir()
            frameworks.mkdir()
            shutil.copy2(bin_path / "Lumen", exe / "Lumen")
            shutil.copytree(REPO/"engine", res/"engine", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(REPO/"设计稿/assets/storage.jpg", res/"storage.jpg")
            shutil.copy2(REPO/"assets/Lumen.icns", res/"Lumen.icns")
            zstd = frameworks / "liblumen-zstd.dylib"
            subprocess.run(["/usr/bin/xcrun", "clang", "-dynamiclib", "-Oz", "-mmacosx-version-min=13.0",
                            "-Wl,-install_name,@rpath/liblumen-zstd.dylib",
                            "-Wl,-exported_symbol,_ZSTD_findFrameCompressedSize",
                            "-Wl,-exported_symbol,_ZSTD_decompress",
                            "-Wl,-exported_symbol,_ZSTD_createDCtx",
                            "-Wl,-exported_symbol,_ZSTD_freeDCtx",
                            "-Wl,-exported_symbol,_ZSTD_DCtx_setParameter",
                            "-Wl,-exported_symbol,_ZSTD_decompressStream",
                            "-Wl,-exported_symbol,_ZSTD_isError",
                            str(REPO / "vendor/zstd/zstddeclib.c"), "-o", str(zstd)], env=env, check=True)
            shutil.copy2(REPO / "vendor/zstd/LICENSE", res / "Zstandard-LICENSE.txt")
            (content/"Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier":"org.lumen.cleaner", "CFBundleExecutable":"Lumen",
                "CFBundleName":"Lumen", "CFBundleDisplayName":"既白 Lumen",
                "CFBundleShortVersionString":"0.1.0", "CFBundleVersion":"1",
                "CFBundlePackageType":"APPL", "CFBundleIconFile":"Lumen.icns", "LSMinimumSystemVersion":"13.0",
                "NSHighResolutionCapable":True, "NSPrincipalClass":"NSApplication"}))
            subprocess.run(["codesign","--force","--sign","-",str(zstd)],check=True)
            subprocess.run(["codesign","--force","--sign","-",str(app)],check=True)
            subprocess.run(["codesign","--verify","--deep","--strict",str(app)],check=True)
            os.rename(app,dest)
    print(dest)


if __name__ == "__main__":
    main()
