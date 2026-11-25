import AppKit
import SwiftUI

private struct MCPDraft: Identifiable {
    let raw: Object
    var id: String { raw.string("draft_id") }
    var items: [Object] { raw.rows("items") }
    var paths: [String] { items.map { $0.string("path") } }
    var expires: Date { Date(timeIntervalSince1970: Double(raw.number("expires_at"))) }
}

struct MCPCleanupPanel: View {
    @ObservedObject var store: Store
    @State private var drafts: [MCPDraft] = []
    @State private var selected: MCPDraft?
    @State private var copied = ""
    @State private var loaded = false

    private var enabled: Bool { store.data["mcp_enabled"] as? Bool == true }

    var body: some View {
        Panel {
            HStack {
                Label("本地 MCP", systemImage: "link").font(.headline)
                Spacer()
                Toggle("允许本地 MCP", isOn: Binding(get: { enabled }, set: { value in
                    store.call("settings", ["mcp_enabled": value]) { _ in refresh() }
                })).toggleStyle(.switch).labelsHidden().accessibilityLabel("允许本地 MCP")
            }
            Text("Agent 可读取脱敏汇总和提交草案。隔离文件需在既白逐项确认。")
                .font(.callout).foregroundStyle(.secondary)
            if enabled {
                HStack {
                    Button("刷新 MCP 草案") { refresh() }
                    Menu(copied.isEmpty ? "复制连接配置" : "已复制 \(copied)") {
                        Button("Codex 连接命令") { copyConfiguration(codex: true) }
                        Button("通用 MCP JSON") { copyConfiguration(codex: false) }
                    }.disabled(store.fixtureState != nil)
                }
                if loaded && drafts.isEmpty {
                    Text("暂无可审批的草案。").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(drafts) { draft in
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("\(draft.items.count) 项待审阅").font(.callout.weight(.medium))
                            Text("有效期至 " + draft.expires.formatted(date: .omitted, time: .shortened))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("拒绝") { store.call("mcp_decline", ["draft_id": draft.id]) { _ in refresh() } }
                        Button("审阅 MCP 草案") { selected = draft }
                            .disabled(draft.expires <= Date())
                    }
                }
            }
        }.disabled(!store.busy.isEmpty)
            .task { refresh() }
            .onChange(of: enabled) { _ in
                drafts = []
                loaded = false
            }
            .sheet(item: $selected) { draft in
                MCPApprovalSheet(draft: draft) {
                    store.call("mcp_approve", ["draft_id": draft.id, "paths": draft.paths]) { _ in refresh() }
                }
            }
    }

    private func refresh() {
        guard enabled else { return }
        store.call("mcp_pending") { response in
            drafts = response.rows("drafts").map(MCPDraft.init)
            loaded = true
        }
    }

    private func copyConfiguration(codex: Bool) {
        guard let script = resource("engine/mcp_server.py", source: "engine/mcp_server.py") else { return }
        if codex {
            let quotedPath = "'" + script.path.replacingOccurrences(of: "'", with: "'\\''") + "'"
            copy("codex mcp add lumen -- /usr/bin/python3 -I -B \(quotedPath)", label: "Codex 命令")
            return
        }
        let config = ["mcpServers": ["lumen": [
            "command": "/usr/bin/python3", "args": ["-I", "-B", script.path],
        ]]]
        guard let data = try? JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys]),
              let json = String(data: data, encoding: .utf8) else { return }
        copy(json, label: "MCP JSON")
    }

    private func copy(_ text: String, label: String) {
        NSPasteboard.general.clearContents()
        if NSPasteboard.general.setString(text, forType: .string) { copied = label }
    }
}

private struct MCPApprovalSheet: View {
    let draft: MCPDraft
    let approve: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("审阅 MCP 草案").font(.title2.weight(.semibold))
            Text("确认后，既白会复核这 \(draft.items.count) 个文件并移入可恢复隔离区。")
                .font(.callout).foregroundStyle(.secondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 9) {
                    ForEach(draft.items, id: \.mcpPath) { item in
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(URL(fileURLWithPath: item.string("path")).lastPathComponent).font(.callout.weight(.medium))
                                Text(displayPath(item.string("path"))).font(.caption).foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                            }
                            Spacer(minLength: 10)
                            Text(verdicts[item.string("decision")] ?? item.string("decision")).font(.caption)
                            Text(bytes(item.number("size"))).font(.caption.monospacedDigit())
                        }
                        Divider()
                    }
                }
            }.frame(height: 270)
            HStack {
                Spacer()
                Button("返回") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("批准并隔离") { dismiss(); approve() }.buttonStyle(.borderedProminent)
                    .disabled(draft.expires <= Date() || draft.paths.isEmpty)
            }
        }.padding(22).frame(width: 610)
    }
}

private extension Object {
    var mcpPath: String { string("path") }
}
