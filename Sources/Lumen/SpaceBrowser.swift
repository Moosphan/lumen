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
    var icon: String { directory ? "folder" : link ? "link" : "doc" }
}

private struct SpaceLevel {
    let path: String
    let amount: Int64
    let children: [SpaceNode]
    let gaps: Int64

    init(_ raw: Object) {
        path = raw.string("path")
        amount = raw.number("bytes")
        children = raw.rows("children").map(SpaceNode.init)
        gaps = raw.number("gaps")
    }
}

struct SpaceBrowser: View {
    @ObservedObject var store: Store
    let review: (String, Bool) -> Void
    @State private var trail: [String] = []
    @State private var current: SpaceLevel?
    @State private var levels: [String: SpaceLevel] = [:]
    @State private var selection: String?
    @State private var limit = 80
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
                                trail = Array(trail.prefix(index + 1))
                                load(path)
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
                summary(current)
                directoryList(current)
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

    private func summary(_ level: SpaceLevel) -> some View {
        Panel {
            HStack(alignment: .firstTextBaseline) {
                Text("空间分布").font(.headline)
                Spacer()
                Text(bytes(level.amount)).font(.system(size: 24, weight: .semibold, design: .rounded)).monospacedDigit()
                Text("\(level.children.count.formatted()) 项").font(.caption).foregroundStyle(.secondary)
            }
            if level.gaps > 0 {
                Label("\(level.gaps.formatted()) 处未能读取或位于其他卷", systemImage: "info.circle")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if level.amount > 0 {
                ProportionalMap(nodes: level.children, total: level.amount, selected: selection, select: open)
                    .frame(height: 208)
            } else {
                Text("当前目录没有可计量的文件占用。").font(.callout).foregroundStyle(.secondary)
            }
            if let selection, let file = level.children.first(where: { $0.id == selection }) {
                HStack(spacing: 10) {
                    Label(file.name, systemImage: file.icon).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Text(bytes(file.amount)).monospacedDigit().foregroundStyle(.secondary)
                    FinderButton(path: file.id, reveal: true)
                    CleanupIcon(title: "审阅此文件") { clean(file) }.disabled(!store.busy.isEmpty || file.link)
                }.font(.callout).padding(.top, 4)
            }
        }
    }

    private func directoryList(_ level: SpaceLevel) -> some View {
        let rows = level.children.filter { filter == "全部" || $0.directory == (filter == "文件夹") }
        return Panel {
            HStack {
                Text("此层目录").font(.headline)
                Spacer()
                Picker("显示", selection: $filter) {
                    ForEach(["全部", "文件夹", "文件"], id: \.self) { Text($0) }
                }.pickerStyle(.segmented).labelsHidden().frame(width: 175)
            }
            if rows.isEmpty { Text("此层没有符合条件的项目。").foregroundStyle(.secondary) }
            LazyVStack(spacing: 0) {
                ForEach(rows.prefix(limit)) { node in
                    Divider()
                    row(node, total: level.amount)
                }
            }
            if rows.count > limit { Button("显示更多") { limit += 80 } }
        }
    }

    private func row(_ node: SpaceNode, total: Int64) -> some View {
        HStack(spacing: 10) {
            Button { open(node) } label: {
                HStack(spacing: 10) {
                    Image(systemName: node.icon).foregroundStyle(lilac).frame(width: 20)
                    Text(node.name).font(.callout.weight(.medium)).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 6)
                    Text(bytes(node.amount) + (node.partial ? "+" : "")).monospacedDigit()
                    Text((Double(node.amount) / Double(max(1, total))).formatted(.percent.precision(.fractionLength(1))))
                        .font(.caption.monospacedDigit()).foregroundStyle(.secondary).frame(width: 52, alignment: .trailing)
                    Image(systemName: node.directory ? "chevron.right" : "circle.dotted")
                        .font(.caption).foregroundStyle(.secondary).frame(width: 14)
                }.contentShape(Rectangle())
            }.buttonStyle(.plain).disabled(node.link || !store.busy.isEmpty).accessibilityLabel(node.name)
            Divider().frame(height: 20)
            FinderButton(path: node.id, reveal: true)
            CleanupIcon(title: node.directory ? "审阅目录内的清理项目" : "审阅此文件") {
                clean(node)
            }.disabled(node.link || !store.busy.isEmpty)
        }
        .padding(.vertical, 7).padding(.horizontal, 5)
        .background(selection == node.id ? lilac.opacity(0.07) : .clear, in: RoundedRectangle(cornerRadius: 5))
    }

