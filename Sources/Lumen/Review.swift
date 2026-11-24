import SwiftUI
import AppKit
import Quartz

struct BatchPreview: Identifiable {
    let id = UUID()
    let action: String
    let files: [Entry]
    let batch: String?
    var scanID: String = ""
}

struct PreviewSheet: View {
    let preview: BatchPreview
    let commit: () -> Void
    @Environment(\.dismiss) private var dismiss
    var explanation: String {
        switch preview.action {
        case "clean": return "直接删除所列文件。执行前引擎将复核条件，清理后无法通过既白恢复。"
        case "quarantine": return "移动到同卷隔离区，本步释放 0 B。请回到工程验证预览与构建，再决定恢复或清空。"
        case "trash": return "移入废纸篓，暂不回收卷空间。"
        case "purge": return "永久移除批次内仍处于隔离或废纸篓状态的文件，此操作无法撤回。"
        case "restore_beside": return "恢复所列文件。原位置被占用时，为恢复的文件选择同目录下的新名称。"
        default: return "将仍在批次中的文件恢复到原位置。若原位置已被占用，请检查记录中的原因。"
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("操作预演 · " + (actions[preview.action] ?? preview.action)).font(.title2.bold())
            Text(explanation)
            Text("\(preview.files.count) 个文件 · 逻辑大小 \(bytes(preview.files.reduce(0) { $0 + $1.size }))").foregroundStyle(lilac)
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(preview.files) { file in
                        Text(file.path).bold().textSelection(.enabled)
                        Text([file.reason, file.owner, file.raw.paths("agents").joined(separator: "、"), file.raw.paths("projects").joined(separator: "、")].filter { !$0.isEmpty }.joined(separator: " · ")).foregroundStyle(.secondary)
                        Divider()
                    }
                }
            }
            HStack { Spacer(); Button("取消") { dismiss() }; Button("确认" + (actions[preview.action] ?? "")) { dismiss(); commit() }.buttonStyle(.borderedProminent) }
        }.padding(24).frame(width: 600, height: 460).preferredColorScheme(.dark)
    }
}

struct ReviewView: View {
    @ObservedObject var store: Store
    @Binding var owner: String
    @Binding var project: String
    @Binding var verdict: String
    @Binding var bucketPaths: Set<String>?
    @State private var query = ""
    @State private var kind = ""
    @State private var age = "不限时间"
    @State private var minimumSize = "不限大小"
    @State private var selectedTask = ""
    @State private var sorting = "大小"
    @State private var duplicatesOnly = false
    @State private var residualsOnly = false
    @State private var selection = Set<String>()
    @State private var detail: Entry?
    @State private var preview: BatchPreview?
    @State private var limit = 100

    var matches: [Entry] {
        let days = age == "超过 30 天" ? 30 : 90
        let cutoff = Int64((Date().timeIntervalSince1970 - Double(days * 86400)) * 1_000_000_000)
        let minimum: Int64 = minimumSize == "≥ 100 MB" ? 100_000_000 : (minimumSize == "≥ 1 GB" ? 1_000_000_000 : 0)
        let task = store.data.rows("tasks").first { $0.string("id") == selectedTask }
        let taskPaths = Set(["inputs","outputs","temporary"].flatMap { task?.paths($0) ?? [] })
        return store.files.filter { f in
            (owner.isEmpty || f.owner == owner || f.raw.string("app_path") == owner) && (bucketPaths?.contains(f.path) ?? (project.isEmpty || f.raw.paths("projects").contains(project)))
            && (verdict.isEmpty || f.decision == verdict) && (kind.isEmpty || f.raw.string("kind") == kind)
            && f.size >= minimum && (age == "不限时间" || f.raw.number("mtime_ns") < cutoff)
            && (selectedTask.isEmpty || taskPaths.contains(f.path))
            && (!residualsOnly || f.raw["residual"] as? Bool == true)
            && (!duplicatesOnly || !f.raw.string("duplicate_group").isEmpty)
            && (query.isEmpty || f.path.localizedCaseInsensitiveContains(query))
        }.sorted { lhs, rhs in
            if duplicatesOnly && lhs.raw.string("duplicate_group") != rhs.raw.string("duplicate_group") {
                return lhs.raw.string("duplicate_group") < rhs.raw.string("duplicate_group")
            }
            if sorting == "大小", lhs.size != rhs.size { return lhs.size > rhs.size }
            if sorting == "修改时间", lhs.raw.number("mtime_ns") != rhs.raw.number("mtime_ns") {
                return lhs.raw.number("mtime_ns") > rhs.raw.number("mtime_ns")
            }
            return lhs.path.localizedStandardCompare(rhs.path) == .orderedAscending
        }
    }

