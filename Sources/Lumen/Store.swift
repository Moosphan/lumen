import AppKit
import Foundation
import SwiftUI

typealias Object = [String: Any]

let fileStates = ["cleaned":"已直接移除", "quarantined":"已隔离", "trashed":"已移入废纸篓",
                  "restored":"已恢复", "purged":"已清空", "skipped":"已跳过"]

struct ActionResult: Identifiable {
    let id = UUID()
    let batches: [Object]
}

extension Dictionary where Key == String, Value == Any {
    func string(_ key: String) -> String { self[key] as? String ?? "" }
    func number(_ key: String) -> Int64 { (self[key] as? NSNumber)?.int64Value ?? 0 }
    func paths(_ key: String) -> [String] { self[key] as? [String] ?? [] }
    func rows(_ key: String) -> [Object] { self[key] as? [Object] ?? [] }
}

func bytes(_ n: Int64) -> String {
    guard n != 0 else { return "0 B" }
    return ByteCountFormatter.string(fromByteCount: n, countStyle: .file)
}

let verdicts = ["safe": "低影响清理", "review": "待确认", "keep": "保留", "defer": "暂缓"]
let actions = ["clean": "直接移除", "quarantine": "移入隔离区", "trash": "移入废纸篓", "restore": "原位恢复", "restore_beside": "同名时旁置恢复", "uninstall":"卸载应用", "purge": "清空批次"]

struct Entry: Identifiable {
    let raw: Object
    var path: String { raw.string("path") }
    var id: String { path }
    var name: String { URL(fileURLWithPath: path).lastPathComponent }
    var size: Int64 { raw.number("size") }
    var allocated: Int64 { (raw["allocated_bytes"] as? NSNumber)?.int64Value ?? size }
    var decision: String { raw.string("decision") }
    var owner: String { raw.string("owner") }
    var reason: String { raw.string("reason") }
}

func resource(_ name: String, source: String) -> URL? {
    let manager = FileManager.default
    let bundled = Bundle.main.resourceURL?.appendingPathComponent(name)
    let local = URL(fileURLWithPath: manager.currentDirectoryPath).appendingPathComponent(source)
    // A packaged executable must only load code shipped in its own application.
    let options = Bundle.main.bundleURL.pathExtension == "app" ? [bundled] : [bundled, local]
    return options.compactMap { $0 }.first { manager.fileExists(atPath: $0.path) }
}

@MainActor final class Store: ObservableObject {
    @Published var data: Object = [:]
    @Published private(set) var inventory = InventoryPresentation()
    @Published var busy = ""
    @Published var notice = ""
    @Published var error: String?
    @Published var result: ActionResult?
    @Published private(set) var fixtureState: URL?
    @Published var generation = 0
    private var process: Process?
    var roots: [String] { data.paths("roots") }
    var files: [Entry] { inventory.files }
    var scanning: Bool { busy == "scan" || busy == "scan_local" }
    var projects: [String: [String]] { data["projects"] as? [String: [String]] ?? [:] }
    var duplicates: [String: [String]] { data["duplicates"] as? [String: [String]] ?? [:] }

