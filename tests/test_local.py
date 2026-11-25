import json
import hashlib
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

    def test_aider_discovers_local_project_and_protects_only_recognized_aider_data(self):
        header = b"\n# aider chat started at 2026-11-25 11:21:05\n\n"
        log = self.write("Work/团队/film/.aider.chat.history.md", header + b"synthetic-private-text")
        prompt = self.write("Work/团队/film/.aider.input.history", b"synthetic prompt")
        tags = self.write("Work/团队/film/.aider.tags.cache.v4/entry.dat", b"synthetic tags")
        source = self.write("Work/团队/film/reference.md", b"synthetic source")
        self.write("Projects/reserved/.aider.chat.history.md", b"not an Aider header")
        self.write("Projects/settings-only/.aider.conf.yml", b"model: fixture")
        self.write("Projects/node_modules/dependency/.aider.chat.history.md", header)
        self.write("Library/Caches/example/.aider.chat.history.md", header)
        link = self.home / "Projects/Link"
        link.symlink_to(log.parent, target_is_directory=True)

        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"], {str(log.parent): ["Aider"]})
        files = {row["path"]: row for row in report["files"]}
        for item in (log, prompt, tags):
            self.assertIn("Aider", files[str(item)]["agents"])
            self.assertEqual(files[str(item)]["decision"], "keep")
        self.assertEqual(files[str(source)]["projects"], [str(log.parent)])
        self.assertNotIn("synthetic-private-text", (self.state / "workspace.json").read_text())

    def test_aider_manual_scope_and_discovery_cache_revalidate_marker(self):
        project = self.manual / "worktree"
        project.mkdir()
        marker = project / ".aider.chat.history.md"
        marker.write_bytes(b"\r\n# aider chat started at 2026-11-25 12:00:00\r\n\r\n")
        self.assertNotIn(str(project), self.call("scan_local", startup=True)["projects"])
        self.call("scan", roots=[str(self.manual)], deep=False)
        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"][str(project)], ["Aider"])
        self.assertNotIn(str(project), self.call("settings", roots=[])["projects"])
        self.assertTrue(marker.exists())
        self.assertEqual(self.call("scan", roots=[str(self.manual)], deep=False)["projects"][str(project)], ["Aider"])
        marker.unlink()
        self.assertNotIn(str(project), self.call("scan_local", startup=True)["projects"])

    def test_aider_artifacts_stay_protected_in_manual_only_scan(self):
        marker = self.manual / ".AIDER.CHAT.HISTORY.MD"
        marker.write_bytes(b"unrecognized historical format")
        protected = self.manual / ".aider.tags.cache.v4" / "entry.dat"
        protected.parent.mkdir()
        protected.write_bytes(b"cache")
        report = self.call("scan", roots=[str(self.manual)], deep=True)
        rows = {row["path"]: row for row in report["files"]}
        self.assertEqual([rows[str(item)]["decision"] for item in (marker, protected)], ["keep", "keep"])
        result = self.call("run", paths=[str(marker), str(protected)], scan_id=report["scan_id"], action="quarantine")
        self.assertEqual({item["status"] for item in result["last_action"]["items"]}, {"skipped"})
        self.assertEqual(marker.read_bytes(), b"unrecognized historical format")

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

    def test_agent_store_inventory_is_protected_and_deduplicates_shared_bytes(self):
        root = self.write("Projects/film/notes.txt").parent
        codex = self.write(".codex/sessions/rollout.jsonl", json.dumps(
            {"type": "session_meta", "payload": {"cwd": str(root)}}).encode())
        self.write(".claude/projects/film/session.jsonl", json.dumps({"cwd": str(root)}).encode())
        shared = self.write(".codex/sessions/shared.log", b"shared")
        alias = self.home / ".claude/projects/film/shared.log"
        os.link(shared, alias)

        report = self.call("scan_local", startup=True)
        agents = {row["name"]: row for row in report["agent_inventory"]}
        self.assertEqual(report["projects"][str(root)], ["Claude Code", "Codex"])
        self.assertEqual(agents["Codex"]["projects"], [str(root)])
        self.assertEqual(agents["Codex"]["stores"][0]["path"], str(codex.parent))
        self.assertEqual(agents["Claude Code"]["stores"][0]["shared_bytes"], shared.stat().st_blocks * 512)
        self.assertTrue(agents["Claude Code"]["supports_events"])
        self.assertFalse(agents["Codex"]["has_verified_cleanup"])
        indexed = {row["path"]: row for row in report["files"]}
        self.assertEqual([indexed[str(p)]["decision"] for p in (codex, shared, alias)], ["keep"] * 3)
        self.assertEqual(report["indexed_bytes"],
                         sum(path.stat().st_blocks * 512 for path in
                             (root / "notes.txt", codex, alias.parent / "session.jsonl", shared)))

    def test_session_index_cache_refreshes_changed_files_and_forgets_excluded_stores(self):
        old = self.write("Projects/old/notes.txt").parent
        new = self.write("Projects/new/notes.txt").parent
        session = self.write(".codex/sessions/rollout.jsonl", json.dumps(
            {"type": "session_meta", "payload": {"cwd": str(old)}}).encode())
        self.assertIn(str(old), self.call("scan_local", startup=True)["projects"])
        with patch("engine.lumen.scan._cwd_records", side_effect=AssertionError("unmodified metadata was parsed again")):
            self.assertIn(str(old), self.call("scan_local", startup=True)["projects"])

        session.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(new)}}))
        refreshed = self.call("scan_local", startup=True)
        self.assertIn(str(new), refreshed["projects"])
        self.assertNotIn(str(old), refreshed["projects"])
        excluded = self.call("settings", local_exclude=[str(session.parent)])
        self.assertNotIn(str(new), excluded["projects"])
        self.assertFalse(excluded["agent_inventory"])

    def test_custom_session_store_must_stay_in_the_scoped_home(self):
        project = self.write("Projects/scene/render.mov").parent
        config = self.home / ".agent-data"
        self.write(".agent-data/projects/scene/session.jsonl", json.dumps({"cwd": str(project)}).encode())
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            result = self.call("scan_local", startup=True)
        self.assertEqual(result["projects"][str(project)], ["Claude Code"])
        self.assertEqual(result["agent_inventory"][0]["stores"][0]["path"], str(config / "projects"))

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.manual)}):
            result = self.call("scan_local", startup=True)
        self.assertNotIn(str(project), result["projects"])
        self.assertFalse(result["agent_inventory"])

    def test_hidden_and_symlinked_session_locations_are_never_read(self):
        project = self.write("Projects/hidden/source.txt").parent
        self.write(".codex/sessions/.Trash/retired.jsonl", json.dumps({
            "type": "session_meta", "payload": {"cwd": str(project)},
        }).encode())
        self.write(".codex/sessions/.git/retired.jsonl", json.dumps({
            "type": "session_meta", "payload": {"cwd": str(project)},
        }).encode())
        cycle = self.home / ".cycle"
        cycle.symlink_to(cycle, target_is_directory=True)
        with patch.dict(os.environ, {"CODEX_HOME": str(cycle)}):
            result = self.call("scan_local", startup=True)
        self.assertNotIn(str(project), result["projects"])

    def test_external_custom_store_requires_grant_and_stays_read_only(self):
        project = self.write("Projects/film/notes.txt").parent
        index = self.manual / "sessions"
        index.mkdir()
        source = index / "session.jsonl"
        source.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(project)}}))
        with patch.dict(os.environ, {"CODEX_HOME": str(self.manual)}):
            self.assertFalse(self.call("scan_local", startup=True)["agent_inventory"])
            self.call("scan", roots=[str(index)], deep=False)
            result = self.call("scan_local", startup=True)
            self.assertEqual(result["projects"][str(project)], ["Codex"])
            item = next(row for row in result["files"] if row["path"] == str(source))
            self.assertEqual((item["decision"], item["agent_store_path"]), ("keep", str(index)))
            outcome = self.call("run", paths=[str(source)], scan_id=result["scan_id"], action="quarantine")
            self.assertEqual(outcome["last_action"]["items"][0]["status"], "skipped")
            self.assertTrue(source.is_file())
            result = self.call("scan", roots=[], deep=False)
            self.assertFalse(result["agent_inventory"])

    def test_unreadable_store_metadata_marks_agent_footprint_partial(self):
        store = self.write(".gemini/projects.json", b"invalid json").parent
        agent, = self.call("scan_local", startup=True)["agent_inventory"]
        self.assertTrue(agent["partial"])
        self.assertTrue(any(row["path"] == str(store) and row["partial"] for row in agent["stores"]))

    def test_local_inventory_lists_models_and_protects_their_configuration(self):
        q4 = self.write(".lmstudio/models/Publisher/Camera/Camera-Q4_K_M.gguf", b"weight")
        manifest = self.write(".lmstudio/models/Publisher/Camera/README.md")
        report = self.call("scan_local", startup=True)
        model, = report["model_inventory"]["models"]
        self.assertEqual((model["tool"], model["quantization"], model["path"]),
                         ("LM Studio", "Q4_K_M", str(q4)))
        self.assertEqual(model["active_state"], "unknown")
        rows = {row["path"]: row for row in report["files"]}
        self.assertEqual(rows[str(manifest)]["decision"], "keep")
        self.assertTrue(any(row.get("model_store") for row in report["local_locations"]))
        result = self.call("run", action="quarantine", paths=[str(q4), str(manifest)],
                           scan_id=report["scan_id"])
        self.assertEqual({item["status"] for item in result["last_action"]["items"]}, {"skipped"})

    def test_openclaw_default_hidden_workspace_appears_in_workspace_inventory(self):
        workspace = self.write(".openclaw/workspace/notes.txt").parent
        asset = self.write("Media/footage.wav")
        self.write(".openclaw/workspace/timeline.otio", json.dumps({
            "OTIO_SCHEMA": "Timeline.1",
            "tracks": {"OTIO_SCHEMA": "Stack.1", "children": [
                {"OTIO_SCHEMA": "Clip.1", "media_reference": {
                    "OTIO_SCHEMA": "ExternalReference.1", "target_url": asset.as_uri(),
                }},
            ]},
        }).encode())
        self.write(".openclaw/openclaw.json", json.dumps(
            {"agents": {"defaults": {"workspace": str(workspace)}}}).encode())
        self.call("scan", roots=[str(asset.parent)], deep=False)
        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"][str(workspace)], ["OpenClaw"])
        self.assertIn(str(workspace), next(row for row in report["agent_inventory"]
                                          if row["name"] == "OpenClaw")["projects"])
        source = next(row for row in report["files"] if row["path"] == str(asset))
        self.assertEqual(source["decision"], "keep")
        self.assertTrue(source["refs"])

    def test_developer_cache_candidates_use_scoped_quarantine_and_preserve_vm_images(self):
        npm = self.write(".npm/_cacache/content-v2/sha512/aa/bb/" + "c" * 124, b"cache bytes")
        docker = self.write("Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw", b"VM bytes")
        report = self.call("scan_local", startup=True)
        groups = {row["tool"]: row for row in report["developer_caches"]}
        self.assertEqual(groups["npm"]["review_paths"], [str(npm)])
        self.assertEqual(groups["Docker"]["review_paths"], [])
        self.assertIsNone(groups["Docker"]["isolated_build_cache_bytes"])
        result = self.call("run", paths=[str(npm), str(docker)], scan_id=report["scan_id"], action="quarantine")
        status = {row["path"]: row["status"] for row in result["last_action"]["items"]}
        self.assertEqual(status, {str(npm): "quarantined", str(docker): "skipped"})
        self.call("restore", batch_id=result["last_action"]["id"])
        self.assertEqual(npm.read_bytes(), b"cache bytes")
        self.assertEqual(docker.read_bytes(), b"VM bytes")

    def test_manual_case_alias_cannot_quarantine_docker_vm(self):
        docker = self.write("Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw", b"VM bytes")
        alias = self.home / "library/containers/com.docker.docker/data/vms/0/data"
        if not alias.is_dir():
            self.skipTest("requires a case-insensitive fixture volume")
        report = self.call("scan", roots=[str(alias)], deep=True)
        row, = [entry for entry in report["files"] if entry["ino"] == docker.stat().st_ino]
        self.assertEqual(row["decision"], "keep")
        docker_group, = report["developer_caches"]
        self.assertEqual(docker_group["review_paths"], [])
        result = self.call("run", paths=[row["path"]], scan_id=report["scan_id"], action="quarantine")
        self.assertEqual(result["last_action"]["items"][0]["status"], "skipped")
        self.assertEqual(docker.read_bytes(), b"VM bytes")

    def test_model_recipe_survives_rescan_and_revalidates_destination_grant(self):
        weights = b"versioned weights"
        digest = hashlib.sha256(weights).hexdigest()
        repository = ".cache/huggingface/hub/models--org--weights"
        blob = self.write(repository + "/blobs/" + digest, weights)
        artifact = self.home / repository / "snapshots" / ("a" * 40) / "model.safetensors"
        artifact.parent.mkdir(parents=True)
        artifact.symlink_to(os.path.relpath(blob, artifact.parent))
        report = self.call("scan_local", startup=True)
        model, = [item for item in report["model_inventory"]["models"] if item["version"]]
        with self.assertRaisesRegex(ValueError, "版本"):
            self.call("model_pin", model_id=model["id"], scan_id=report["scan_id"],
                      filename=artifact.name, digest="sha256:" + "b" * 64)
        recipe = self.call("model_pin", model_id=model["id"], scan_id=report["scan_id"],
                           filename=artifact.name)["recipe"]
        artifact.unlink()
        blob.unlink()
        scanned = self.call("scan", roots=[str(self.manual)])
        self.assertEqual(scanned["model_recovery_recipes"], [recipe])

        def revoke(proof, destination, *, cancel, excluded_roots, precommit):
            self.assertEqual(proof.details()["digest"], recipe["digest"])
            self.assertEqual(destination, self.manual)
            self.call("settings", roots=[])
            with precommit():
                self.fail("revoked directory was accepted for publication")

        with patch("engine.lumen.model_recovery.recover", side_effect=revoke):
            with self.assertRaisesRegex(ValueError, "授权"):
                self.call("model_recover", recipe_id=recipe["id"], destination=str(self.manual))

    def test_configured_comfy_weights_are_discovered_in_the_scoped_home(self):
        weight = self.write("CustomWeights/landscape.safetensors", b"synthetic")
        self.write("Documents/ComfyUI/extra_model_paths.yaml",
                   f"studio:\n  base_path: {self.home}\n  checkpoints: CustomWeights\n".encode())
        report = self.call("scan_local", startup=True)
        model, = report["model_inventory"]["models"]
        self.assertEqual((model["path"], model["tool"]), (str(weight), "ComfyUI"))
        report = self.call("settings", local_exclude=[str(weight.parent)])
        self.assertEqual(report["model_inventory"]["models"], [])

    def test_explicit_lm_studio_model_folder_is_scoped_and_revocable(self):
        weight = self.manual / 'Publisher' / 'Camera' / 'Camera-Q8_0.gguf'
        weight.parent.mkdir(parents=True)
        weight.write_text('synthetic')
        root = {"path": str(self.manual), "tool": "LM Studio"}
        with self.assertRaises(ValueError):
            self.call("settings", model_roots=[root])
        report = self.call("settings", roots=[str(self.manual)], model_roots=[root])
        model, = report["model_inventory"]["models"]
        self.assertEqual((model["path"], model["tool"]), (str(weight), "LM Studio"))
        removed = self.call("settings", roots=[])
        self.assertEqual(removed["model_roots"], [])
        self.assertFalse(removed["model_inventory"]["models"])

    def test_manual_comfy_bin_weights_stay_protected_without_model_scope(self):
        project = self.manual / 'ComfyUI'
        weight = project / 'models/checkpoints/model.bin'
        weight.parent.mkdir(parents=True)
        weight.write_bytes(b'weights')
        workflow = project / 'user/default/workflows/render.json'
        workflow.parent.mkdir(parents=True)
        workflow.write_text(json.dumps({"nodes": [
            {"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": [weight.name]},
            {"id": "\ud800", "type": "UnknownLoader\ud800", "widgets_values": []},
        ]}))
        report = self.call("scan", roots=[str(project)], deep=False)
        model, = report["model_inventory"]["models"]
        self.assertEqual(model["path"], str(weight))
        self.assertTrue(model["workflow_refs"])
        self.assertTrue(report["model_inventory"]["issues"])
        current = next(row for row in report["files"] if row["path"] == str(weight))
        self.assertEqual(current["decision"], "keep")
        blocked = self.call("run", action="quarantine", paths=[str(weight)], scan_id=report["scan_id"])
        self.assertEqual(blocked["last_action"]["items"][0]["status"], "skipped")

    def test_agent_observation_is_removed_when_a_grant_is_renewed_for_replaced_directory(self):
        self.call("scan", roots=[str(self.manual)])
        event = {"hook_event_name": "Stop", "session_id": "session", "turn_id": "turn", "cwd": str(self.manual)}
        self.call("agent_event", agent="Codex", event=event)
        self.manual.rename(self.manual.with_name("old manual"))
        self.manual.mkdir()
        self.assertEqual(self.call("status")["agent_observations"], [])
        report = self.call("scan", roots=[str(self.manual)], renew_roots=[str(self.manual)])
        self.assertEqual(report["agent_observations"], [])
        self.assertFalse(self.call("agent_event", agent="Codex", event=event)["duplicate"])

    def test_overlapping_session_sources_keep_distinct_parser_metadata(self):
        codex = self.write("Projects/codex/source.txt").parent
        claude = self.write("Projects/claude/source.txt").parent
        self.write(".custom/sessions/projects/session.jsonl", (
            json.dumps({"type": "session_meta", "payload": {"cwd": str(codex)}}) + "\n" +
            json.dumps({"cwd": str(claude)})
        ).encode())
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".custom"),
                                     "CLAUDE_CONFIG_DIR": str(self.home / ".custom/sessions")}):
            for _ in range(2):
                result = self.call("scan_local", startup=True)
                self.assertEqual(result["projects"][str(codex)], ["Codex"])
                self.assertEqual(result["projects"][str(claude)], ["Claude Code"])

    def test_invalid_session_paths_cannot_poison_persisted_metadata(self):
        project = self.write("Projects/valid/source.txt").parent
        self.write(".codex/sessions/rollout.jsonl", "\n".join(json.dumps({
            "type": "session_meta", "payload": {"cwd": cwd},
        }) for cwd in ("relative\ud800", "/nul\0byte", str(project))).encode())
        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"], {str(project): ["Codex"]})
        cache = json.loads((self.state / "workspace.json").read_text())["agent_metadata"]
        self.assertEqual([row["paths"] for row in cache.values()], [[str(project)]])

    def test_installed_agent_and_two_discovered_agent_sources_are_attributed_read_only(self):
        self.write("Applications/Cursor.app/Contents/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": "com.todesktop.230313mzl4w4u92", "CFBundleShortVersionString": "test",
        }))
        root = self.write("Projects/shared cut/notes.txt").parent
        cursor = self.write("Library/Application Support/Cursor/User/workspaceStorage/id/workspace.json",
                            json.dumps({"folder": root.as_uri()}).encode())
        gemini = self.write(".gemini/projects.json",
                            json.dumps({"projects": {str(root): "shared-cut"}}).encode())

        report = self.call("scan_local", startup=True)
        self.assertEqual(report["projects"][str(root)], ["Cursor", "Gemini CLI"])
        agents = {row["name"]: row for row in report["agent_inventory"]}
        self.assertTrue(agents["Cursor"]["installed"])
        self.assertEqual(agents["Cursor"]["installations"][0]["version"], "test")
        self.assertFalse(agents["Gemini CLI"]["installed"])
        rows = {r["path"]: r for r in report["files"]}
        for path, owner in ((cursor, "Cursor"), (gemini, "Gemini CLI")):
            self.assertEqual(rows[str(path)]["agents"], [owner])
            self.assertEqual(rows[str(path)]["decision"], "keep")
        outcome = self.call("run", paths=[str(cursor), str(gemini)], scan_id=report["scan_id"],
                            action="quarantine")
        self.assertTrue(all(row["status"] == "skipped" for row in outcome["last_action"]["items"]))
        self.assertTrue(cursor.exists() and gemini.exists())

    def test_deep_inventory_does_not_analyze_agent_store_payloads(self):
        source = self.write(".codex/sessions/capture.html", b'<img src="image.png">')
        copy = self.write(".codex/sessions/capture-copy.html", source.read_bytes())
        target = self.write(".codex/sessions/image.png")
        result = self.call("scan_local", deep=True)
        items = {row["path"]: row for row in result["files"]}
        self.assertTrue(all(str(p) in items for p in (source, copy, target)))
        self.assertEqual(result["hash_reads"], 0)
        self.assertEqual(result["static_refs"], [])
        self.assertTrue(all(row["decision"] == "keep" for row in items.values()))

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
