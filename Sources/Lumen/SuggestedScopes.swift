import SwiftUI
import AppKit

struct SuggestedScopes: View {
    @ObservedObject var store: Store
    let deep: Bool

    private let extra = [
        ("应用支持数据", "Library/Application Support"),
        ("应用容器", "Library/Containers"),
        ("共享容器", "Library/Group Containers"),
        ("已安装应用的管理范围", "/Applications"),
        ("个人应用的管理范围", "Applications"),
        ("机器级缓存", "/Library/Caches"),
        ("机器级日志", "/Library/Logs")
    ]

    var body: some View {
        let automatic = store.data.rows("local_locations")
        let excluded = store.data.paths("local_exclude")
        let groups = [("文件", "folder"), ("资源库", "folder"),
                      ("Agent 工程", "square.3.layers.3d"), ("Agent 数据", "externaldrive"),
                      ("模型存储", "cpu")]
        Panel {
            Text("本机自动扫描").font(.headline)
            Text("标准位置和已识别工程会在打开既白时盘点。").font(.callout).foregroundStyle(.secondary)
            if automatic.isEmpty && excluded.isEmpty {
                Text("完成自动扫描后会列出已发现的位置。").font(.callout).foregroundStyle(.secondary)
            }
            ForEach(groups, id: \.0) { group, icon in
                let rows = automatic.filter { $0.string("group") == group }
                if !rows.isEmpty {
                    DisclosureGroup {
                        ForEach(rows, id: \.scopePath) { row in
                            let path = row.string("path")
                            location(row.string("title"), path: path, icon: icon) {
                                FinderButton(path: path, reveal: row["directory"] as? Bool == false)
                                if group == "Agent 工程" && !store.roots.contains(where: { path == $0 || path.hasPrefix($0 + "/") }) {
                                    Button("加为清理范围") { store.scan(store.roots + [path], deep: deep, renewRoots: [path]) }
                                }
                                Button {
                                    updateExclusion(excluded + [path])
                                } label: { Image(systemName: "minus.circle").frame(width: 25, height: 23) }
                                    .buttonStyle(.borderless).help("排除自动扫描")
                                    .accessibilityLabel("排除 \(row.string("title"))")
                            }
                        }
                    } label: {
                        HStack {
                            Text(group).fontWeight(.medium)
                            Spacer()
                            Text(rows.count.formatted()).monospacedDigit().foregroundStyle(.secondary)
                        }
                    }
                }
            }
            if !excluded.isEmpty {
                DisclosureGroup("已排除的位置（\(excluded.count)）") {
                    ForEach(excluded, id: \.self) { path in
                        location(URL(fileURLWithPath: path).lastPathComponent, path: path, icon: "minus.circle") {
                            Button("重新纳入") { updateExclusion(excluded.filter { $0 != path }) }
                        }
                    }
                }
            }
            Divider()
            DisclosureGroup("添加扩展位置") {
                let home = FileManager.default.homeDirectoryForCurrentUser
                let known = extra.map { title, rel in (title, rel.hasPrefix("/") ? rel : home.appendingPathComponent(rel).path) }
                let discovered = store.data.paths("suggested_projects").map { path in (URL(fileURLWithPath: path).lastPathComponent, path) }
                ForEach((discovered + known).filter { !store.roots.contains($0.1) && !excluded.contains($0.1) }, id: \.1) { name, path in
                    location(name, path: path, icon: "folder.badge.plus") {
                        Button("添加") { store.scan(store.roots + [path], deep: deep, renewRoots: [path]) }
                    }
                }
            }
        }.disabled(!store.busy.isEmpty)
    }

    private func updateExclusion(_ paths: [String]) {
        store.call("settings", ["local_exclude": paths])
    }

    private func location<Trailing: View>(_ name: String, path: String, icon: String,
                                          @ViewBuilder trailing: () -> Trailing) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon).foregroundStyle(lilac).frame(width: 24)
            VStack(alignment: .leading, spacing: 3) {
                Text(name).font(.callout.weight(.medium)).lineLimit(1)
                Text(displayPath(path)).font(.caption).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle).help(path)
            }
            Spacer(minLength: 8)
            trailing()
        }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6).padding(.leading, 8)
    }
}

private extension Object {
    var scopePath: String { string("path") }
}