    var body: some View {
        let rows = matches
        let chosen = rows.filter { selection.contains($0.path) }
        let markedSafe = rows.filter { $0.decision == "safe" }
        return VStack(alignment: .leading, spacing: 12) {
            Panel {
                HStack {
                    TextField("搜索文件名或路径", text: $query).textFieldStyle(.roundedBorder)
                    Picker("决策", selection: $verdict) {
                        Text("全部").tag("")
                        ForEach(verdicts.keys.sorted(), id: \.self) { Text(verdicts[$0] ?? $0).tag($0) }
                    }
                    Picker("类型", selection: $kind) {
                        Text("全部").tag("")
                        ForEach(Set(store.files.map { $0.raw.string("kind") }.filter { !$0.isEmpty }).sorted(), id: \.self) { Text($0).tag($0) }
                    }
                }
                HStack {
                    Toggle("仅重复件", isOn: $duplicatesOnly)
                    Toggle("卸载残留", isOn: $residualsOnly)
                    Picker("排序", selection: $sorting) { ForEach(["大小","修改时间","路径"], id: \.self) { Text($0) } }.frame(width: 152)
                    Spacer()
                    Button("选择低影响项") { selection = Set(markedSafe.map(\.path)) }.disabled(markedSafe.isEmpty)
                    Button("取消选择") { selection.removeAll() }
                }
                DisclosureGroup("更多筛选") {
                    HStack {
                        Picker("大小", selection: $minimumSize) { ForEach(["不限大小","≥ 100 MB","≥ 1 GB"], id: \.self) { Text($0) } }
                        Picker("修改日期", selection: $age) { ForEach(["不限时间","超过 30 天","超过 90 天"], id: \.self) { Text($0) } }
                        Picker("任务", selection: $selectedTask) {
                            Text("全部").tag("")
                            ForEach(Array(store.data.rows("tasks").enumerated()), id: \.offset) { pair in
                                let item = pair.element
                                Text(item.string("agent") + " · " + URL(fileURLWithPath: item.string("root")).lastPathComponent).tag(item.string("id"))
                            }
                        }
                    }
                }
                if !owner.isEmpty || !project.isEmpty {
                    HStack { Text(owner + project).lineLimit(1); Button("清除筛选") { owner = ""; project = ""; bucketPaths = nil } }
                }
            }
            Panel {
                HStack {
                    Text("已选 \(chosen.count) / \(rows.count) 项").bold()
                    Spacer()
                    ForEach(["clean","quarantine","trash"], id: \.self) { action in
                        Button(actions[action] ?? "") { preview = BatchPreview(action: action, files: chosen, batch: nil, scanID: store.data.string("scan_id")) }
                            .disabled(chosen.isEmpty || !store.busy.isEmpty || (action == "clean" && chosen.contains { $0.decision != "safe" }))
                    }
                }
                if rows.isEmpty { Text("当前条件下没有文件。").foregroundStyle(.secondary) }
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(rows.prefix(limit)) { file in
                        HStack {
                            Toggle("", isOn: Binding(get: { selection.contains(file.path) }, set: {
                                if $0 { selection.insert(file.path) } else { selection.remove(file.path) }
                            })).labelsHidden().disabled(!["safe", "review"].contains(file.decision)).accessibilityLabel("选择 \(file.name)")
                            Button {
                                detail = file
                            } label: {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(file.name).fontWeight(.medium)
                                    Text(file.path).font(.caption).lineLimit(1).truncationMode(.middle).foregroundStyle(.secondary)
                                }.frame(maxWidth: .infinity, alignment: .leading)
                            }.buttonStyle(.plain)
                            Text(verdicts[file.decision] ?? file.decision).foregroundStyle(lilac)
                            Text(bytes(file.size)).monospacedDigit().frame(width: 72, alignment: .trailing)
                        }
                        Divider()
                    }
                }
                if rows.count > limit { Button("显示更多文件") { limit += 100 } }
            }
        }
        .onAppear { if verdict == "safe" { selection = Set(markedSafe.map(\.path)) } }
        .onChange(of: store.generation) { _ in selection.removeAll() }
        .sheet(item: $detail) { file in FileDetail(store: store, file: file) }
        .sheet(item: $preview) { plan in
            PreviewSheet(preview: plan) {
                store.call("run", ["action": plan.action, "paths": plan.files.map(\.path), "scan_id": plan.scanID])
            }
        }
    }
}