    private func shortName(_ path: String) -> String {
        if path == "/System/Volumes/Data" { return "启动盘数据卷" }
        return URL(fileURLWithPath: path).lastPathComponent
    }

    private func loadRoot(_ path: String?) {
        trail = []
        levels.removeAll()
        load(path, refresh: true)
    }

    private func goBack() {
        trail.removeLast()
        load(trail.last)
    }

    private func navigate(_ node: SpaceNode) {
        trail.append(node.id)
        load(node.id)
    }

    private func open(_ node: SpaceNode) {
        guard store.busy.isEmpty else { return }
        if node.directory { navigate(node) }
        else { selection = node.id }
    }

    private func load(_ path: String?, refresh: Bool = false) {
        guard store.busy.isEmpty else { return }
        selection = nil
        limit = 80
        filter = "全部"
        if !refresh, let path, let cached = levels[path] {
            current = cached
            return
        }
        current = nil
        store.call("size_tree", path.map { ["path": $0] } ?? [:]) { value in
            let level = SpaceLevel(value)
            if trail.last != level.path { trail = [level.path] }
            if levels.count >= 16 { levels.removeAll(keepingCapacity: true) }
            levels[level.path] = level
            current = level
        }
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

private struct ProportionalMap: View {
    let nodes: [SpaceNode]
    let total: Int64
    let selected: String?
    let select: (SpaceNode) -> Void

    private struct Tile: Identifiable {
        let node: SpaceNode?
        let bytes: Int64
        var id: String { node?.id ?? "" }
    }

    private struct Placed: Identifiable {
        let tile: Tile
        let rect: CGRect
        var id: String { tile.id }
    }

    var body: some View {
        let first = nodes.filter { $0.amount > 0 }.prefix(9)
        let remainder = total - first.reduce(0) { $0 + $1.amount }
        let tiles = first.map { Tile(node: $0, bytes: $0.amount) } + (remainder > 0 ? [Tile(node: nil, bytes: remainder)] : [])
        return GeometryReader { proxy in
            ForEach(layout(tiles[...], in: CGRect(origin: .zero, size: proxy.size))) { piece in
                let box = piece.rect.insetBy(dx: 2, dy: 2)
                if box.width > 0 && box.height > 0 {
                    tile(piece.tile, width: box.width, height: box.height)
                        .position(x: box.midX, y: box.midY)
                }
            }
        }
    }

    private func tile(_ tile: Tile, width: CGFloat, height: CGFloat) -> some View {
        let node = tile.node
        let name = node?.name ?? "其余项目"
        let color = node.map { $0.directory ? lilac : Color(red: 0.38, green: 0.79, blue: 0.84) } ?? Color.secondary
        let shape = RoundedRectangle(cornerRadius: min(10, min(width, height)/4))
        return Button {
            if let node { select(node) }
        } label: {
            VStack(alignment: .leading, spacing: 5) {
                if width > 76 && height > 50 {
                    Image(systemName: node?.icon ?? "square.grid.2x2").foregroundStyle(color)
                    Text(name).font(.callout.weight(.semibold)).lineLimit(1).truncationMode(.middle)
                    if height > 70 {
                        Text(bytes(tile.bytes)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
            }
            .padding(10).frame(width: width, height: height, alignment: .bottomLeading)
            .background(color.opacity(node?.id == selected ? 0.31 : 0.14), in: shape)
            .contentShape(shape)
        }.buttonStyle(.plain)
            .disabled(node == nil).help("\(name) · \(bytes(tile.bytes))")
            .accessibilityLabel("\(name)，\(bytes(tile.bytes))")
    }

    private func layout(_ tiles: ArraySlice<Tile>, in rect: CGRect) -> [Placed] {
        guard let first = tiles.first else { return [] }
        if tiles.count == 1 { return [Placed(tile: first, rect: rect)] }
        let sum = tiles.reduce(Int64(0)) { $0 + $1.bytes }
        var count = 0
        var leading: Int64 = 0
        for tile in tiles.dropLast() {
            leading += tile.bytes
            count += 1
            if leading >= sum / 2 { break }
        }
        let ratio = CGFloat(leading) / CGFloat(sum)
        var front = rect
        var end = rect
        if rect.width >= rect.height {
            front.size.width *= ratio
            end.origin.x += front.width
            end.size.width -= front.width
        } else {
            front.size.height *= ratio
            end.origin.y += front.height
            end.size.height -= front.height
        }
        return layout(tiles.prefix(count), in: front) + layout(tiles.dropFirst(count), in: end)
    }
}
