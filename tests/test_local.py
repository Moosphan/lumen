import json
import os
import plistlib
import tempfile
import unittest
from unittest.mock import patch
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

    def test_application_cache_analysis_preserves_credentials_and_requires_an_idle_app(self):
        app = self.write("Applications/Editor.app/Contents/Info.plist",
                         plistlib.dumps({"CFBundleIdentifier": "example.editor"})).parents[1]
        cache = self.write("Library/Containers/example.editor/Data/Library/Caches/preview.bin", b"rebuild")
        credential = self.write("Library/Containers/example.editor/Data/Library/Caches/Cookies", b"session")
        sidecar = credential.with_name("Cookies.sqlite-wal")
        sidecar.write_bytes(b"session journal")
        result = self.call("scan_local", startup=True)
        self.assertNotIn(str(cache), {r["path"] for r in result["files"]})
        result = self.call("scan_app", path=str(app))
        rows = {r["path"]: r for r in result["files"]}
        self.assertEqual((rows[str(cache)]["cleanup_kind"], rows[str(cache)]["decision"]), ("cache", "review"))
        self.assertEqual(rows[str(credential)]["decision"], "keep")
        self.assertEqual(rows[str(sidecar)]["decision"], "keep")
        with patch("engine.lumen.app_cleanup.available", return_value=False):
            busy = self.call("run", paths=[str(cache)], scan_id=result["scan_id"], action="quarantine")
            self.assertEqual(busy["last_action"]["items"][0]["status"], "skipped")
        with patch("engine.lumen.app_cleanup.available", return_value=True):
            direct = self.call("run", paths=[str(cache)], scan_id=busy["scan_id"], action="clean")
            self.assertEqual(direct["last_action"]["items"][0]["status"], "skipped")
            moved = self.call("run", paths=[str(cache), str(credential)], scan_id=direct["scan_id"], action="quarantine")
        self.assertEqual({r["status"] for r in moved["last_action"]["items"]}, {"quarantined", "skipped"})
        self.assertEqual(credential.read_bytes(), b"session")
        self.call("restore", batch_id=moved["last_action"]["id"])
        self.assertEqual(cache.read_bytes(), b"rebuild")

    def test_xcode_candidates_exclude_compiled_deliverables_and_sources(self):
        self.write("Applications/Xcode.app/Contents/Info.plist",
                   plistlib.dumps({"CFBundleIdentifier": "com.apple.dt.Xcode"}))
        prefix = "Library/Developer/Xcode/DerivedData/Example/"
        intermediate = self.write(prefix + "Build/Intermediates.noindex/output.o")
        product = self.write(prefix + "Build/Products/export.zip")
        checkout = self.write(prefix + "SourcePackages/checkouts/module/README.md")
        report = self.call("scan_local", startup=True)
        rows = {r["path"]: r for r in report["files"]}
        self.assertEqual(rows[str(intermediate)]["cleanup_kind"], "build")
        self.assertNotIn("cleanup_kind", rows[str(product)])
        self.assertNotIn("cleanup_kind", rows[str(checkout)])

    def test_space_tree_ranks_immediate_children_and_does_not_follow_links(self):
        biggest = self.write("Projects/big/deep.bin", bytes(32 * 1024))
        small = self.write("Downloads/notes.txt", b"hello")
        hidden = self.write("Projects/hidden/private.bin", bytes(64 * 1024))
        (self.home / "cross-link").symlink_to(self.manual, target_is_directory=True)
        (self.manual / "outside").write_bytes(bytes(128 * 1024))
        os.link(biggest, biggest.with_name("same-inode.bin"))
        self.call("settings", local_exclude=[str(hidden.parent)])
        top = self.call("size_tree")
        self.assertEqual([row["name"] for row in top["children"]], ["Projects", "Downloads", "cross-link"])
        self.assertEqual(top["bytes"], (os.stat(biggest).st_blocks + os.stat(small).st_blocks) * 512)
        projects = self.call("size_tree", path=str(self.home / "Projects"))
        self.assertEqual([row["name"] for row in projects["children"]], ["big"])
        self.assertEqual(projects["children"][0]["bytes"], os.stat(biggest).st_blocks * 512)
        with self.assertRaises(ValueError):
            self.call("size_tree", path=str(self.home / "cross-link"))

    def test_space_tree_uses_parent_snapshot_for_unvisited_descendants(self):
        file = self.write("Projects/film/cuts/preview.mov", bytes(16 * 1024))
        top = self.call("size_tree")
        read_workspace = patch("engine.lumen.service.load_config", side_effect=AssertionError("read full workspace"))
        rebuild = patch("engine.lumen.space_tree.build_index", side_effect=AssertionError("walked subtree twice"))
        with read_workspace, rebuild:
            for folder in (self.home / "Projects", self.home / "Projects/film", file.parent):
                cached = self.call("size_tree", path=str(folder))
                self.assertEqual(cached["indexed_at"], top["indexed_at"])
                self.assertEqual(cached["bytes"], top["bytes"])

        new_file = self.write("Projects/film/cuts/new.cut", bytes(8192))
        refreshed = self.call("size_tree", path=str(file.parent), refresh=True)
        self.assertIn(str(new_file), {item["path"] for item in refreshed["children"]})
        self.call("settings", local_exclude=[str(new_file)])
        filtered = self.call("size_tree", path=str(file.parent))
        self.assertNotIn(str(new_file), {item["path"] for item in filtered["children"]})
