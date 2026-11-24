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

    init(_ raw: Object) {
        path = raw.string("path")
        amount = raw.number("bytes")
        children = raw.rows("children").map(SpaceNode.init)
        gaps = raw.number("gaps")
    }
}

struct SpaceBrowser: View {
    @ObservedObject var store: Store
    let canvasHeight: CGFloat
    let review: (String, Bool) -> Void
    @State private var trail: [String] = []
    @State private var current: SpaceLevel?
    @State private var levels: [String: SpaceLevel] = [:]
    @State private var selection: String?
    @State private var limit = 72
    @State private var filter = "全部"
    @State private var mode: SpaceDisplay = .map
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
                    Picker("排列", selection: $mode) {
                        ForEach(SpaceDisplay.allCases) { item in Text(item.rawValue).tag(item) }
                    }.pickerStyle(.segmented).labelsHidden().frame(width: 119)
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
                if rows.isEmpty {
                    ContentPlaceholder(icon: "folder", title: "当前没有匹配的项目", message: "调整筛选或选择其他目录。")
                } else if mode == .map && rows.contains(where: { $0.amount > 0 }) {
                    ProportionalMap(nodes: rows, selected: selection, open: open, clean: clean) {
                        mode = .grid
                    }
                    .frame(height: canvasHeight)
                    .accessibilityLabel("空间分布")
                    Text("面积表示已分配空间。").font(.caption).foregroundStyle(.secondary)
                } else {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 116), spacing: 6)], spacing: 6) {
                        ForEach(rows.prefix(limit)) { node in
                            SpaceTileView(node: node, amount: node.amount, name: node.name, selected: selection == node.id,
                                          size: nil, open: { open(node) }, clean: { clean(node) })
                                .frame(height: 104)
                        }
                    }
                    if rows.count > limit { Button("显示更多项目") { limit += 72 } }
                }
                if let selection, let node = rows.first(where: { $0.id == selection }) {
                    HStack(spacing: 8) {
                        Label(node.name, systemImage: node.icon).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Text(bytes(node.amount)).monospacedDigit().foregroundStyle(.secondary)
                        FinderButton(path: node.id, reveal: true)
                        CleanupIcon(title: "审阅此文件") { clean(node) }.disabled(!store.busy.isEmpty || node.link)
                    }.font(.callout).padding(.top, 2)
                }
            }
            .padding(15).frame(maxWidth: .infinity, alignment: .leading)
            .background(surface, in: RoundedRectangle(cornerRadius: 14))
            .disabled(!store.busy.isEmpty)
        }
        .onChange(of: filter) { _ in limit = 72 }
        .onChange(of: mode) { _ in limit = 72 }
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
                            if siblings { trail.removeLast() }
                            navigate(folder)
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
        limit = 72
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

private enum SpaceDisplay: String, CaseIterable, Identifiable {
    case map = "占用图"
    case grid = "网格"
    var id: Self { self }
}

private struct ProportionalMap: View {
    let nodes: [SpaceNode]
    let selected: String?
    let open: (SpaceNode) -> Void
    let clean: (SpaceNode) -> Void
    let showGrid: () -> Void

    private struct Tile: Identifiable {
        let node: SpaceNode?
        let bytes: Int64
        let name: String
        var id: String { node?.id ?? "" }
    }

    private struct Placed: Identifiable {
        let tile: Tile
        let rect: CGRect
        var id: String { tile.id }
    }

    var body: some View {
        let visible = nodes.lazy.filter { $0.amount > 0 }.prefix(24)
        let otherAmount = nodes.reduce(0) { $0 + $1.amount } - visible.reduce(0) { $0 + $1.amount }
        let tiles = Array(visible.map { Tile(node: $0, bytes: $0.amount, name: $0.name) })
        let displayed = otherAmount > 0
            ? (tiles + [Tile(node: nil, bytes: otherAmount, name: "其余项目")]).sorted { $0.bytes > $1.bytes }
            : tiles
        return GeometryReader { proxy in
            ForEach(layout(displayed, in: CGRect(origin: .zero, size: proxy.size))) { piece in
                let box = piece.rect.insetBy(dx: 3, dy: 3)
                let node = piece.tile.node
                if box.width >= 10 && box.height >= 10 {
                    SpaceTileView(node: node, amount: piece.tile.bytes, name: piece.tile.name,
                                  selected: node?.id == selected && node != nil, size: box.size,
                                  open: { if let node { open(node) } else { showGrid() } },
                                  clean: { if let node { clean(node) } })
                        .position(x: box.midX, y: box.midY)
                }
            }
        }.accessibilityElement(children: .contain)
    }

