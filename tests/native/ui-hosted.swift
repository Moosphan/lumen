import AppKit
import ApplicationServices
import Foundation
import ImageIO

private struct Failure: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) { description = message }
}

private func eventually<T>(_ expectation: String, seconds: Double = 30, _ attempt: () -> T?) throws -> T {
    let deadline = Date().addingTimeInterval(seconds)
    repeat {
        if let value = attempt() { return value }
        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
    } while Date() < deadline
    throw Failure("Timed out waiting for \(expectation)")
}

private func attribute(_ element: AXUIElement, _ key: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, key as CFString, &value) == .success else { return nil }
    return value
}

private func window(_ app: AXUIElement) -> AXUIElement? {
    let windows = attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
    return windows.first { (attribute($0, kAXTitleAttribute) as? String) == "既白 Lumen" }
}

private func element(_ name: String, in app: AXUIElement, button: Bool) -> AXUIElement? {
    guard let root = window(app) else { return nil }
    var queue = [root], cursor = 0
    while cursor < queue.count && cursor < 6000 {
        let node = queue[cursor]
        cursor += 1
        let labels = [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute].compactMap {
            attribute(node, $0) as? String
        }
        if labels.contains(name) {
            if !button { return node }
            var supported: CFArray?
            if AXUIElementCopyActionNames(node, &supported) == .success,
               (supported as? [String])?.contains(kAXPressAction) == true,
               (attribute(node, kAXEnabledAttribute) as? Bool) != false { return node }
        }
        queue += attribute(node, kAXChildrenAttribute) as? [AXUIElement] ?? []
    }
    return nil
}

private func press(_ name: String, in app: AXUIElement) throws {
    let control = try eventually("enabled button \(name)") { element(name, in: app, button: true) }
    guard AXUIElementPerformAction(control, kAXPressAction as CFString) == .success else {
        throw Failure("AXPress failed on \(name)")
    }
}

private func mcpCall(bundle: URL, tool: String, arguments: [String: Any] = [:]) throws -> [String: Any] {
    let messages: [[String: Any]] = [
        ["jsonrpc": "2.0", "id": 1, "method": "initialize", "params": [
            "protocolVersion": "2025-11-25", "capabilities": [String: Any](),
            "clientInfo": ["name": "lumen-hosted-check", "version": "1.0.0"],
        ]],
        ["jsonrpc": "2.0", "method": "notifications/initialized"],
        ["jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": ["name": tool, "arguments": arguments]],
    ]
    let script = bundle.appendingPathComponent("Contents/Resources/engine/mcp_server.py")
    let input = Pipe(), output = Pipe()
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    process.arguments = ["-I", "-B", script.path]
    process.standardInput = input
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    try process.run()
    for message in messages {
        try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: message))
        try input.fileHandleForWriting.write(contentsOf: Data([10]))
    }
    try input.fileHandleForWriting.close()
    let outputData = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    let replies = try outputData.split(separator: 10).compactMap { line in
        try JSONSerialization.jsonObject(with: Data(line)) as? [String: Any]
    }
    guard (replies.first?["result"] as? [String: Any])?["protocolVersion"] as? String == "2025-11-25" else {
        throw Failure("MCP initialization failed before verifying \(tool)")
    }
    guard process.terminationStatus == 0,
          let reply = replies.first(where: { $0["id"] as? Int == 2 }),
          let result = reply["result"] as? [String: Any], result["isError"] as? Bool == false,
          let blocks = result["content"] as? [[String: Any]], let text = blocks.first?["text"] as? String,
          let response = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] else {
        throw Failure("MCP did not return a usable response for \(tool)")
    }
    return response
}

private func position(_ element: AXUIElement) -> CGPoint? {
    guard let raw = attribute(element, kAXPositionAttribute),
          CFGetTypeID(raw) == AXValueGetTypeID() else { return nil }
    let value = raw as! AXValue
    var point = CGPoint.zero
    return AXValueGetValue(value, .cgPoint, &point) ? point : nil
}

