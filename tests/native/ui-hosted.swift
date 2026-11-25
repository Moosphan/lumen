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
                  let projects = report["projects"] as? [String: [String]], projects.values.contains(["Codex"]),
                  projects.values.contains(["Gemini CLI"]),
                  let agents = report["agent_inventory"] as? [[String: Any]],
                  agents.contains(where: { row in
                      row["name"] as? String == "Gemini CLI" &&
                      (row["stores"] as? [[String: Any]])?.contains(where: { ($0["count"] as? Int ?? 0) > 0 }) == true
                  }),
                  let categories = report["categories"] as? [[String: Any]], categories.count >= 3,
                  (data["roots"] as? [String])?.isEmpty == true else { return nil as Bool? }
            return true
        }
        print("Packaged app automatically inventoried an installed app, Codex and Gemini CLI projects, and a download.")
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
        try press("应用排行", in: ax)
        _ = try eventually("the installed application row") { element("Sample Studio", in: ax, button: false) }
        _ = try eventually("the application Finder action") { element("打开目录", in: ax, button: true) }
        try press("应用清理详情", in: ax)
        _ = try eventually("the selected application's analysis action") { element("分析应用缓存", in: ax, button: true) }
        try press("分析应用缓存", in: ax)
        _ = try eventually("an analyzed sandbox cache candidate") { element("preview-cache.bin", in: ax, button: false) }
        captureWindow(pid: pid, lab: lab, filename: "Lumen-app-cleanup.png")
        try press("返回应用排行", in: ax)
        try press("Agent 工作区", in: ax)
        _ = try eventually("the automatically detected project row") { element("Studio Project", in: ax, button: false) }
        _ = try eventually("the automatically detected Gemini workspace") { element("Gemini Project", in: ax, button: false) }
        _ = try eventually("the workspace cleanup icon") { element("审阅工程清理项目", in: ax, button: true) }
        captureWindow(pid: pid, lab: lab, filename: "Lumen-agent-sweep.png")
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
