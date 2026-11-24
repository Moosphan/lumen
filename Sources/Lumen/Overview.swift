import SwiftUI
import AppKit

struct InventoryCategory: Identifiable {
    let id: String
    let amount: Int64

    var label: String {
        switch id {
        case "apps": return "应用程序"
        case "agent": return "Agent 项目"
        case "library": return "缓存与资源库"
        case "downloads": return "下载"
        case "media": return "图片、音频与视频"
        case "models": return "本地模型"
        default: return "其他文件"
        }
    }
    var color: Color {
        switch id {
        case "apps": return Color(red: 0.63, green: 0.52, blue: 0.98)
        case "agent": return Color(red: 0.37, green: 0.78, blue: 0.91)
        case "library": return Color(red: 0.94, green: 0.62, blue: 0.72)
        case "downloads": return Color(red: 0.98, green: 0.74, blue: 0.48)
        case "media": return Color(red: 0.53, green: 0.80, blue: 0.62)
        case "models": return Color(red: 0.46, green: 0.60, blue: 0.95)
        default: return Color(white: 0.54)
        }
    }
}

struct SpaceOverview: View {
    @ObservedObject var store: Store
    let review: (String) -> Void
    let applications: () -> Void
    let workspaces: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var highlighted: String?
    @State private var disk: (total: Int64, free: Int64) = (0, 0)

