import SwiftUI

struct UnclassifiedUsageDetail: View {
    @ObservedObject var store: Store
    let overview: Object?
    @Environment(\.dismiss) private var dismiss
    @State private var tab = "目录"

    var body: some View {
        let details = store.diskUsage?["unclassified_details"] as? Object
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("未归类占用明细").font(.title2.weight(.semibold))
                    Text("按文件实际分配空间统计")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            if store.busy == "scan_disk_usage" {
                HStack {
                    ProgressView().controlSize(.small)
                    Text("正在读取启动盘目录…")
                    Spacer()
                    Button("停止盘点") { store.cancelDiskUsage() }
                }
            }
            if let error = store.diskUsageError {
                Text(error).font(.callout).foregroundStyle(.secondary)
            }
            if let details {
                breakdown(details)
            } else if store.busy != "scan_disk_usage" {
                Panel {
                    Text(bytes(overview?.rows("categories").first { $0.string("id") == "unclassified" }?.number("bytes") ?? 0))
                        .font(.title2.monospacedDigit())
                    if store.needsDiskAccess {
                        Text("开启完全磁盘访问权限后，可查看目录细分。")
                            .foregroundStyle(.secondary)
                        Button("打开系统设置") { store.openDiskAccessSettings() }
                    } else {
                        Text("开始盘点后可查看补查出的目录和类别。")
                            .foregroundStyle(.secondary)
                        Button("盘点启动盘分类") { store.call("scan_disk_usage") }
                            .disabled(!store.busy.isEmpty)
                    }
                }
            }
        }
        .padding(24)
        .frame(width: 640)
        .background(canvas)
    }

    private func breakdown(_ details: Object) -> some View {
        let found = details.number("found_bytes")
        let remaining = details.number("remaining_bytes")
        let locations = details.rows("locations")
        return VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 28) {
                metric("本次补查到的文件", amount: found)
                metric("仍未归因", amount: remaining)
                Spacer()
                Button("重新盘点") { store.call("scan_disk_usage") }
                    .disabled(!store.busy.isEmpty || store.needsDiskAccess)
            }
            if remaining > 0 {
                Text("剩余空间可能来自 APFS 快照、共享空间、其他卷或系统未开放的目录。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let gaps = store.diskUsage?.number("gaps"), gaps > 0 {
                Text("有 \(gaps.formatted()) 处位置因权限、排除规则或卷边界跳过。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if store.diskUsage?["allocation_overlaps"] as? Bool == true {
                Text("APFS 共享块可能计入多个文件，分配量可能超过启动盘实际占用。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Picker("查看方式", selection: $tab) {
                Text("按目录").tag("目录")
                Text("按类别").tag("类别")
            }.pickerStyle(.segmented).labelsHidden().frame(width: 180)
            if found == 0 {
                ContentPlaceholder(icon: "folder", title: "本次没有补查到文件",
                                   message: "其余占用以磁盘容量差额显示。")
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        if tab == "目录" {
                            ForEach(locations, id: \.detailPath) { item in
                                let path = item.string("path")
                                HStack(spacing: 12) {
                                    Image(systemName: "folder").foregroundStyle(lilac)
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(displayPath(path))
                                            .lineLimit(1).truncationMode(.middle).help(path)
                                        Text("\(item.number("count").formatted()) 项").font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                    Spacer(minLength: 10)
                                    Text(bytes(item.number("bytes"))).monospacedDigit()
                                    FinderButton(path: path)
                                }.padding(.vertical, 10).padding(.horizontal, 12)
                                Divider()
                            }
                        } else {
                            ForEach(details.rows("categories"), id: \.detailCategory) { item in
                                let category = InventoryCategory(id: item.string("id"), amount: item.number("bytes"))
                                HStack(spacing: 12) {
                                    Circle().fill(category.color).frame(width: 8, height: 8)
                                    Text(category.label)
                                    Spacer()
                                    Text(bytes(category.amount)).monospacedDigit()
                                }.padding(12)
                                Divider()
                            }
                        }
                    }
                }
                .frame(height: 290)
                .background(surface, in: RoundedRectangle(cornerRadius: 12))
            }
        }
    }

    private func metric(_ title: String, amount: Int64) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(bytes(amount)).font(.title2.weight(.semibold)).monospacedDigit()
        }
    }
}

private extension Object {
    var detailPath: String { string("path") }
    var detailCategory: String { string("id") }
}
