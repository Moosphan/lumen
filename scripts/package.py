#!/usr/bin/env python3
"""Build a development bundle or a notarized archive for direct distribution."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

REPO = Path(__file__).resolve().parent.parent


def notarize(app, profile, archive):
    with tempfile.TemporaryDirectory(dir=archive.parent, prefix=".notary-") as folder:
        stage = Path(folder)
        submission = stage / "submission.zip"
        zip_command = ["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app)]
        subprocess.run([*zip_command, str(submission)], check=True)
        reply = subprocess.run([
            "/usr/bin/xcrun", "notarytool", "submit", str(submission),
            "--keychain-profile", profile, "--wait", "--output-format", "json",
        ], check=True, stdout=subprocess.PIPE, text=True)
        result = json.loads(reply.stdout)
        if result.get("status") != "Accepted":
            raise ValueError(f"Apple 公证未通过（{result.get('status')}，提交 ID {result.get('id')}）。")

        subprocess.run(["/usr/bin/xcrun", "stapler", "staple", str(app)], check=True)
        subprocess.run(["/usr/bin/xcrun", "stapler", "validate", str(app)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        final_zip = stage / "release.zip"
        subprocess.run([*zip_command, str(final_zip)], check=True)
        # Publish only the stapled archive, without replacing an existing file.
        os.link(final_zip, archive)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO / "dist/Lumen.app")
    parser.add_argument("--sign-identity", default="-", help="Keychain signing identity for the app and its library")
    parser.add_argument("--notary-profile", help="Local notarytool keychain profile")
    parser.add_argument("--release-zip", type=Path, help="Output ZIP for GitHub Releases")
    args = parser.parse_args(argv)
    releasing = args.release_zip is not None
    if releasing != bool(args.notary_profile):
        parser.error("--release-zip 与 --notary-profile 必须配套使用。")
    if releasing and not args.sign_identity.startswith("Developer ID Application: "):
        parser.error("GitHub 分发包需要 Developer ID Application 签名证书。")

    dest = args.output.absolute()
    if dest.exists() or dest.is_symlink():
        parser.error("目标已存在。请选择新的 --output 路径。")
    archive = args.release_zip.absolute() if releasing else None
    if archive is not None:
        if archive.suffix.lower() != ".zip" or archive.exists() or archive.is_symlink():
            parser.error("请选择尚不存在的 .zip 作为 --release-zip。")
        identities = subprocess.run(
            ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
            stdout=subprocess.PIPE, text=True, check=True,
        )
        if f'"{args.sign_identity}"' not in identities.stdout:
            parser.error("钥匙串中未找到指定的 Developer ID Application 证书及私钥。")
        archive.parent.mkdir(exist_ok=True, parents=True)
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
            sign = ["/usr/bin/codesign", "--force", "--sign", args.sign_identity]
            sign += ["--timestamp", "--options", "runtime"] if releasing else ["--timestamp=none"]
            subprocess.run([*sign, str(zstd)], check=True)
            subprocess.run([*sign, str(app)], check=True)
            subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
            if archive is not None:
                notarize(app, args.notary_profile, archive)
            os.rename(app,dest)
    print(dest)
    if archive is not None:
        print(archive)


if __name__ == "__main__":
    main()
