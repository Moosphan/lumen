import SwiftUI

/// Present known macOS locations without enumerating them before consent.
struct SuggestedScopes: View {
    @ObservedObject var store: Store
    let deep: Bool
    @State private var selected = Set<String>()

    private var locations: [(String, String)] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let userPaths = [
            ("Claude Code 工程索引", ".claude/projects"), ("Codex 会话索引", ".codex/sessions"),
            ("下载", "Downloads"), ("用户缓存", "Library/Caches"), ("应用日志", "Library/Logs"),
            ("应用支持数据", "Library/Application Support"), ("应用容器", "Library/Containers"),
            ("共享容器", "Library/Group Containers"), ("开发工具", "Library/Developer"),
            ("模型与工具缓存", ".cache")
        ]
        let known = userPaths.map { ($0.0, home.appendingPathComponent($0.1).path) } +
            [("已安装应用", "/Applications"), ("机器级第三方缓存", "/Library/Caches"),
             ("机器级第三方日志", "/Library/Logs")]
        let linked = store.data.paths("suggested_projects").map { ("会话记录提到的工程", $0) }
        var seen = Set<String>()
        return (linked + known).filter { seen.insert($0.1).inserted && !store.roots.contains($0.1) }
    }

    var body: some View {
        Panel {
            DisclosureGroup("常见位置与已发现工程") {
                ForEach(locations, id: \.1) { location in
                    Toggle(isOn: Binding(get: { selected.contains(location.1) }, set: { enabled in
                        if enabled { selected.insert(location.1) } else { selected.remove(location.1) }
                    })) {
                        VStack(alignment: .leading) {
                            Text(location.0)
                            Text(location.1).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        }
                    }
                }
                Button("授权并扫描所选的 \(selected.count) 个位置") {
                    let additions = Array(selected)
                    selected.removeAll()
                    store.scan(store.roots + additions, deep: deep, renewRoots: additions)
                }.disabled(selected.isEmpty || !store.busy.isEmpty)
            }
        }
    }
}