    func call(_ command: String, _ args: Object = [:], done: ((Object) -> Void)? = nil) {
        guard busy.isEmpty else { return }
        guard let engine = resource("engine/bridge.py", source: "engine/bridge.py") else {
            error = "未找到本地引擎 engine/bridge.py。"
            return
        }
        let task = Process()
        let input = Pipe()
        let output = Pipe()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        task.arguments = ["-I", "-B", engine.path]
        if let fixtureState {
            var env = ProcessInfo.processInfo.environment
            env["LUMEN_STATE_DIR"] = fixtureState.path
            task.environment = env
        }
        task.standardInput = input
        task.standardOutput = output
        task.standardError = FileHandle.nullDevice
        var body = args
        body["cmd"] = command
        do {
            let json = try JSONSerialization.data(withJSONObject: body)
            try task.run()
            busy = command
            notice = ""
            process = task
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    try input.fileHandleForWriting.write(contentsOf: json)
                } catch { /* Exit and response are checked below. */ }
                try? input.fileHandleForWriting.close()
                let reply = output.fileHandleForReading.readDataToEndOfFile()
                task.waitUntilExit()
                let envelope = (try? JSONSerialization.jsonObject(with: reply)) as? Object
                let dashboard = envelope?["data"] as? Object
                let presentation = command == "fixture" ? nil : dashboard.map(InventoryPresentation.init)
                DispatchQueue.main.async {
                    guard self.process === task else { return }
                    self.process = nil
                    self.busy = ""
                    if ["scan", "scan_local"].contains(command) && task.terminationReason == .uncaughtSignal {
                        self.notice = "扫描已停止，保留上次完成的盘点。"
                        return
                    }
                    guard task.terminationStatus == 0, envelope?["ok"] as? Bool == true,
                          let dashboard else {
                        self.error = envelope?["error"] as? String ?? "本地引擎未能完成操作（退出码 \(task.terminationStatus)）。"
                        return
                    }
                    if let presentation {
                        self.data = dashboard
                        self.inventory = presentation
                        self.generation += 1
                    }
                    if command != "status" && command != "fixture" {
                        self.notice = ["scan", "scan_local"].contains(command) ? "扫描完成，共 \(self.files.count) 个文件。" : "已更新本地记录。"
                    }
                    if let last = dashboard["last_action"] as? Object {
                        let batches = dashboard.rows("last_actions")
                        self.result = ActionResult(batches: batches.isEmpty ? [last] : batches)
                    }
                    done?(dashboard)
                }
            }
        } catch { self.error = error.localizedDescription }
    }

    func scan(_ scopes: [String], deep: Bool, renewRoots: [String] = []) {
        call("scan", ["roots": Array(Set(scopes)).sorted(), "deep": deep, "renew_roots": renewRoots])
    }
    func scanLocal(deep: Bool) {
        if fixtureState != nil { scan(roots, deep: deep) }
        else { call("scan_local", ["deep": deep]) }
    }
    func startInventory(done: @escaping (Object) -> Void) {
        call("scan_local", ["startup": true], done: done)
    }
    func cancelScan() {
        if scanning, let process, process.isRunning { process.terminate() }
    }
    func addScope(deep: Bool) {
        guard fixtureState == nil else { return }
        let panel = NSOpenPanel()
        panel.title = "选择允许既白扫描的目录"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.showsHiddenFiles = true
        if panel.runModal() == .OK {
            let selected = panel.urls.map(\.path)
            scan(roots + selected, deep: deep, renewRoots: selected)
        }
    }
    func markDirectory(_ key: String) {
        let panel = NSOpenPanel()
        panel.title = key == "keep" ? "保留授权范围内的目录" : "排除授权范围内的目录"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.directoryURL = roots.first.map { URL(fileURLWithPath: $0) }
        guard panel.runModal() == .OK, let path = panel.url?.path else { return }
        guard roots.contains(where: { path == $0 || path.hasPrefix($0 + "/") }) else {
            error = "请选择已授权扫描范围内的目录。"
            return
        }
        call("settings", [key: Array(Set(data.paths(key) + [path])).sorted()])
    }
    func authorizeTrash() {
        guard fixtureState == nil else { return }
        let panel = NSOpenPanel()
        panel.title = "选择本用户的废纸篓目录"
        panel.showsHiddenFiles = true
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        guard panel.runModal() == .OK, let path = panel.url else { return }
        let userBin = path.lastPathComponent == ".Trash"
        let volumeBin = path.deletingLastPathComponent().lastPathComponent == ".Trashes" && path.lastPathComponent == String(getuid())
        guard userBin || volumeBin else {
            error = "请选择 .Trash 或外置卷 .Trashes 下以本人用户编号命名的目录。"
            return
        }
        call("settings", ["roots": Array(Set(roots + [path.path])).sorted(), "renew_roots": [path.path]]) { _ in
            self.call("trash_list")
        }
    }
    func exitFixture() {
        guard busy.isEmpty else { return }
        fixtureState = nil
        data = [:]
        inventory = InventoryPresentation()
        call("status")
    }

    func fixture() {
        guard busy.isEmpty else { return }
        let base = ProcessInfo.processInfo.environment["LUMEN_STATE_DIR"].map {
            URL(fileURLWithPath: $0).deletingLastPathComponent()
        } ?? FileManager.default.temporaryDirectory
        fixtureState = base.appendingPathComponent("Lumen-State-" + UUID().uuidString)
        data = [:]
        inventory = InventoryPresentation()
        call("fixture") { result in
            if let root = result["root"] as? String { self.scan([root], deep: true) }
        }
    }
}
