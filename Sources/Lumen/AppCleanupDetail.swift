import SwiftUI
import AppKit

struct CleanupIcon: View {
    let title: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: "broom").font(.system(size: 15))
                .frame(width: 25, height: 24)
        }.buttonStyle(.borderless).foregroundStyle(lilac)
            .help(title).accessibilityLabel(title)
    }
}

enum AppFileFilter: String, CaseIterable, Identifiable {
    case safe = "低影响", review = "待确认", protected = "保留或暂缓"
    var id: Self { self }
}

private struct AppCleanupFiles {
    var safe: [Entry] = []
    var review: [Entry] = []
    var protected: [Entry] = []

    init(_ entries: [Entry] = []) {
        for entry in entries {
            switch entry.decision {
            case "safe": safe.append(entry)
            case "review" where !entry.raw.string("cleanup_kind").isEmpty: review.append(entry)
            default: protected.append(entry)
            }
        }
        safe.sort { $0.allocated > $1.allocated }
        review.sort { $0.allocated > $1.allocated }
        protected.sort { $0.allocated > $1.allocated }
    }

    subscript(_ filter: AppFileFilter) -> [Entry] {
        switch filter {
        case .safe: return safe
        case .review: return review
        case .protected: return protected
        }
    }
}

struct AppCleanupDetail: View {
    @ObservedObject var store: Store
    let app: RankedApp
    let back: () -> Void
    @State private var files = AppCleanupFiles()
    @State private var filtered: AppFileFilter = .safe
    @State private var selection = Set<String>()
    @State private var limit = 80
    @State private var loadedGeneration: Int?
    @State private var plan: BatchPreview?
    @State private var detail: Entry?

    var body: some View {
        let rows = files[filtered]
        let loaded = loadedGeneration == store.generation
        let ready = loaded && store.data.string("scan_id") == app.scanID && store.busy.isEmpty
        let selected = rows.filter { selection.contains($0.path) }
        VStack(alignment: .leading, spacing: 18) {
            Button(action: back) { Label("返回应用排行", systemImage: "chevron.left") }
                .buttonStyle(.link)
            Panel {
                HStack(spacing: 14) {
                    Image(nsImage: NSWorkspace.shared.icon(forFile: app.id)).resizable()
                        .frame(width: 50, height: 50)
                    VStack(alignment: .leading, spacing: 5) {
                        Text(app.app.string("name")).font(.system(size: 25, weight: .semibold))
                        Text(displayPath(app.id)).font(.caption).foregroundStyle(.secondary)
                            .lineLimit(1).truncationMode(.middle)
                    }
                    Spacer(minLength: 12)
                    FinderButton(path: app.id, reveal: true)
                    Button { store.call("scan_app", ["path": app.id]) } label: {
                        Label("分析应用缓存", systemImage: "viewfinder")
                    }.disabled(!ready || store.fixtureState != nil)
                }
                Divider()
                HStack(spacing: 24) {
                    metric("已归属占用", bytes(app.size))
                    metric("低影响可清理", bytes(app.safe))
                    metric("待确认缓存与临时文件", bytes(files.review.reduce(0) { $0 + $1.allocated }))
                }
            }
            HStack {
                Picker("清理分类", selection: $filtered) {
                    ForEach(AppFileFilter.allCases) { filter in
                        Text("\(filter.rawValue) \(files[filter].count.formatted())").tag(filter)
                    }
                }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 380)
                Spacer()
                if filtered == .safe && !rows.isEmpty {
                    Button("全选低影响项") { selection = Set(rows.map(\.path)) }
                }
            }
            if store.busy == "scan_app" {
                ProgressView("正在分析 Cache、日志和应用容器…").controlSize(.small)
            }
            if !loaded || rows.isEmpty {
                ContentPlaceholder(icon: "broom", title: !loaded ? "正在整理文件" : emptyTitle,
                                   message: emptyMessage)
            } else {
                Panel {
                    if filtered == .review {
                        Text("请先退出应用。选中的文件会移入隔离区，可在清理记录中恢复。")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(rows.prefix(limit)) { row in fileRow(row) }
                    }
                    if rows.count > limit { Button("显示更多") { limit += 80 } }
                }
            }
            if filtered != .protected && !rows.isEmpty {
                HStack {
                    Text("已选 \(selected.count.formatted()) 项 · \(bytes(selected.reduce(0) { $0 + $1.allocated }))")
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button(filtered == .safe ? "审阅低影响清理" : "审阅移入隔离区") {
                        plan = BatchPreview(action: filtered == .safe ? "clean" : "quarantine",
                                            files: selected, batch: nil, scanID: app.scanID)
                    }.buttonStyle(.borderedProminent).disabled(selected.isEmpty || !ready)
                }
            }
        }
        .task(id: store.generation) {
            let entries = store.inventory.appFiles[app.id] ?? []
            let next = await Task.detached(priority: .userInitiated) { AppCleanupFiles(entries) }.value
            guard !Task.isCancelled else { return }
            if loadedGeneration == nil || (filtered == .safe && next.safe.isEmpty && !next.review.isEmpty) {
                filtered = next.safe.isEmpty && !next.review.isEmpty ? .review : .safe
            }
            files = next
            selection.removeAll()
            loadedGeneration = store.generation
        }
        .onChange(of: filtered) { _ in selection.removeAll(); limit = 80 }
        .sheet(item: $detail) { FileDetail(store: store, file: $0) }
        .sheet(item: $plan) { batch in
            PreviewSheet(preview: batch) {
                store.call("run", ["paths": batch.files.map(\.path), "action": batch.action, "scan_id": batch.scanID])
            }
        }
    }

    private var emptyTitle: String {
        switch filtered {
        case .safe: return "没有已验证的低影响项目"
        case .review: return "没有待确认的缓存或临时文件"
        case .protected: return "没有保留或暂缓的关联文件"
        }
    }

    private var emptyMessage: String {
        if loadedGeneration != store.generation { return "正在按用途归类已索引的文件。" }
        switch filtered {
        case .safe: return "可以查看待确认项目。匹配应用版本及占用检查的文件才会列为低影响。"
        case .review: return "可通过「分析应用缓存」检查当前应用的沙盒缓存与临时目录。"
        case .protected: return "已识别的配置、用户数据和使用中的文件会在这里显示。"
        }
    }

    private func metric(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(size: 20, weight: .semibold, design: .rounded)).monospacedDigit()
        }
    }

    @ViewBuilder private func fileRow(_ row: Entry) -> some View {
        HStack(spacing: 12) {
            if filtered != .protected {
                Toggle(row.name, isOn: Binding(get: { selection.contains(row.path) }, set: {
                    if $0 { selection.insert(row.path) } else { selection.remove(row.path) }
                })).labelsHidden()
            }
            Button { detail = row } label: {
                VStack(alignment: .leading, spacing: 4) {
                    Text(row.name).font(.callout.weight(.semibold)).lineLimit(1)
                    Text(row.reason.isEmpty ? displayPath(row.path) : row.reason)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.buttonStyle(.plain)
            VStack(alignment: .trailing, spacing: 3) {
                Text(bytes(row.allocated)).monospacedDigit().font(.callout)
                if let type = ["cache": "缓存", "logs": "日志", "temporary": "临时文件", "build": "构建缓存"][row.raw.string("cleanup_kind")] {
                    Text(type).font(.caption).foregroundStyle(.secondary)
                }
            }
            FinderButton(path: row.path, reveal: true)
        }.padding(.vertical, 9)
        Divider()
    }
}
