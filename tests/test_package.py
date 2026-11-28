import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from scripts import package


class PackageSigningTests(unittest.TestCase):
    def test_release_rejects_a_development_identity_before_building(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(package.subprocess, "run") as run:
            root = Path(folder)
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                package.main([
                    "--output", str(root / "Lumen.app"),
                    "--sign-identity", "Apple Development: Fixture",
                    "--notary-profile", "fixture",
                    "--release-zip", str(root / "Lumen.zip"),
                ])
            self.assertEqual(error.exception.code, 2)
            self.assertIn("Developer ID Application", stderr.getvalue())
            run.assert_not_called()
            self.assertFalse((root / "Lumen.zip").exists())

    def test_rejected_notarization_does_not_produce_release_archive(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(package.subprocess, "run") as run:
            root = Path(folder)
            run.side_effect = [
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=json.dumps({"status": "Invalid", "id": "fixture-id"})),
            ]
            with self.assertRaisesRegex(ValueError, "fixture-id"):
                package.notarize(root / "Lumen.app", "fixture", root / "Lumen.zip")
            self.assertEqual(run.call_count, 2)
            self.assertFalse((root / "Lumen.zip").exists())

    def test_published_archive_is_packaged_after_ticket_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "Lumen.zip"
            validated = False

            def run(command, **kwargs):
                nonlocal validated
                if command[0] == "/usr/bin/ditto":
                    Path(command[-1]).write_text("stapled" if validated else "submission")
                elif command[1:3] == ["notarytool", "submit"]:
                    self.assertEqual(Path(command[3]).read_text(), "submission")
                    return subprocess.CompletedProcess(command, 0, stdout='{"status":"Accepted"}')
                elif command[1:3] == ["stapler", "validate"]:
                    validated = True
                self.assertFalse(archive.exists())
                return subprocess.CompletedProcess(command, 0)

            with patch.object(package.subprocess, "run", side_effect=run):
                package.notarize(root / "Lumen.app", "fixture", archive)
            self.assertEqual(archive.read_text(), "stapled")
