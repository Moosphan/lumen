import SwiftUI
import AppKit

private struct SpaceNode: Identifiable {
    let raw: Object
    var id: String { raw.string("path") }
    var name: String { raw.string("name") }
    var amount: Int64 { raw.number("bytes") }
    var directory: Bool { raw["directory"] as? Bool == true }
    var partial: Bool { raw["partial"] as? Bool == true }
    var link: Bool { raw["link"] as? Bool == true }
    var application: Bool { directory && name.lowercased().hasSuffix(".app") }
    var icon: String { application ? "app" : directory ? "folder" : link ? "link" : "doc" }
}

private struct SpaceLevel {
    let path: String
    let amount: Int64
    let children: [SpaceNode]
    let gaps: Int64
    let indexedAt: Date?

    init(_ raw: Object) {
        path = raw.string("path")
        amount = raw.number("bytes")
        children = raw.rows("children").map(SpaceNode.init)
        gaps = raw.number("gaps")
        indexedAt = (raw["indexed_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
    }
}

struct SpaceBrowser: View {
    @ObservedObject var store: Store
    let canvasHeight: CGFloat
    let didOpenLevel: () -> Void
    let review: (String, Bool) -> Void
    @State private var trail: [String] = []
    @State private var current: SpaceLevel?
    @State private var levels: [String: SpaceLevel] = [:]
    @State private var selection: String?
    @State private var filter = "全部"
    @State private var plan: BatchPreview?
    @State private var enrollment: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 10) {
                Button { goBack() } label: { Image(systemName: "chevron.left") }
                    .disabled(trail.count < 2 || !store.busy.isEmpty).help("返回上一级")
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 7) {
                        ForEach(Array(trail.enumerated()), id: \.offset) { index, path in
                            if index > 0 { Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary) }
                            Button(shortName(path)) {
                                load(path, nextTrail: Array(trail.prefix(index + 1)))
                            }.buttonStyle(.plain)
                                .foregroundStyle(index == trail.count - 1 ? Color.primary : lilac)
                                .disabled(!store.busy.isEmpty)
                                .help(displayPath(path))
                        }
                    }.font(.callout.weight(.medium)).padding(.vertical, 4)
                }
                Spacer(minLength: 0)
                if store.busy == "size_tree" {
                    Button("停止") { store.cancelSpaceAnalysis() }
                } else {
                    Button { load(trail.last, refresh: true) } label: {
                        Image(systemName: "arrow.clockwise")
                    }.help("重新盘点此目录").disabled(!store.busy.isEmpty)
                }
                if store.fixtureState == nil {
                    Menu("位置") {
                        Button("个人目录") { loadRoot(nil) }
                        Button("应用程序") { loadRoot("/Applications") }
                        Button("启动盘数据卷") { loadRoot("/System/Volumes/Data") }
                        Button("选择目录或磁盘…") { chooseLocation() }
                    }.fixedSize().disabled(!store.busy.isEmpty)
                }
            }
            if let current {
                if store.busy == "size_tree" {
                    ProgressView("正在打开目录…").controlSize(.small).foregroundStyle(.secondary)
                }
                browser(current)
            } else if store.busy == "size_tree" {
                Panel {
                    ProgressView("正在统计目录与子目录的占用…").controlSize(.small)
                    Text("读取各级目录的大小和名称。").font(.callout).foregroundStyle(.secondary)
                }
            } else {
                ContentPlaceholder(icon: "chart.bar.xaxis", title: "查看占用空间最多的位置",
                                   message: "从个人目录开始，或选择任意可访问的目录和磁盘。")
                Button("盘点个人目录") { loadRoot(nil) }.buttonStyle(.borderedProminent).disabled(!store.busy.isEmpty)
            }
        }
        .task { if current == nil { loadRoot(nil) } }
        .onDisappear { store.cancelSpaceAnalysis() }
        .onChange(of: store.generation) { _ in
            levels.removeAll()
            load(trail.last, refresh: true)
        }
        .sheet(item: $plan) { batch in
            PreviewSheet(preview: batch) {
                store.call("run", ["paths": batch.files.map(\.path), "action": batch.action, "scan_id": batch.scanID])
            }
        }
        .confirmationDialog("加入清理审阅", isPresented: Binding(get: { enrollment != nil }, set: { if !$0 { enrollment = nil } }),
                            titleVisibility: .visible, presenting: enrollment) { path in
            Button("加入并扫描") { review(path, true) }
            Button("取消", role: .cancel) { }
        } message: { path in
            Text("将「\(displayPath(path))」加入扫描范围。扫描会检查文件归属及引用，再显示可审阅的项目。")
        }
    }

    private func browser(_ level: SpaceLevel) -> some View {
        let rows = level.children.filter { filter == "全部" || $0.directory == (filter == "文件夹") }
        let navigator = trail.dropLast().last.flatMap { levels[$0] } ?? level
        let folders = navigator.children.filter { $0.directory && !$0.application }
        return HStack(alignment: .top, spacing: 14) {
            directoryRail(level, folders: folders, siblings: navigator.path != level.path)
                .frame(width: 176)
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top, spacing: 10) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(shortName(level.path)).font(.system(size: 21, weight: .semibold))
                            .lineLimit(1).truncationMode(.middle).help(displayPath(level.path))
                        Text("\(rows.count.formatted()) 项 · \(bytes(rows.reduce(0) { $0 + $1.amount }))")
                            .font(.callout.monospacedDigit()).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 4)
                    Menu(filter) {
                        ForEach(["全部", "文件夹", "文件"], id: \.self) { category in
                            Button(category) { filter = category }
                        }
                    }.fixedSize().accessibilityLabel("筛选项目")
                }
                if level.gaps > 0 {
                    Label("\(level.gaps.formatted()) 处未能读取或位于其他卷", systemImage: "info.circle")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let selection, let node = rows.first(where: { $0.id == selection }) {
                    HStack(spacing: 8) {
                        Label(node.name, systemImage: node.icon).textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer()
                        FinderButton(path: node.id, reveal: true)
                        CleanupIcon(title: "审阅此文件") { clean(node) }.disabled(!store.busy.isEmpty || node.link)
                    }.font(.callout).padding(9).background(lilac.opacity(0.07), in: RoundedRectangle(cornerRadius: 8))
                }
                if rows.isEmpty {
                    ContentPlaceholder(icon: "folder", title: "当前没有匹配的项目", message: "调整筛选或选择其他目录。")
                } else {
                    HStack {
                        Text("空间分布").font(.callout.weight(.semibold))
                        Spacer()
                        if let time = level.indexedAt {
                            Label(time.formatted(date: .omitted, time: .shortened), systemImage: "clock")
                                .font(.caption).foregroundStyle(.secondary).help("占用快照时间，重新盘点可更新")
                        }
                    }.padding(.top, 2)
                    distribution(rows, total: level.amount)
                }
            }
            .padding(15).frame(maxWidth: .infinity, alignment: .leading)
            .background(surface, in: RoundedRectangle(cornerRadius: 14))
            .disabled(!store.busy.isEmpty)
        }
    }

    @ViewBuilder private func distribution(_ rows: [SpaceNode], total: Int64) -> some View {
        let featured = rows.count >= 6 ? 2 : 0
        if featured > 0 {
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible())], spacing: 8) {
                ForEach(rows.prefix(featured)) { node in tile(node, total: total, featured: true) }
            }
        }
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 145), spacing: 8)], spacing: 8) {
            ForEach(rows.dropFirst(featured)) { node in tile(node, total: total, featured: false) }
        }
    }

    private func tile(_ node: SpaceNode, total: Int64, featured: Bool) -> some View {
        SpaceTileView(node: node, share: Double(node.amount) / Double(max(1, total)), featured: featured,
                      selected: selection == node.id, open: { open(node) }, clean: { clean(node) })
    }

    private func directoryRail(_ level: SpaceLevel, folders: [SpaceNode], siblings: Bool) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(siblings ? "同级目录" : "目录").font(.callout.weight(.semibold)).padding(.horizontal, 8)
            if folders.isEmpty {
                Text("当前没有子目录").font(.caption).foregroundStyle(.secondary).padding(.horizontal, 8)
            }
            ScrollView {
                LazyVStack(spacing: 3) {
                    ForEach(folders) { folder in
                        let selected = level.path == folder.id
                        Button {
                            let parentTrail = siblings ? Array(trail.dropLast()) : trail
                            load(folder.id, nextTrail: parentTrail + [folder.id])
                        } label: {
                            HStack(spacing: 8) {
                                Image(systemName: selected ? "folder.fill" : "folder").foregroundStyle(lilac)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(folder.name).font(.callout.weight(.medium)).lineLimit(1).truncationMode(.middle)
                                    Text(bytes(folder.amount)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 0)
                                Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.secondary)
                            }
                            .padding(.vertical, 8).padding(.horizontal, 8)
                            .background(lilac.opacity(selected ? 0.14 : 0), in: RoundedRectangle(cornerRadius: 7))
                            .contentShape(Rectangle())
                        }.buttonStyle(.plain).disabled(selected || !store.busy.isEmpty)
                            .help(displayPath(folder.id)).accessibilityLabel("浏览目录 \(folder.name)")
                    }
                }
            }.frame(maxHeight: canvasHeight + 40)
        }.padding(9).padding(.vertical, 5)
            .background(surface, in: RoundedRectangle(cornerRadius: 12))
    }

    private func shortName(_ path: String) -> String {
        if path == "/System/Volumes/Data" { return "启动盘数据卷" }
        return URL(fileURLWithPath: path).lastPathComponent
    }

    private func loadRoot(_ path: String?) {
        levels.removeAll()
        load(path, nextTrail: [])
    }

    private func goBack() {
        let parentTrail = Array(trail.dropLast())
        load(parentTrail.last, nextTrail: parentTrail)
    }

    private func navigate(_ node: SpaceNode) {
        load(node.id, nextTrail: trail + [node.id])
    }

    private func open(_ node: SpaceNode) {
        guard store.busy.isEmpty else { return }
        if node.directory { navigate(node) }
        else { selection = node.id }
    }

    private func load(_ path: String?, refresh: Bool = false, nextTrail: [String]? = nil) {
        guard store.busy.isEmpty else { return }
        selection = nil
        let requestedTrail = nextTrail ?? trail
        if !refresh, let path, let cached = levels[path] {
            show(cached, trail: requestedTrail)
            return
        }
        var request: Object = ["refresh": refresh]
        if let path { request["path"] = path }
        store.call("size_tree", request) { value in
            let level = SpaceLevel(value)
            if refresh {
                levels = levels.filter { key, _ in key != level.path && !key.hasPrefix(level.path + "/") }
            }
            if levels.count >= 16, let expired = levels.keys.first(where: { !requestedTrail.contains($0) }) {
                levels.removeValue(forKey: expired)
            }
            levels[level.path] = level
            show(level, trail: requestedTrail)
        }
    }

    private func show(_ level: SpaceLevel, trail requestedTrail: [String]) {
        trail = requestedTrail.last == level.path ? requestedTrail : [level.path]
        current = level
        didOpenLevel()
    }

    private func chooseLocation() {
        let picker = NSOpenPanel()
        picker.title = "选择要盘点的目录或磁盘"
        picker.canChooseFiles = false
        picker.canChooseDirectories = true
        if picker.runModal() == .OK { loadRoot(picker.url?.resolvingSymlinksInPath().path) }
    }

    private func clean(_ node: SpaceNode) {
        if !node.directory, let entry = store.files.first(where: { $0.path == node.id }) {
            if ["safe", "review"].contains(entry.decision) {
                plan = BatchPreview(action: "quarantine", files: [entry], batch: nil, scanID: store.data.string("scan_id"))
            } else {
                review(node.id, false)
            }
        } else if store.files.contains(where: { $0.path.hasPrefix(node.id + "/") }) {
            review(node.id, false)
        } else if store.fixtureState == nil {
            let directory = node.directory ? node.id : URL(fileURLWithPath: node.id).deletingLastPathComponent().path
            guard directory != "/", !directory.hasPrefix("/System/"), !directory.hasPrefix("/Applications/") else {
                store.error = "此位置可查看占用和 Finder 路径。"
                return
            }
            enrollment = directory
        }
    }
}