private func config(_ directory: URL) -> [String: Any]? {
    guard let bytes = try? Data(contentsOf: directory.appendingPathComponent("workspace.json")) else { return nil }
    return (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any]
}

private struct FixtureReport {
    let root: String
    let scan: String
}

private func fixtureReport(_ lab: URL) -> FixtureReport? {
    let dirs = (try? FileManager.default.contentsOfDirectory(at: lab, includingPropertiesForKeys: nil)) ?? []
    guard let state = dirs.first(where: { $0.lastPathComponent.hasPrefix("Lumen-State-") }),
          let data = config(state), let roots = data["roots"] as? [String], roots.count == 1,
          let report = data["report"] as? [String: Any], let scan = report["scan_id"] as? String,
          let files = report["files"] as? [[String: Any]], !files.isEmpty else { return nil }
    let sample = URL(fileURLWithPath: roots[0]).resolvingSymlinksInPath()
    let tmp = lab.appendingPathComponent("tmp").resolvingSymlinksInPath()
    guard sample.deletingLastPathComponent().path == tmp.path, sample.lastPathComponent.hasPrefix("Lumen-Lab-") else { return nil }
    return FixtureReport(root: roots[0], scan: scan)
}

private func captureOverview(pid: pid_t, lab: URL, ax: AXUIElement) {
    guard CGPreflightScreenCaptureAccess() else {
        print("Overview PNG: skipped because Screen Recording permission is unavailable.")
        return
    }
    // A completed scan in the state directory can precede the SwiftUI redraw.
    let ready: Bool? = try? eventually("the populated overview", seconds: 10) {
        guard element("重新扫描", in: ax, button: true) != nil,
              element("审阅清理项目", in: ax, button: false) != nil else { return nil }
        return true
    }
    guard ready == true else {
        print("Overview PNG: skipped because the populated overview controls could not be verified.")
        return
    }
    captureWindow(pid: pid, lab: lab, filename: "Lumen-overview.png")
}

private func captureWindow(pid: pid_t, lab: URL, filename: String) {
    guard CGPreflightScreenCaptureAccess() else { return }
    // Select the one normal on-screen Lumen window belonging to this exact process.
    let candidates = (CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                      as? [[String: Any]] ?? []).filter {
        ($0[kCGWindowOwnerPID as String] as? Int32) == pid &&
        ($0[kCGWindowLayer as String] as? Int) == 0 &&
        ($0[kCGWindowName as String] as? String) == "既白 Lumen"
    }
    guard candidates.count == 1, let id = candidates[0][kCGWindowNumber as String] as? NSNumber,
          id.uint32Value != kCGNullWindowID else {
        print("Overview PNG: skipped because the Lumen window could not be uniquely identified.")
        return
    }
    let pending = lab.appendingPathComponent("capture.png")
    let capture = Process()
    capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    capture.arguments = ["-x", "-o", "-t", "png", "-l", id.stringValue, pending.path]
    do {
        try capture.run()
        capture.waitUntilExit()
        guard capture.terminationStatus == 0,
              let source = CGImageSourceCreateWithURL(pending as CFURL, nil),
              CGImageSourceGetType(source) as String? == "public.png",
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
              image.width >= 900, image.height >= 500 else {
            print("Overview PNG: skipped because the window capture did not produce a usable PNG.")
            return
        }
        try FileManager.default.moveItem(at: pending, to: lab.appendingPathComponent(filename))
        print("\(filename): captured the synthetic workspace (\(image.width) × \(image.height)).")
    } catch {
        print("Overview PNG: skipped because the capture failed: \(error.localizedDescription)")
    }
}

private func fallbackReport(_ pid: pid_t) {
    let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    let windows = info.filter {
        ($0[kCGWindowOwnerPID as String] as? Int32) == pid && ($0[kCGWindowLayer as String] as? Int) == 0
    }
    let title = windows.contains { ($0[kCGWindowName as String] as? String) == "既白 Lumen" }
    print("WindowServer visible app window: \(windows.isEmpty ? "unverified" : "verified").")
    print("Native window title: \(title ? "verified" : "unverified").")
    print(CGPreflightScreenCaptureAccess()
          ? "Overview PNG: skipped because the populated fixture overview could not be verified through AX."
          : "Overview PNG: skipped because Screen Recording permission is unavailable.")
    print("AX controls: skipped because the runner has no accessibility permission. Fixture-button, scope-list and rescan interactions were not tested.")
}

