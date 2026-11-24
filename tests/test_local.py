import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from engine.lumen.service import dispatch


class LocalInventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.manual = self.base / "manual"
        self.manual.mkdir()

    def write(self, relative, data=b"data"):
        path = self.home / relative
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(data)
        return path

    def call(self, cmd, **args):
        return dispatch(self.state, {"cmd": cmd, **args}, local_home=self.home)

    def test_default_scan_finds_installed_apps_and_agent_projects(self):
        app = self.write("Applications/Editor.app/Contents/Info.plist",
                         plistlib.dumps({"CFBundleIdentifier": "example.editor"}))
        root = self.home / "Projects/film"
        self.write("Projects/film/render.mp4")
        session = {"type": "session_meta", "payload": {"cwd": str(root)}}
        self.write(".codex/sessions/rollout.jsonl", json.dumps(session).encode())
        self.write("Documents/Codex/2026-11-24/new-project/dist/thumb.png")
        build_app = "Projects/film/build/Runner.app/Contents/Info.plist"
        self.write(build_app, plistlib.dumps({"CFBundleIdentifier": "example.runner"}))
        report = self.call("scan_local", deep=False)
        self.assertEqual(report["roots"], [])
        self.assertEqual([r["path"] for r in report["apps"]], [str(app.parents[1])])
        self.assertGreater(report["apps"][0]["inventory_bytes"], 0)
        self.assertEqual(report["projects"][str(root)], ["Codex"])
        self.assertEqual(report["projects"][str(self.home / "Documents/Codex/2026-11-24/new-project")], ["Codex"])
        self.assertEqual(sum(r["bytes"] for r in report["categories"]), report["indexed_bytes"])

    def test_download_review_does_not_require_a_manual_folder_grant(self):
        item = self.write("Downloads/archive.zip")
        self.call("scan", roots=[str(self.manual)])
        report = self.call("scan_local", deep=False)
        self.assertEqual(report["roots"], [str(self.manual)])
        self.assertTrue(item.exists())
        result = self.call("run", paths=[str(item)], scan_id=report["scan_id"], action="quarantine")
        self.assertEqual(result["last_action"]["items"][0]["status"], "quarantined")
        self.call("restore", batch_id=result["last_action"]["id"])
        self.assertEqual(item.read_bytes(), b"data")

    def test_discovered_project_source_is_inventory_only(self):
        item = self.write("Documents/Codex/movie/notes.txt")
        first = self.call("scan_local", deep=False)
        row = next(r for r in first["files"] if r["path"] == str(item))
        self.assertEqual(row["decision"], "keep")
        result = self.call("run", paths=[str(item)], scan_id=first["scan_id"], action="quarantine")
        self.assertEqual(result["last_action"]["items"][0]["status"], "skipped")
        self.assertTrue(item.exists())

    def test_excluding_automatic_root_blocks_scan_and_actions(self):
        item = self.write("Downloads/archive.zip")
        first = self.call("scan_local", deep=False)
        latest = self.call("settings", local_exclude=[str(item.parent)])
        self.assertFalse(latest["files"])
        with self.assertRaises(ValueError):
            self.call("run", paths=[str(item)], scan_id=first["scan_id"], action="quarantine")
        self.assertTrue(item.exists())

    def test_startup_samples_session_metadata_without_changing_deep_scan_preference(self):
        early = self.write("Projects/early/notes.txt").parent
        late = self.write("Projects/late/notes.txt").parent
        first = {"type": "session_meta", "payload": {"cwd": str(early)}}
        last = {"type": "turn_context", "payload": {"cwd": str(late)}}
        long_message = {"type": "response_item", "payload": {"text": "x" * (132 * 1024)}}
        records = "\n".join(map(json.dumps, (first, long_message, last)))
        self.write(".codex/sessions/rollout.jsonl", records.encode())

        normal = self.call("scan_local", deep=True)
        self.assertIn(str(late), normal["projects"])
        startup = self.call("scan_local", startup=True)
        self.assertTrue(startup["deep"])
        self.assertIn(str(early), startup["projects"])
        self.assertNotIn(str(late), startup["projects"])
        self.assertTrue(any("后续工作目录" in issue for issue in startup["issues"]))