    // Choose each strip by minimizing its worst rectangle aspect ratio.
    private func layout(_ tiles: [Tile], in bounds: CGRect) -> [Placed] {
        let total = tiles.reduce(CGFloat(0)) { $0 + CGFloat($1.bytes) }
        guard total > 0, bounds.width > 0, bounds.height > 0 else { return [] }
        let scale = bounds.width * bounds.height / total
        var remaining = bounds
        var index = 0
        var placed: [Placed] = []
        while index < tiles.count && remaining.width > 0 && remaining.height > 0 {
            let vertical = remaining.width >= remaining.height
            let side = vertical ? remaining.height : remaining.width
            let firstArea = CGFloat(tiles[index].bytes) * scale
            var lastArea = firstArea
            var area = firstArea
            var end = index + 1
            func worstRatio(total: CGFloat, smallest: CGFloat) -> CGFloat {
                let square = (total / side) * (total / side)
                return max(firstArea / square, square / smallest)
            }
            while end < tiles.count {
                let next = CGFloat(tiles[end].bytes) * scale
                if worstRatio(total: area + next, smallest: next) > worstRatio(total: area, smallest: lastArea) { break }
                area += next
                lastArea = next
                end += 1
            }
            let thickness = min(area / side, vertical ? remaining.width : remaining.height)
            var offset: CGFloat = 0
            for tile in tiles[index..<end] {
                let length = CGFloat(tile.bytes) * scale / thickness
                let start = vertical
                    ? CGPoint(x: remaining.minX, y: remaining.minY + offset)
                    : CGPoint(x: remaining.minX + offset, y: remaining.minY)
                let size = vertical ? CGSize(width: thickness, height: length) : CGSize(width: length, height: thickness)
                placed.append(Placed(tile: tile, rect: CGRect(origin: start, size: size)))
                offset += length
            }
            if vertical {
                remaining.origin.x += thickness
                remaining.size.width -= thickness
            } else {
                remaining.origin.y += thickness
                remaining.size.height -= thickness
            }
            index = end
        }
        return placed
    }
}

private struct SpaceTileView: View {
    let node: SpaceNode?
    let amount: Int64
    let name: String
    let selected: Bool
    let size: CGSize?
    let open: () -> Void
    let clean: () -> Void
    @State private var hovered = false

    var body: some View {
        let compact = (size?.width ?? 120) < 115 || (size?.height ?? 104) < 88
        let hasLabel = (size?.width ?? 120) >= 45 && (size?.height ?? 104) >= 34
        let hasIcon = (size?.height ?? 104) >= 66
        let tint = node.map { $0.directory ? lilac : Color(red: 0.42, green: 0.78, blue: 0.80) } ?? .secondary
        let shape = RoundedRectangle(cornerRadius: 8)
        Button(action: open) {
            VStack(alignment: .leading, spacing: compact ? 3 : 6) {
                if hasIcon {
                    if let node, node.application {
                        Image(nsImage: NSWorkspace.shared.icon(forFile: node.id)).resizable()
                            .frame(width: compact ? 17 : 28, height: compact ? 17 : 28)
                    } else {
                        Image(systemName: node?.icon ?? "square.grid.2x2").font(.system(size: compact ? 12 : 20))
                            .foregroundStyle(tint)
                    }
                }
                if hasLabel {
                    Text(name).font(.system(size: compact ? 11 : 13, weight: .semibold))
                        .lineLimit(1).truncationMode(.middle)
                    if (size?.height ?? 104) >= 46 {
                        Text(bytes(amount) + (node?.partial == true ? "+" : ""))
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            }
            .padding(compact ? 7 : 12)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomLeading)
            .frame(width: size?.width, height: size?.height)
            .background(tint.opacity(selected ? 0.26 : hovered ? 0.18 : 0.10), in: shape)
            .overlay { shape.strokeBorder(tint.opacity(selected ? 0.75 : hovered ? 0.42 : 0.14)) }
            .clipShape(shape).contentShape(shape)
        }.buttonStyle(.plain)
            .onHover { hovered = $0 }
            .help("\(name) · \(bytes(amount))")
            .accessibilityLabel(name).accessibilityValue(bytes(amount))
            .contextMenu {
                if let node {
                    Button("在 Finder 中显示") {
                        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: node.id)])
                    }
                    Button("审阅清理项目", action: clean).disabled(node.link)
                }
            }
    }
}
