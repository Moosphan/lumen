import AppKit
import SwiftUI

struct ResultSheet: View {
    let report: ActionResult
    @Environment(\.dismiss) var dismiss
    var body: some View {
        let items = report.batches.flatMap { $0.rows("items") }
        let skipped = items.filter { $0.string("status") == "skipped" }
        return VStack(alignment: .leading, spacing: 14) {
            Text("逐项操作结果").font(.title2.bold())
            Text("\(items.count) 条记录 · 跳过 \(skipped.count) 项").foregroundStyle(lilac)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(report.batches.enumerated()), id: \.offset) { _, batch in
                        if let title = actions[batch.string("action")] { Text(title).font(.headline) }
                        BatchMetrics(data: batch)
                        ForEach(Array(batch.rows("items").enumerated()), id: \.offset) { _, item in
                            Text(item.string("path")).textSelection(.enabled)
                            Text([fileStates[item.string("status")] ?? item.string("status"), item.string("reason")]
                                .filter { !$0.isEmpty }.joined(separator: " · ")).foregroundStyle(.secondary)
                            Divider()
                        }
                    }
                }
            }
            HStack { Spacer(); Button("完成") { dismiss() }.keyboardShortcut(.defaultAction) }
        }.padding(24).frame(width: 610, height: 420)
    }
}

struct ConfirmRemoval: View {
    let title: String
    let explanation: String
    let paths: [String]
    let button: String
    let execute: () -> Void
    @Environment(\.dismiss) var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(title).font(.title2.bold())
            Text(explanation)
            ScrollView {
                LazyVStack(alignment: .leading) {
                    ForEach(paths, id: \.self) { path in Text(path).textSelection(.enabled); Divider() }
                }
            }
            HStack { Spacer(); Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button(button, role: .destructive) { dismiss(); execute() }.disabled(paths.isEmpty)
            }
        }.padding(24).frame(width: 600, height: 370)
    }
}

struct TrashVolume: Identifiable {
    let raw: Object
    let scanID: String
    var id: String { raw.string("root") }
    var items: [Object] { raw.rows("items") }
}

struct TrashView: View {
    @ObservedObject var store: Store
    @State private var volume: TrashVolume?
    var body: some View {
        Panel {
            HStack {
                Label("卷废纸篓", systemImage: "trash").font(.headline)
                Spacer()
                Button("授权废纸篓目录…") { store.authorizeTrash() }.disabled(store.fixtureState != nil)
                Button("读取废纸篓清单") { store.call("trash_list") }
            }
            ForEach(store.data.rows("trash_volumes").map { TrashVolume(raw: $0, scanID: store.data.string("scan_id")) }) { v in
                Divider()
                HStack {
                    VStack(alignment: .leading) {
                        Text(v.id).textSelection(.enabled)
                        Text(v.raw.string("error").isEmpty ? "\(v.items.count) 项" : v.raw.string("error")).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("审阅清空此卷…") { volume = v }.disabled(v.items.isEmpty || !v.raw.string("error").isEmpty)
                }
            }
        }.disabled(!store.busy.isEmpty).sheet(item: $volume) { v in
            TrashPreview(volume: v) { selected in
                store.call("empty_trash", ["paths": selected, "scan_id": v.scanID])
            }
        }
    }
}

struct BatchMetrics: View {
    let data: Object
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let processed = data["logical_processed_bytes"] as? NSNumber {
                Text("已处理逻辑大小 " + bytes(processed.int64Value))
            }
            if let delta = data["measured_free_delta"] as? NSNumber {
                Text("卷可用空间净变化 " + bytes(delta.int64Value))
            }
        }.font(.callout).foregroundStyle(.secondary)
    }
}

struct TrashPreview: View {
    let volume: TrashVolume
    let execute: ([String]) -> Void
    @State private var chosen = Set<String>()
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("审阅废纸篓").font(.title2.bold())
            Text(volume.id).textSelection(.enabled)
            Text("所选项目将被永久移除。目录可能包含多个文件，清空后无法通过既白恢复。")
            Button("选择本卷可处理项") {
                chosen = Set(volume.items.filter { $0.string("reason").isEmpty }.map { $0.string("path") })
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(volume.items.enumerated()), id: \.offset) { pair in
                        let item = pair.element
                        let path = item.string("path")
                        Toggle(isOn: Binding(get: { chosen.contains(path) }, set: {
                            if $0 { chosen.insert(path) } else { chosen.remove(path) }
                        })) {
                            VStack(alignment: .leading) {
                                Text(path).textSelection(.enabled)
                                Text(item.string("reason").isEmpty ? bytes(item.number("size")) : item.string("reason")).foregroundStyle(.secondary)
                            }
                        }.disabled(!item.string("reason").isEmpty)
                        Divider()
                    }
                }
            }
            HStack { Spacer(); Button("取消") { dismiss() }
                Button("确认永久移除 \\(chosen.count) 项", role: .destructive) { dismiss(); execute(chosen.sorted()) }.disabled(chosen.isEmpty)
            }
        }.padding(24).frame(width: 620, height: 440)
    }
}