    var body: some View {
        let categories = store.data.rows("categories").map { InventoryCategory(id: $0.string("id"), amount: $0.number("bytes")) }
        let total = store.data.number("indexed_bytes")
        let safe = store.inventory.safeCount
        let pending = store.inventory.reviewCount
        let current = categories.first { $0.id == highlighted }
        VStack(alignment: .leading, spacing: 20) {
            Panel {
                HStack {
                    Label("存储构成", systemImage: "chart.donut")
                        .font(.system(size: 15, weight: .semibold))
                    Spacer()
                    if total > 0 { Text("按已盘点数据计算").font(.caption).foregroundStyle(.secondary) }
                }
                if categories.isEmpty {
                    ContentPlaceholder(icon: "internaldrive", title: store.busy.isEmpty ? "暂无可显示的空间数据" : "正在盘点本机空间",
                                       message: store.busy.isEmpty ? "重新扫描后可查看各类文件的占用。" : "按应用、项目和文件类型汇总。")
                } else {
                    HStack(spacing: 26) {
                        ZStack {
                            Circle().stroke(.white.opacity(0.07), lineWidth: 16)
                            ForEach(Array(categories.enumerated()), id: \.element.id) { index, category in
                                let start = categories.prefix(index).reduce(Int64(0)) { $0 + $1.amount }
                                let end = start + category.amount
                                let fraction = Double(category.amount) / Double(total)
                                let gap = min(fraction * 0.15, 0.004)
                                Circle().trim(from: Double(start) / Double(total) + gap,
                                              to: Double(end) / Double(total) - gap)
                                    .stroke(category.color.opacity(highlighted == nil || highlighted == category.id ? 1 : 0.24),
                                            style: StrokeStyle(lineWidth: 16, lineCap: .butt))
                                    .rotationEffect(.degrees(-90))
                            }
                            VStack(spacing: 5) {
                                Text(bytes(current?.amount ?? total))
                                    .font(.system(size: 26, weight: .semibold, design: .rounded)).monospacedDigit()
                                    .lineLimit(1).minimumScaleFactor(0.68)
                                Text(current?.label ?? "已盘点占用").font(.caption).foregroundStyle(.secondary)
                            }.padding(16)
                        }
                        .frame(width: 168, height: 168).padding(10)
                        .accessibilityHidden(true)
                        VStack(spacing: 3) {
                            ForEach(categories) { category in
                                HStack(spacing: 8) {
                                    Circle().fill(category.color).frame(width: 8, height: 8)
                                    Text(category.label).lineLimit(1)
                                    Spacer(minLength: 6)
                                    Text(bytes(category.amount)).foregroundStyle(.secondary)
                                    Text((Double(category.amount) / Double(total)).formatted(.percent.precision(.fractionLength(1))))
                                        .frame(width: 54, alignment: .trailing)
                                }
                                .font(.system(size: 12)).monospacedDigit()
                                .padding(.vertical, 5).padding(.horizontal, 8)
                                .background(.white.opacity(highlighted == category.id ? 0.06 : 0), in: RoundedRectangle(cornerRadius: 6))
                                .onHover { hovered in
                                    withAnimation(reduceMotion ? nil : .easeOut(duration: 0.16)) { highlighted = hovered ? category.id : nil }
                                }
                                .accessibilityElement(children: .combine)
                            }
                        }
                    }.padding(.vertical, 8)
                }
                if disk.total > 0 {
                    Divider().padding(.top, 4)
                    HStack {
                        Label("启动盘容量", systemImage: "internaldrive").font(.caption.weight(.semibold))
                        Spacer()
                        Text("\(bytes(disk.free)) 可用 / \(bytes(disk.total))")
                            .font(.caption).monospacedDigit().foregroundStyle(.secondary)
                    }
                    ProportionBar(value: Double(max(0, disk.total - disk.free)) / Double(disk.total), color: lilac.opacity(0.7))
                }
            }
            Panel {
                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 7) {
                        Label("清理建议", systemImage: "sparkles").font(.headline)
                        Text(store.data.string("scan_id").isEmpty ? "完成扫描后会列出可审阅的项目。" :
                             "\(safe.formatted()) 项低影响 · \(pending.formatted()) 项待确认")
                            .foregroundStyle(.secondary).font(.callout)
                    }
                    Spacer()
                    if safe > 0 {
                        Button("低影响清理") { review("safe") }.buttonStyle(.bordered)
                    }
                    Button("审阅清理项目") { review(safe == 0 ? "review" : "") }
                        .buttonStyle(.borderedProminent).disabled(safe == 0 && pending == 0)
                }
            }
            HStack(spacing: 12) {
                Button(action: applications) {
                    overviewLink("应用排行", icon: "macwindow", count: "\(store.data.rows("apps").count.formatted()) 个应用")
                }
                Button(action: workspaces) {
                    overviewLink("Agent 工作区", icon: "square.3.layers.3d", count: "\(store.projects.count.formatted()) 个工程")
                }
            }.buttonStyle(.plain)
        }
        .task(id: store.generation) {
            let path = store.fixtureState == nil ? NSHomeDirectory() : store.roots.first ?? NSHomeDirectory()
            let attributes = try? FileManager.default.attributesOfFileSystem(forPath: path)
            disk = ((attributes?[.systemSize] as? NSNumber)?.int64Value ?? 0,
                    (attributes?[.systemFreeSize] as? NSNumber)?.int64Value ?? 0)
        }
    }

    private func overviewLink(_ title: String, icon: String, count: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon).foregroundStyle(lilac)
            Text(title).font(.callout.weight(.semibold))
            Spacer(minLength: 6)
            Text(count).font(.caption).foregroundStyle(.secondary)
            Image(systemName: "arrow.right").font(.caption).foregroundStyle(.secondary)
        }.padding(14).background(surface, in: RoundedRectangle(cornerRadius: 10))
    }
}

struct ProportionBar: View {
    let value: Double
    let color: Color
    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(.white.opacity(0.08))
                Capsule().fill(color).frame(width: geometry.size.width * min(max(value, 0), 1))
            }
        }.frame(height: 4).accessibilityHidden(true)
    }
}

struct ContentPlaceholder: View {
    let icon: String
    let title: String
    let message: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(title, systemImage: icon).font(.headline)
            Text(message).foregroundStyle(.secondary)
        }.padding(.vertical, 16).frame(maxWidth: .infinity, alignment: .leading)
    }
}

func displayPath(_ path: String) -> String {
    (path as NSString).abbreviatingWithTildeInPath
}

struct FinderButton: View {
    let path: String
    var reveal = false

    var body: some View {
        Button {
            let url = URL(fileURLWithPath: path)
            if reveal { NSWorkspace.shared.activateFileViewerSelecting([url]) }
            else { NSWorkspace.shared.open(url) }
        } label: {
            Image(systemName: reveal ? "location" : "folder")
                .frame(width: 25, height: 23)
        }.buttonStyle(.borderless)
            .help(reveal ? "在 Finder 中显示" : "打开目录")
            .accessibilityLabel(reveal ? "在 Finder 中显示" : "打开目录")
    }
}