private struct SpaceTileView: View {
    let node: SpaceNode
    let share: Double
    let featured: Bool
    let selected: Bool
    let open: () -> Void
    let clean: () -> Void
    @State private var hovered = false

    var body: some View {
        let tint = node.directory ? lilac : Color(red: 0.42, green: 0.78, blue: 0.80)
        let shape = RoundedRectangle(cornerRadius: 9)
        Button(action: open) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    if node.application {
                        Image(nsImage: NSWorkspace.shared.icon(forFile: node.id)).resizable().scaledToFit()
                            .frame(width: 22, height: 22)
                    } else {
                        Image(systemName: node.icon).font(.system(size: 16, weight: .medium))
                            .foregroundStyle(tint).frame(height: 22)
                    }
                    Spacer(minLength: 6)
                    Text(share.formatted(.percent.precision(.fractionLength(1))))
                        .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                if featured { Spacer(minLength: 4) }
                Text(node.name).font(.system(size: featured ? 16 : 13, weight: .semibold))
                    .fixedSize(horizontal: false, vertical: true)
                Text(bytes(node.amount) + (node.partial ? "+" : ""))
                    .font(.system(size: featured ? 16 : 12, weight: .medium).monospacedDigit())
                    .foregroundStyle(.secondary)
                ProportionBar(value: share, color: tint.opacity(0.85))
            }
            .padding(featured ? 14 : 11)
            .frame(maxWidth: .infinity, minHeight: featured ? 138 : 105, alignment: .topLeading)
            .background(tint.opacity(selected ? 0.26 : hovered ? 0.18 : featured ? 0.11 : 0.07), in: shape)
            .overlay { shape.strokeBorder(tint.opacity(selected ? 0.75 : hovered ? 0.42 : 0.15)) }
            .contentShape(shape)
        }.buttonStyle(.plain)
            .onHover { hovered = $0 }
            .help("\(displayPath(node.id)) · \(bytes(node.amount))")
            .accessibilityLabel(node.name).accessibilityValue(bytes(node.amount))
            .contextMenu {
                Button("在 Finder 中显示") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: node.id)])
                }
                Button("审阅清理项目", action: clean).disabled(node.link)
            }
    }
}
