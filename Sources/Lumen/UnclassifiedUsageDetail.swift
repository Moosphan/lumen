import SwiftUI

struct UnclassifiedUsageDetail: View {
    @ObservedObject var store: Store
    @State private var byCategory = true
    @State private var collapsedGroups: Set<String> = []
    @State private var expandedTypes: Set<String> = []

    private var scanning: Bool { store.busy == "scan_disk_usage" }

    var body: some View {
        let details = store.diskUsage?["unclassified_details"] as? Object
        VStack(alignment: .leading, spacing: 18) {
            summary(details)
            if let details {
                if details.number("found_bytes") > 0 {
                    breakdown(details)
                } else {
                    ContentPlaceholder(icon: "folder", title: "本次没有补查到文件",
                                       message: "其余占用以启动盘容量差额显示。")
                }
                coverage(details)
            }
        }
    }

    private func summary(_ details: Object?) -> some View {
        let indexedUsage = store.data["disk_usage"] as? Object
        let unclassified = indexedUsage?.rows("categories").first { $0.string("id") == "unclassified" }?.number("bytes")
        return Panel {
            HStack(alignment: .center, spacing: 24) {
                if let details {
                    metric("本次补查到的文件", amount: details.number("found_bytes"))
                    Divider().frame(height: 34)
                    metric("仍未归因", amount: details.number("remaining_bytes"))
                } else if let unclassified {
                    metric("未归类占用", amount: unclassified)
                }
                Spacer(minLength: 12)
                if scanning {
                    Button("停止盘点") { store.cancelDiskUsage() }
                } else if store.needsDiskAccess {
                    Button("打开系统设置") { store.openDiskAccessSettings() }
                } else {
                    Button {
                        store.call("scan_disk_usage")
                    } label: {
                        Label(details == nil ? "盘点启动盘分类" : "重新盘点", systemImage: "arrow.clockwise")
                    }.buttonStyle(.bordered).disabled(!store.busy.isEmpty)
                }
            }
            if scanning {
                HStack(spacing: 9) {
                    ProgressView().controlSize(.small)
                    Text("正在读取启动盘目录…")
                }.font(.callout).foregroundStyle(.secondary)
            } else if let error = store.diskUsageError {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.callout).foregroundStyle(.secondary)
            } else if store.needsDiskAccess {
                Text("开启完全磁盘访问权限后可盘点更多目录。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            if let details {
                Text(details.number("remaining_bytes") > 0
                     ? "文件按实际分配空间统计。剩余占用可能来自 APFS 快照、其他卷、系统数据或无法读取的目录。"
                     : "文件按实际分配空间统计。")
                    .font(.caption).foregroundStyle(.secondary)
                if store.diskUsage?["allocation_overlaps"] as? Bool == true {
                    Text("APFS 共享块可能计入多个文件，分配量可能超过启动盘实际占用。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func breakdown(_ details: Object) -> some View {
        Panel {
            HStack {
                Label("占用细分", systemImage: "list.bullet.indent").font(.headline)
                Spacer()
                Picker("查看方式", selection: $byCategory) {
                    Text("按类别").tag(true)
                    Text("按目录").tag(false)
                }.pickerStyle(.segmented).labelsHidden().frame(width: 172)
            }
            LazyVStack(alignment: .leading, spacing: 0) {
                Divider()
                if byCategory {
                    ForEach(details.rows("categories"), id: \.detailCategory) { group in
                        categoryRows(group)
                        Divider()
                    }
                } else {
                    ForEach(details.rows("locations"), id: \.detailPath) { item in
                        locationRow(item, indent: 0)
                        Divider()
                    }
                }
            }
        }
    }

    private func categoryRows(_ group: Object) -> some View {
        let id = group.string("id")
        let category = InventoryCategory(id: id, amount: group.number("bytes"))
        let expanded = !collapsedGroups.contains(id)
        return VStack(alignment: .leading, spacing: 0) {
            expansionRow(category.label, item: group, expanded: expanded, color: category.color, indent: 0) {
                if expanded { collapsedGroups.insert(id) } else { collapsedGroups.remove(id) }
            }
            if expanded {
                ForEach(group.rows("subcategories"), id: \.detailCategory) { kind in
                    let type = kind.string("id")
                    let key = "\(id)/\(type)"
                    let showLocations = expandedTypes.contains(key)
                    expansionRow(Self.typeNames[type] ?? "其他文件", item: kind,
                                 expanded: showLocations, color: nil, indent: 20) {
                        if showLocations { expandedTypes.remove(key) } else { expandedTypes.insert(key) }
                    }
                    if showLocations {
                        ForEach(kind.rows("locations"), id: \.detailPath) { item in
                            locationRow(item, indent: 42)
                        }
                    }
                }
            }
        }
    }

    private func expansionRow(_ title: String, item: Object, expanded: Bool, color: Color?,
                              indent: CGFloat, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    .font(.system(size: 9, weight: .semibold)).frame(width: 12)
                    .foregroundStyle(.secondary)
                if let color {
                    Circle().fill(color).frame(width: 8, height: 8)
                }
                Text(title).font(color == nil ? .callout : .callout.weight(.semibold))
                Spacer(minLength: 10)
                totals(item)
            }
            .padding(.leading, indent).padding(.vertical, 9)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityValue("\(expanded ? "已展开" : "已折叠")，\(bytes(item.number("bytes")))，\(item.number("count").formatted()) 项")
    }

    private func locationRow(_ item: Object, indent: CGFloat) -> some View {
        let path = item.string("path")
        return HStack(spacing: 10) {
            Image(systemName: "folder").foregroundStyle(lilac)
            Text(displayPath(path))
                .font(.callout).lineLimit(1).truncationMode(.middle).help(path)
            Spacer(minLength: 10)
            totals(item)
            FinderButton(path: path)
        }.padding(.leading, indent).padding(.vertical, 8)
    }

    private func totals(_ item: Object) -> some View {
        HStack(spacing: 12) {
            Text("\(item.number("count").formatted()) 项")
                .font(.caption).foregroundStyle(.secondary)
            Text(bytes(item.number("bytes")))
                .font(.callout.weight(.medium)).frame(minWidth: 76, alignment: .trailing)
        }.monospacedDigit()
    }

    @ViewBuilder
    private func coverage(_ details: Object) -> some View {
        let reasons = details["skipped_reasons"] as? Object ?? [:]
        let gaps = store.diskUsage?.number("gaps") ?? 0
        if gaps > 0 {
            Panel {
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(Self.skipReasons.filter { reasons.number($0) > 0 }, id: \.self) { reason in
                            HStack {
                                Text(Self.skipNames[reason] ?? reason)
                                Spacer()
                                Text("\(reasons.number(reason).formatted()) 处").monospacedDigit()
                            }.font(.callout).foregroundStyle(.secondary)
                        }
                        let locations = details.rows("skipped_locations")
                        if !locations.isEmpty {
                            Divider()
                            Text("部分跳过的位置").font(.caption.weight(.semibold))
                            ForEach(locations.indices, id: \.self) { index in
                                let item = locations[index]
                                HStack {
                                    Text(displayPath(item.string("path")))
                                        .lineLimit(1).truncationMode(.middle).help(item.string("path"))
                                    Spacer()
                                    Text(Self.skipNames[item.string("reason")] ?? "无法读取")
                                }.font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }.padding(.top, 10)
                } label: {
                    Label("盘点范围 · 跳过 \(gaps.formatted()) 处位置", systemImage: "info.circle")
                        .font(.callout.weight(.semibold))
                }
            }
        }
    }

    private func metric(_ title: String, amount: Int64) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(bytes(amount)).font(.title.weight(.semibold)).monospacedDigit()
        }
    }

    private static let typeNames = [
        "archives": "压缩归档", "installers": "安装包与磁盘映像", "pdf": "PDF",
        "office": "办公文档与电子书", "text": "文本与表格数据", "image": "图片",
        "audio": "音频", "video": "视频", "caches": "缓存与临时文件", "logs": "日志",
        "app_support": "应用支持文件", "containers": "应用容器", "shared_containers": "共享容器",
        "developer": "开发工具数据", "cloud": "本地同步数据", "preferences": "偏好设置",
        "library_other": "其他资源库文件", "dependencies": "依赖与包缓存", "build": "构建产物",
        "git": "Git 仓库数据", "databases": "数据库文件", "app_bundle": "应用包文件",
        "weights": "模型权重", "model_support": "模型支持文件",
        "virtual_disks": "虚拟机磁盘", "device_backups": "设备备份", "simulators": "模拟器数据",
    ]
    private static let skipReasons = ["timeout", "permission", "unreadable", "changed", "excluded", "volume", "depth"]
    private static let skipNames = [
        "timeout": "读取超时", "permission": "访问受限", "unreadable": "无法读取", "changed": "位置已变化",
        "excluded": "排除位置", "volume": "其他卷", "depth": "目录层级超出扫描范围",
    ]
}

private extension Object {
    var detailPath: String { string("path") }
    var detailCategory: String { string("id") }
}
