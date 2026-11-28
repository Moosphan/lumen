import Foundation

@main struct StoreSmoke {
    @MainActor static func waitForReply(_ store: Store) async throws {
        for _ in 0..<300 {
            if let error = store.error { fatalError(error) }
            if store.busy.isEmpty { return }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        fatalError("Timed out waiting for Python bridge")
    }

    @MainActor static func checkDiskAccessGate(state: String) async throws {
        let store = Store()
        store.startInventory { report in precondition(report.string("disk_access") == "limited") }
        try await waitForReply(store)
        precondition(store.needsDiskAccess && store.data.string("scan_id").isEmpty && store.files.isEmpty)
        precondition(!FileManager.default.fileExists(atPath: state))
        store.scanLocal(deep: true)
        try await waitForReply(store)
        precondition(store.needsDiskAccess && !FileManager.default.fileExists(atPath: state))
        store.fixture()
        try await waitForReply(store)
        precondition(store.fixtureState != nil && !store.files.isEmpty)
        precondition(!FileManager.default.fileExists(atPath: state))
        store.exitFixture()
        try await waitForReply(store)
        precondition(store.fixtureState == nil && store.data.string("disk_access") == "limited")
        precondition(!FileManager.default.fileExists(atPath: state))
        print("Native Store loaded limited inventory without indexing protected files and kept the isolated demo available.")
    }

    @MainActor static func checkDiskAccessRecovery(state: String) async throws {
        let home = URL(fileURLWithPath: ProcessInfo.processInfo.environment["HOME"] ?? "")
        precondition(home.path == URL(fileURLWithPath: state).deletingLastPathComponent()
            .appendingPathComponent("permission-home").path)
        let trash = home.appendingPathComponent(".Trash")
        let sample = home.appendingPathComponent("Downloads/fixture.txt")
        let store = Store()
        store.startInventory { _ in }
        try await waitForReply(store)
        precondition(store.needsDiskAccess && !FileManager.default.fileExists(atPath: state))

        try FileManager.default.createDirectory(at: trash, withIntermediateDirectories: false)
        try FileManager.default.createDirectory(at: sample.deletingLastPathComponent(), withIntermediateDirectories: false)
        try Data("synthetic download".utf8).write(to: sample)
        store.startInventory { _ in }
        try await waitForReply(store)
        precondition(!store.needsDiskAccess && !store.data.string("scan_id").isEmpty)
        precondition(store.files.contains { $0.path == sample.path })
        precondition((store.data["disk_usage"] as? Object)?.number("used_bytes") ?? 0 > 0)

        let workspace = URL(fileURLWithPath: state).appendingPathComponent("workspace.json")
        let original = try Data(contentsOf: workspace)
        try FileManager.default.removeItem(at: trash)
        store.scanLocal(deep: false)
        try await waitForReply(store)
        precondition(store.needsDiskAccess && store.data.string("scan_id").isEmpty && store.files.isEmpty)
        let after = try Data(contentsOf: workspace)
        precondition(original == after)
        print("Native Store resumed a complete scan after access was available and preserved its snapshot when access was lost.")
    }

    static func checkWorkspaceRanking() {
        let projects = ["/a": ["Codex"], "/b": ["Claude Code", "Codex"]]
        func file(_ path: String, _ inode: Int, _ size: Int, _ root: String, _ decision: String) -> Entry {
            Entry(raw: ["path": path, "dev": 1, "ino": inode, "size": size, "projects": [root], "decision": decision])
        }
        let files = [
            file("/a/candidate", 1, 10, "/a", "safe"),
            file("/b/unknown", 2, 25, "/b", "review"),
            file("/a/shared", 3, 50, "/a", "keep"),
            file("/b/shared-link", 3, 50, "/b", "keep")
        ]
        let groups = workspacePartitions(projects: projects, files: files, bySafe: false)
        precondition(groups.map(\.id) == [.shared, .joint, .single("Codex")])
        precondition(groups.map { $0.stats.bytes } == [50, 25, 10])
        precondition(groups[0].buckets[0].paths == ["/a/shared", "/b/shared-link"])
        precondition(groups[1].stats.review == 1 && groups[2].stats.safe == 1)
        let bySafe = workspacePartitions(projects: projects, files: files, bySafe: true)
        precondition(bySafe.first?.id == .single("Codex"))
    }

    static func checkWorkspaceInventory() {
        let files: [Object] = [
            ["path": "/projects/shared/export.mov", "dev": 1, "ino": 1, "size": 20,
             "projects": ["/projects/shared"], "kind": "video", "decision": "review"],
            ["path": "/projects/shared/input.png", "dev": 1, "ino": 2, "size": 40,
             "projects": ["/projects/shared"], "kind": "image", "decision": "keep"],
            ["path": "/projects/linked/input.png", "dev": 1, "ino": 2, "size": 40,
             "projects": ["/projects/linked"], "kind": "image", "decision": "keep"],
            ["path": "/agents/cursor/cache.log", "dev": 1, "ino": 3, "size": 8,
             "agents": ["Cursor"], "agent_store_path": "/agents/cursor", "kind": "log", "decision": "keep"],
            ["path": "/agents/extra/cache-alias.log", "dev": 1, "ino": 3, "size": 8,
             "agents": ["Cursor"], "kind": "log", "decision": "keep"],
            ["path": "/agents/extra/note.log", "dev": 1, "ino": 4, "size": 4,
             "agents": ["Cursor"], "kind": "log", "decision": "keep"]
        ]
        let stores: [Object] = [["path": "/agents/cursor", "title": "Cursor 数据", "bytes": 8, "count": 1]]
        let agents: [Object] = [
            ["name": "Cursor", "installed": true, "projects": ["/projects/shared"],
             "bytes": 72, "count": 4, "shared_bytes": 68,
             "installations": [["path": "/Applications/Cursor.app", "version": "2.0"]], "stores": stores]
        ]
        let report: Object = [
            "projects": ["/projects/shared": ["Codex", "Cursor"], "/projects/linked": ["Codex"]],
            "files": files, "agent_inventory": agents
        ]
        let inventory = InventoryPresentation(report)
        let images = inventory.mediaGroups.first { $0.id == "image" }
        precondition(images?.allocated == 40 && images?.identities == 1 && images?.referenced == 1)
        let project = inventory.workspaceProjects["/projects/shared"]!
        precondition(project.agents == ["Codex", "Cursor"])
        precondition(project.detail.stats.bytes == 60 && project.detail.stats.sharedBytes == 40)
        precondition(project.detail.stats.review == 1 && project.detail.stats.reviewBytes == 20)
        precondition(project.categories.first(where: { $0.kind == "image" })?.detail.paths.count == 2)
        let cursor = inventory.workspaceAgents.first(where: { $0.id == "Cursor" })!
        precondition(cursor.stats.bytes == 72 && cursor.stats.sharedBytes == 68 && cursor.installed)
        precondition(cursor.stores.first?.detail.paths == ["/agents/cursor/cache.log"])
        precondition(cursor.toolBuckets.count == 1 && cursor.toolBuckets[0].stats.bytes == 4)
        precondition(cursor.toolBuckets[0].paths == ["/agents/extra/cache-alias.log", "/agents/extra/note.log"])
        precondition(inventory.workspaceAgents.contains { $0.id == "Codex" && !$0.installed })

        var legacy = report
        legacy.removeValue(forKey: "agent_inventory")
        let old = InventoryPresentation(legacy)
        precondition(old.workspaceProjects.count == 2)
        precondition(old.workspaceAgents.first { $0.id == "Cursor" }?.stats.bytes == 72)
    }

    @MainActor static func main() async throws {
        checkWorkspaceRanking()
        checkWorkspaceInventory()
        guard let temporaryPath = ProcessInfo.processInfo.environment["TMPDIR"],
              let state = ProcessInfo.processInfo.environment["LUMEN_STATE_DIR"],
              URL(fileURLWithPath: state).deletingLastPathComponent().path ==
              URL(fileURLWithPath: temporaryPath).deletingLastPathComponent().path else {
            fatalError("Use tests/native/smoke.sh to provide an isolated workspace")
        }
        if CommandLine.arguments.contains("--verify-disk-access-gate") {
            try await checkDiskAccessGate(state: state)
            return
        }
        if CommandLine.arguments.contains("--verify-disk-access-recovery") {
            try await checkDiskAccessRecovery(state: state)
            return
        }
        let lab = URL(fileURLWithPath: temporaryPath).resolvingSymlinksInPath()
        let store = Store()
        store.call("status")
        try await waitForReply(store)
        precondition(store.roots.isEmpty)
        let original = lab.appendingPathComponent("workspace")
        try FileManager.default.createDirectory(at: original, withIntermediateDirectories: false)
        let kept = original.appendingPathComponent("keep.txt")
        try Data("preserve me".utf8).write(to: kept)
        store.scan([original.path], deep: false, renewRoots: [original.path])
        try await waitForReply(store)
        store.call("settings", ["keep": [kept.path]])
        try await waitForReply(store)
        store.fixture()
        try await waitForReply(store)
        precondition(store.roots.count == 1 && !store.files.isEmpty)
        let fixture = URL(fileURLWithPath: store.roots[0]).resolvingSymlinksInPath()
        precondition(fixture.path.hasPrefix(lab.path + "/") && fixture != original)
        guard let sample = store.files.first(where: {
            $0.decision == "review" && $0.path == store.roots[0] + "/Downloads/draft-a.zip"
        }) else { fatalError("Missing synthetic review asset") }
        let approved = store.data.string("scan_id")
        store.call("run", ["action": "quarantine", "paths": [sample.path], "scan_id": approved])
        try await waitForReply(store)
        guard let batch = store.data.rows("history").first else { fatalError("Missing quarantine batch") }
        precondition(batch.rows("items").first?.string("status") == "quarantined")
        precondition(!FileManager.default.fileExists(atPath: sample.path))
        store.call("restore", ["batch_id": batch.string("id")])
        try await waitForReply(store)
        precondition(FileManager.default.fileExists(atPath: sample.path))
        store.call("settings", ["roots": []])
        try await waitForReply(store)
        precondition(store.roots.isEmpty && store.files.isEmpty)
        store.exitFixture()
        try await waitForReply(store)
        precondition(store.fixtureState == nil && store.roots == [original.path])
        precondition(store.data.paths("keep") == [kept.path])
        print("Native Store passed scoped scan, fixture isolation, quarantine, restore and revocation.")
    }
}
