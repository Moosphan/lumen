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

    @MainActor static func main() async throws {
        checkWorkspaceRanking()
        guard let temporaryPath = ProcessInfo.processInfo.environment["TMPDIR"],
              let state = ProcessInfo.processInfo.environment["LUMEN_STATE_DIR"],
              URL(fileURLWithPath: state).deletingLastPathComponent().path ==
              URL(fileURLWithPath: temporaryPath).deletingLastPathComponent().path else {
            fatalError("Use tests/native/smoke.sh to provide an isolated workspace")
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
