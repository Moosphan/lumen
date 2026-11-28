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
        case "documents": return "文档"
        case "trash": return "废纸篓"
        case "macos": return "macOS"
        case "models": return "本地模型"
        case "unclassified": return "未归类占用"
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
        case "documents": return Color(red: 0.69, green: 0.81, blue: 0.97)
        case "trash": return Color(red: 0.74, green: 0.60, blue: 0.51)
        case "macos": return Color(red: 0.71, green: 0.69, blue: 0.81)
        case "models": return Color(red: 0.46, green: 0.60, blue: 0.95)
        case "unclassified": return Color(white: 0.33)
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
    var body: some View {
        let disk = store.fixtureState == nil
            ? (store.diskUsage ?? store.data["disk_usage"] as? Object) : nil
        let categories = (disk ?? store.data).rows("categories")
            .map { InventoryCategory(id: $0.string("id"), amount: $0.number("bytes")) }
        let chartTotal = categories.reduce(Int64(0)) { $0 + $1.amount }
        let total = disk?.number("used_bytes") ?? chartTotal
        let safe = store.inventory.safeCount
        let pending = store.inventory.reviewCount
        let current = categories.first { $0.id == highlighted }
        VStack(alignment: .leading, spacing: 20) {
            Panel {
                HStack {
                    Label("存储构成", systemImage: "chart.donut")
                        .font(.system(size: 15, weight: .semibold))
                    Spacer()
                    if store.busy == "scan_disk_usage" {
                        ProgressView().controlSize(.small)
                        Button("停止盘点") { store.cancelDiskUsage() }
                    } else if disk != nil && !store.needsDiskAccess {
                        Button("盘点启动盘分类") { store.call("scan_disk_usage") }
                            .disabled(!store.busy.isEmpty)
                    }
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
                                let fraction = Double(category.amount) / Double(chartTotal)
                                let gap = min(fraction * 0.15, 0.004)
                                Circle().trim(from: Double(start) / Double(chartTotal) + gap,
                                              to: Double(end) / Double(chartTotal) - gap)
                                    .stroke(category.color.opacity(highlighted == nil || highlighted == category.id ? 1 : 0.24),
                                            style: StrokeStyle(lineWidth: 16, lineCap: .butt))
                                    .rotationEffect(.degrees(-90))
                            }
                            VStack(spacing: 5) {
                                Text(bytes(current?.amount ?? total))
                                    .font(.system(size: 26, weight: .semibold, design: .rounded)).monospacedDigit()
                                    .lineLimit(1).minimumScaleFactor(0.68)
                                Text(current?.label ?? (disk == nil ? "已盘点占用" : "启动盘已用"))
                                    .font(.caption).foregroundStyle(.secondary)
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
                                    Text(share(category.amount, total: chartTotal))
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
                if let disk {
                    let capacity = disk.number("total_bytes")
                    let free = disk.number("free_bytes")
                    Divider().padding(.top, 4)
                    HStack {
                        Label("启动盘容量", systemImage: "internaldrive").font(.caption.weight(.semibold))
                        Spacer()
                        Text("\(bytes(free)) 可用 / \(bytes(capacity))")
                            .font(.caption).monospacedDigit().foregroundStyle(.secondary)
                    }
                    ProportionBar(value: Double(total) / Double(max(1, capacity)), color: lilac.opacity(0.7))
                    coverageNote(disk)
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
    }

    private func share(_ amount: Int64, total: Int64) -> String {
        let fraction = Double(amount) / Double(total)
        return fraction < 0.0005 ? "<0.1%" : fraction.formatted(.percent.precision(.fractionLength(1)))
    }

    private func coverageNote(_ disk: Object) -> some View {
        let classified = bytes(disk.number("classified_bytes"))
        let gaps = disk.number("gaps")
        let note: String
        if disk["allocation_overlaps"] as? Bool == true {
            note = "已归类 \(classified)。APFS 共享空间可能重复计量，比例按已归类空间计算。"
        } else if disk.string("coverage") == "disk" {
            note = "已归类 \(classified)。未归类占用可能包括快照、其他卷及未能读取的位置。"
        } else {
            note = "已归类 \(classified)。盘点启动盘分类可补查清理索引以外的位置。"
        }
        return Text(gaps > 0 ? "\(note) 跳过了 \(gaps.formatted()) 处位置。" : note)
            .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
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