@main private struct HostedUITest {
    static func main() {
        do { try run() } catch {
            FileHandle.standardError.write(Data("\(error)\n".utf8))
            exit(1)
        }
    }

    static func run() throws {
        let env = ProcessInfo.processInfo.environment
        guard env["GITHUB_ACTIONS"] == "true", env["RUNNER_ENVIRONMENT"] == "github-hosted",
              env["RUNNER_OS"] == "macOS", CommandLine.arguments.count == 4,
              let pid = Int32(CommandLine.arguments[1]) else { throw Failure("Hosted macOS runner required") }
        let bundle = URL(fileURLWithPath: CommandLine.arguments[2]).resolvingSymlinksInPath()
        let lab = URL(fileURLWithPath: CommandLine.arguments[3])
        let state = lab.appendingPathComponent("workspace-state")
        let application = try eventually("the packaged app to finish launching") {
            guard let app = NSRunningApplication(processIdentifier: pid), app.isFinishedLaunching, !app.isTerminated,
                  app.bundleURL?.resolvingSymlinksInPath().path == bundle.path else { return nil as NSRunningApplication? }
            return app
        }
        let lock = state.appendingPathComponent(".service.lock").path
        _ = try eventually("the packaged Python bridge to open its isolated state") {
            FileManager.default.fileExists(atPath: lock) ? true : nil
        }
        guard (config(state)?["roots"] as? [String] ?? []).isEmpty else { throw Failure("Unexpected initial scan roots") }
        _ = try eventually("automatic inventory of the isolated Mac") {
            guard let data = config(state), let report = data["report"] as? [String: Any],
                  let apps = report["apps"] as? [[String: Any]], apps.contains(where: { $0["name"] as? String == "Sample Studio" }),
                  let projects = report["projects"] as? [String: [String]], projects.contains(where: {
                      $0.key.hasSuffix("/Studio Project") && $0.value == ["Amp", "Codex", "Windsurf"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Gemini Project") && $0.value == ["Conductor", "Gemini CLI", "Trae CLI", "Windsurf"]
                  }),
                  projects.values.contains(["Continue", "Kiro", "Kiro CLI", "Trae CLI", "TraeWork"]),
                  projects.contains(where: {
                      $0.key.hasSuffix("/work-mode-projects/6a4db2ef0f25b4742bf9cdc2") && $0.value == ["TraeWork"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Trae CN/ModularData/ai-agent/work-mode-projects/6a4db2ef0f25b4742bf9cdc8")
                          && $0.value == ["Trae"]
                  }),
                  !projects.keys.contains(where: {
                      $0.hasSuffix("/TRAE SOLO CN/ModularData/ai-agent/work-mode-projects/6a4db2ef0f25b4742bf9cdc8")
                  }),
                  projects.values.contains(["Aider", "Amp", "Trae", "Trae CLI"]),
                  projects.values.contains(["DeepSeek Harness"]),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Harness CLI Project") && $0.value == ["DeepSeek Harness"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Pi Project") && $0.value == ["Pi", "Trae CLI"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Kilo Project") && $0.value == ["Kilo Code CLI"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Crush Project") && $0.value == ["Crush"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Zed Project") && $0.value == ["Zed"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Hermes Project") && $0.value == ["Hermes Agent"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Vibe Project") && $0.value == ["Mistral Vibe"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Vibe Origin") && $0.value == ["Mistral Vibe"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Oh My Pi Project") && $0.value == ["Oh My Pi"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Oh My Pi Additional") && $0.value == ["Oh My Pi"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/Oh My Pi Historical") && $0.value == ["Oh My Pi"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/workspaces/0196af9c754f430799ef39aa868a6700")
                          && $0.value == ["OpenHands Agent Canvas"]
                  }),
                  projects.contains(where: {
                      $0.key.hasSuffix("/OpenHands CLI Project") && $0.value == ["OpenHands CLI"]
                  }),
                  let agents = report["agent_inventory"] as? [[String: Any]],
                  agents.contains(where: { row in
                      row["name"] as? String == "Gemini CLI" &&
                      (row["stores"] as? [[String: Any]])?.contains(where: { ($0["count"] as? Int ?? 0) > 0 }) == true
                  }),
                  agents.contains(where: { $0["name"] as? String == "Kiro" }),
                  agents.contains(where: {
                      $0["name"] as? String == "Kiro CLI" && $0["family"] as? String == "Kiro"
                          && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  agents.contains(where: { $0["name"] as? String == "Amp" && ($0["bytes"] as? Int ?? 0) > 0 }),
                  agents.contains(where: {
                      $0["name"] as? String == "Kilo Code CLI" && $0["family"] as? String == "Kilo Code"
                          && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "Kilo Code VS Code" && $0["family"] as? String == "Kilo Code"
                          && $0["installed"] as? Bool == true
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "Crush" && $0["kind"] as? String == "cli"
                          && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "Zed" && $0["kind"] as? String == "ide"
                          && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "Hermes Agent"
                          && ($0["stores"] as? [[String: Any]])?.contains(where: {
                              ($0["path"] as? String)?.hasSuffix("/.hermes/profiles/coder") == true
                                  && ($0["bytes"] as? Int ?? 0) > 0
                          }) == true
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "Mistral Vibe" && $0["kind"] as? String == "cli"
                          && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "Oh My Pi" && $0["kind"] as? String == "cli"
                          && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "OpenHands Agent Canvas" && $0["family"] as? String == "OpenHands"
                          && $0["installed"] as? Bool == true && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "Trae" && $0["installed"] as? Bool == true
                          && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "TraeWork" && $0["family"] as? String == "Trae"
                          && $0["installed"] as? Bool == true
                          && ($0["stores"] as? [[String: Any]])?.contains(where: {
                              ($0["path"] as? String)?.hasSuffix("/ai-agent/vm") == true
                                  && ($0["count"] as? Int ?? 0) > 0
                          }) == true
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "Trae CLI" && $0["family"] as? String == "Trae"
                          && ($0["stores"] as? [[String: Any]])?.contains(where: {
                              ($0["path"] as? String)?.hasSuffix("/archived_sessions") == true
                                  && ($0["count"] as? Int ?? 0) > 0
                          }) == true
                  }),
                  let files = report["files"] as? [[String: Any]], files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("0762b2f839b1.jsonl.zst") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["agent_store_path"] as? String)?.hasSuffix("/.trae/cli/state_5.sqlite") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["agent_store_path"] as? String)?.hasSuffix("/.trae/cli/thread_history_1.sqlite") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("/Library/Caches/coco/history.jsonl") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["agent_store_path"] as? String)?.hasSuffix("/Library/Caches/coco/sessions") == true
                          && ($0["path"] as? String)?.hasSuffix("/events.jsonl") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("/6a4db2ef0f25b4742bf9cdc2/delivery.html") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("/TRAE_PROJECT_MOVED_6a4db2ef0f25b4742bf9cdc8.md") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("/kilo/kilo.db") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("/Crush Project/.crush/crush.db") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("/Zed/threads/threads.db") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("/.hermes/profiles/coder/state.db") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("/d4679b0e-ea09-47a9-8944-7aae071b12c2/CURRENT") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("_019c625b-b900-7000-8000-000000000001.jsonl") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("_019c625b-b900-7000-8000-000000000002.jsonl.gz") == true
                          && ($0["agent_store_path"] as? String)?.hasSuffix("/coder/agent/archive/sessions") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  files.contains(where: {
                      ($0["path"] as? String)?.hasSuffix("/868ce027f0444dbdb1bddb264d48a88b/base_state.json") == true
                          && $0["decision"] as? String == "keep"
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "Conductor" && $0["kind"] as? String == "workspace-manager"
                          && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  agents.contains(where: {
                      $0["name"] as? String == "DeepSeek Harness" && $0["installed"] as? Bool == true
                          && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  agents.contains(where: { $0["name"] as? String == "Pi" && ($0["bytes"] as? Int ?? 0) > 0 }),
                  agents.contains(where: {
                      $0["name"] as? String == "Windsurf" && $0["installed"] as? Bool == true
                          && ($0["bytes"] as? Int ?? 0) > 0
                  }),
                  let modelInventory = report["model_inventory"] as? [String: Any],
                  let models = modelInventory["models"] as? [[String: Any]],
                  models.contains(where: { $0["tool"] as? String == "LM Studio" &&
                      $0["quantization"] as? String == "Q4_K_M" }),
                  let categories = report["categories"] as? [[String: Any]], categories.count >= 3,
                  let disk = report["disk_usage"] as? [String: Any],
                  let capacity = disk["total_bytes"] as? Int64, let free = disk["free_bytes"] as? Int64,
                  let used = disk["used_bytes"] as? Int64, used == capacity - free,
                  let diskCategories = disk["categories"] as? [[String: Any]],
                  diskCategories.contains(where: { $0["id"] as? String == "unclassified" }),
                  (data["roots"] as? [String])?.isEmpty == true else { return nil as Bool? }
            return true
        }
        print("Packaged app inventoried a migrated TRAE Work project and retained the standalone migration marker.")
        print("Packaged app attributed a Kilo Code project, recognized the VS Code extension and retained the database.")
        print("Packaged app discovered an unregistered Crush project and retained its session database.")
        print("Packaged app attributed a local Zed project and kept the native conversation database.")
        print("Packaged app attributed a Hermes profile workspace and kept the session database.")
        print("Packaged app attributed both Mistral Vibe session folders and retained the generation pointer.")
        print("Packaged app attributed both Oh My Pi workspace folders and retained the profile session.")
        print("Packaged app attributed the Oh My Pi cold archive and retained it in the profile archive store.")
        print("Packaged app inventoried OpenHands Agent Canvas and CLI projects and retained CLI conversation state.")
        guard AXIsProcessTrusted() else {
            fallbackReport(pid)
            return
        }
        let ax = AXUIElementCreateApplication(application.processIdentifier)
        AXUIElementSetMessagingTimeout(ax, 2)
        var availableWindows: CFTypeRef?
        if AXUIElementCopyAttributeValue(ax, kAXWindowsAttribute as CFString, &availableWindows) == .apiDisabled {
            fallbackReport(pid)
            return
        }
        _ = try eventually("the native window title") { window(ax) }
        _ = try eventually("the storage chart legend") { element("存储构成", in: ax, button: false) }
        try press("查看未归类占用", in: ax)
        _ = try eventually("the unclassified storage page") { element("未归类占用明细", in: ax, button: false) }
        _ = try eventually("the sidebar remains available") { element("应用排行", in: ax, button: true) }
        _ = try eventually("the supplementary disk scan") { element("本次补查到的文件", in: ax, button: false) }
        _ = try eventually("the remaining unattributed storage") { element("仍未归因", in: ax, button: false) }
        try press("PDF", in: ax)
        let pdfFolder = (lab.appendingPathComponent("home/Documents/Breakdown").path as NSString).abbreviatingWithTildeInPath
        _ = try eventually("the directory for the expanded PDF category") {
            element(pdfFolder, in: ax, button: false)
        }
        captureWindow(pid: pid, lab: lab, filename: "Lumen-unclassified-breakdown.png")
        try press("返回空间总览", in: ax)
        _ = try eventually("back on the overview") { element("存储构成", in: ax, button: false) }
        try press("应用排行", in: ax)
        _ = try eventually("the installed application row") { element("Sample Studio", in: ax, button: false) }
        _ = try eventually("the application Finder action") { element("打开目录", in: ax, button: true) }
        try press("Sample Studio 清理详情", in: ax)
        _ = try eventually("the selected application's analysis action") { element("分析应用缓存", in: ax, button: true) }
        try press("分析应用缓存", in: ax)
        _ = try eventually("an analyzed sandbox cache candidate") { element("preview-cache.bin", in: ax, button: false) }
        captureWindow(pid: pid, lab: lab, filename: "Lumen-app-cleanup.png")
        try press("返回应用排行", in: ax)
        try press("Chromium Fixture 清理详情", in: ax)
        try press("分析应用缓存", in: ax)
        _ = try eventually("only the verified generated-code entry to enter review") {
            guard let report = config(state)?["report"] as? [String: Any],
                  let scanned = report["files"] as? [[String: Any]] else { return nil as Bool? }
            let browser = scanned.filter { ($0["path"] as? String)?.contains("/Chromium/Default/Code Cache/") == true }
            let reviewable = browser.filter { $0["decision"] as? String == "review" }
            let protected = browser.filter { $0["decision"] as? String == "keep" }
            return browser.count == 3 && reviewable.count == 1
                && reviewable.first?["cleanup_kind"] as? String == "compiled_code"
                && protected.contains(where: { ($0["path"] as? String)?.hasSuffix("/index") == true })
                && protected.contains(where: { $0["cache_backend"] as? String == "persistent" }) ? true : nil
        }
        _ = try eventually("the generated-code cache candidate in the Chromium detail") {
            element("ab1234567890cdef_0", in: ax, button: false)
        }
        try press("保留或暂缓 2", in: ax)
        _ = try eventually("the Persistent Cache label in the protected tab") {
            element("持久化代码缓存", in: ax, button: false)
        }
        print("Chromium cleanup detail: a verified generated-code entry is reviewable; the cache index and Persistent Cache database are protected.")
        try press("返回应用排行", in: ax)
        try press("Agent 工作区", in: ax)
        _ = try eventually("the automatically detected project row") { element("Studio Project", in: ax, button: false) }
        _ = try eventually("the automatically detected Gemini workspace") { element("Gemini Project", in: ax, button: false) }
        _ = try eventually("the DeepSeek Harness workspace") { element("Harness Project", in: ax, button: false) }
        _ = try eventually("the DeepSeek Harness unregistered CLI workspace") {
            element("Harness CLI Project", in: ax, button: false)
        }
        _ = try eventually("the Pi workspace") { element("Pi Project", in: ax, button: false) }
        _ = try eventually("the workspace cleanup icon") { element("审阅工程清理项目", in: ax, button: true) }
        try press("Studio Project", in: ax)
        _ = try eventually("three agents sharing the same native workspace") {
            element("关联 Agent（3）", in: ax, button: false)
        }
        _ = try eventually("the expanded project storage categories") { element("数据类别", in: ax, button: false) }
        captureWindow(pid: pid, lab: lab, filename: "Lumen-agent-sweep.png")
        try press("模型存储", in: ax)
        try press("模型详情", in: ax)
        _ = try eventually("the model preservation verdict") { element("保留模型", in: ax, button: false) }
        try press("完成", in: ax)
        try press("fixture/managed", in: ax)
        try press("检查固定修订", in: ax)
        try press("保存恢复凭据", in: ax)
        _ = try eventually("the saved pinned model in the isolated state") {
            let rows = config(state)?["model_recovery_recipes"] as? [String: [String: Any]]
            return rows?.values.contains(where: { $0["filename"] as? String == "example.safetensors" }) == true ? true : nil
        }
        try press("完成", in: ax)
        captureWindow(pid: pid, lab: lab, filename: "Lumen-model-inventory.png")
        try press("大文件清理", in: ax)
        let nodes = ["Documents", "Library", ".cache", "Downloads", "Movies", "Pictures"]
        _ = try eventually("space map tiles in multiple rows and columns") {
            let coordinates = nodes.compactMap { element($0, in: ax, button: true).flatMap(position) }
            guard coordinates.count == nodes.count, let first = coordinates.first,
                  coordinates.contains(where: { abs($0.y - first.y) > 20 }),
                  coordinates.contains(where: { abs($0.x - first.x) > 20 }) else { return nil as Bool? }
            return true
        }
        captureWindow(pid: pid, lab: lab, filename: "Lumen-large-files.png")
        try press("Documents", in: ax)
        try press("Codex", in: ax)
        try press("Studio Project", in: ax)
        _ = try eventually("the zero-byte file in the same space distribution") { element("empty-marker", in: ax, button: true) }
        _ = try eventually("the long-named file in the same space distribution") {
            element("long-shoot-name-with-color-reference-and-export-notes-2026.txt", in: ax, button: true)
        }
        _ = try eventually("the nested project's file") { element("notes.txt", in: ax, button: true) }
        try press("notes.txt", in: ax)
        _ = try eventually("the selected file's cleanup action") { element("审阅此文件", in: ax, button: true) }
        print("Space Browser: multi-row geometry, three directory levels, small and zero-byte files, and file selection verified with AX.")
        try press("扫描范围", in: ax)
        try press("允许本地 MCP", in: ax)
        _ = try eventually("MCP opt-in saved to the isolated workspace") {
            config(state)?["mcp_enabled"] as? Bool == true ? true : nil
        }
        let summary = try mcpCall(bundle: bundle, tool: "lumen_summary")
        guard let snapshot = summary["snapshot"] as? String,
              let candidates = summary["candidates"] as? [[String: Any]],
              let alias = candidates.first(where: { ($0["size"] as? NSNumber)?.intValue == 49_152 })?["alias"] as? String else {
            throw Failure("No opaque MCP alias for the isolated download")
        }
        let draft = try mcpCall(bundle: bundle, tool: "lumen_create_draft",
                                arguments: ["snapshot": snapshot, "aliases": [alias]])
        guard let draftID = draft["draft_id"] as? String, let token = draft["status_token"] as? String else {
            throw Failure("MCP draft had no status credentials")
        }
        try press("刷新 MCP 草案", in: ax)
        try press("审阅 MCP 草案", in: ax)
        _ = try eventually("the original filename in the native approval sheet") { element("archive.zip", in: ax, button: false) }
        try press("批准并隔离", in: ax)
        try press("完成", in: ax)
        let outcome = try mcpCall(bundle: bundle, tool: "lumen_draft_status",
                                  arguments: ["draft_id": draftID, "status_token": token])
        let download = lab.appendingPathComponent("home/Downloads/archive.zip")
        guard outcome["status"] as? String == "completed",
              outcome["quarantined"] as? Int == 1, outcome["skipped"] as? Int == 0,
              !FileManager.default.fileExists(atPath: download.path) else {
            throw Failure("Native MCP approval did not quarantine exactly one synthetic file: \(outcome)")
        }
        print("MCP draft: opaque alias, native approval of the exact file, and token-scoped quarantine outcome verified.")
        try press("空间总览", in: ax)
        try press("创建演练样本", in: ax)
        let initial = try eventually("the fixture scan in a separate state directory") { fixtureReport(lab) }
        captureOverview(pid: pid, lab: lab, ax: ax)
        try press("扫描范围", in: ax)
        _ = try eventually("the fixture path in the scope list") { element(initial.root, in: ax, button: false) }
        try press("重新扫描", in: ax)
        _ = try eventually("a new scan ID for the original fixture root") {
            guard let fresh = fixtureReport(lab), fresh.root == initial.root, fresh.scan != initial.scan else { return nil as Bool? }
            return true
        }
        try press("返回本机工作区", in: ax)
        _ = try eventually("the local workspace home") { element("创建演练样本", in: ax, button: true) }
        guard (config(state)?["roots"] as? [String] ?? []).isEmpty else { throw Failure("Fixture changed primary scope") }
        print("Native window title, fixture creation, scope list, rescan, and return to the original workspace: verified with AX.")
    }
}