struct FileDetail: View {
    @ObservedObject var store: Store
    var file: Entry
    @State var showPreview = false
    @Environment(\.dismiss) var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(file.name).font(.title2.bold())
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(file.path)
                    Text("\(bytes(file.size)) · \(verdicts[file.decision] ?? file.decision) · \(file.raw.string("kind"))").foregroundStyle(lilac)
                    Text(file.reason)
                    if !file.owner.isEmpty { Text("归属应用 · " + file.owner) }
                    ForEach(["projects","agents","refs","shared_owners","model_versions"], id: \.self) { key in
                        let labels = ["projects":"关联工程", "agents":"Agent", "refs":"引用位置","shared_owners":"共享使用方","model_versions":"模型版本"]
                        if !file.raw.paths(key).isEmpty {
                            Text(labels[key] ?? key).bold()
                            Text(file.raw.paths(key).joined(separator: "\n"))
                        }
                    }
                    if let copies = store.duplicates[file.raw.string("duplicate_group")] {
                        Text("相同内容 · \(copies.count) 份").bold()
                        ForEach(copies, id: \.self) { copy in Text(copy) }
                    }
                }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            HStack {
                if ["audio","video","image"].contains(file.raw.string("kind")) {
                    Button("快速查看") { showPreview = true }
                }
                Button("在 Finder 中显示") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: file.path)]) }
                ForEach(["keep","exclude"], id: \.self) { key in
                    Button(key == "keep" ? "长期保留" : "排除文件") {
                        dismiss()
                        store.call("settings", [key: Array(Set(store.data.paths(key) + [file.path])).sorted()])
                    }.disabled(!store.busy.isEmpty)
                }
                Spacer(); Button("关闭") { dismiss() }
            }
        }.padding(24).frame(width: 620, height: 440)
        .sheet(isPresented: $showPreview) {
            VStack { MediaPreview(path: file.path); Button("关闭预览") { showPreview = false } }
                .padding().frame(width: 660, height: 480)
        }
    }
}

struct HistoryView: View {
    @ObservedObject var store: Store
    @State var preview: BatchPreview?
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            TrashView(store: store)
            if store.data.rows("history").isEmpty {
                Panel { Text("暂无操作记录").font(.headline); Text("完成清理或隔离后，可在这里查看逐项结果和可恢复批次。").foregroundStyle(.secondary) }
            }
            ForEach(store.data.rows("history"), id: \.historyKey) { batch in
                let items = batch.rows("items")
                let pending = items.filter { ["quarantined", "trashed"].contains($0.string("status")) }.map(Entry.init)
                Panel {
                    HStack {
                        Text(actions[batch.string("action")] ?? batch.string("action")).font(.headline)
                        Spacer(); Text(batch.string("created_at")).font(.caption).foregroundStyle(.secondary)
                    }
                    Text("批次 " + batch.historyKey).font(.caption).textSelection(.enabled)
                    BatchMetrics(data: batch)
                    if !pending.isEmpty {
                        HStack {
                            ForEach(["restore","restore_beside","purge"], id: \.self) { action in
                                Button(actions[action] ?? "") { preview = BatchPreview(action: action, files: pending, batch: batch.historyKey) }
                            }
                        }.disabled(!store.busy.isEmpty)
                    }
                    DisclosureGroup("逐项结果（\(items.count)）") {
                        ForEach(Array(items.enumerated()), id: \.offset) { pair in
                            let row = pair.element
                            let states = ["cleaned":"已直接移除","quarantined":"已隔离","trashed":"已移入废纸篓","restored":"已恢复","purged":"已清空","skipped":"已跳过"]
                            VStack(alignment: .leading) {
                                Text(row.string("path")).textSelection(.enabled)
                                Text([states[row.string("status")] ?? row.string("status"),row.string("reason")].filter { !$0.isEmpty }.joined(separator: " · ")).foregroundStyle(.secondary)
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 3)
                        }
                    }
                }
            }
        }.sheet(item: $preview) { plan in PreviewSheet(preview: plan) {
            if let batch = plan.batch {
                let beside = plan.action == "restore_beside"
                store.call(beside ? "restore" : plan.action, ["batch_id": batch, "beside": beside])
            }
        }}
    }
}

extension Dictionary where Key == String, Value == Any {
    var historyKey: String { self["batch_id"] as? String ?? string("id") }
}

struct MediaPreview: NSViewRepresentable {
    let path: String
    func makeNSView(context: Context) -> QLPreviewView {
        let view = QLPreviewView(frame: .zero, style: .normal)!
        view.autostarts = false
        view.previewItem = NSURL(fileURLWithPath: path)
        return view
    }
    func updateNSView(_ nsView: QLPreviewView, context: Context) {}
}
