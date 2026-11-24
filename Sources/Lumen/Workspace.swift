import SwiftUI
import AppKit

struct AppRanking: View {
    @ObservedObject var store: Store
    let drill: (String, String) -> Void
    @State private var bySafe = false
    @State private var query = ""
    @State private var systemOnly = false
    @State private var uninstall: RankedApp?

    var body: some View {
        let ranking = rankedApps(store.data.rows("apps"), files: store.files,
                                scanID: store.data.string("scan_id"), bySafe: bySafe)
        let visible = ranking.filter { app in
            (!systemOnly || app.app["system"] as? Bool == true) &&
            (query.isEmpty || app.app.string("name").localizedCaseInsensitiveContains(query) ||
             app.app.string("bundle_id").localizedCaseInsensitiveContains(query))
        }
        let largest = Double(max(ranking.map(\.size).max() ?? 1, 1))
        return VStack(alignment: .leading, spacing: 15) {
            HStack(spacing: 12) {
                Text("\(ranking.count.formatted()) 个应用").font(.headline)
                Text(bytes(ranking.reduce(0) { $0 + $1.size })).monospacedDigit().foregroundStyle(.secondary)
                Spacer()
                Toggle("仅系统应用", isOn: $systemOnly).toggleStyle(.checkbox)
                TextField("搜索应用或标识符", text: $query).textFieldStyle(.roundedBorder).frame(maxWidth: 220)
            }
            VStack(spacing: 0) {
                HStack {
                    Text("应用名称")
                    Spacer()
                    Picker("排序", selection: $bySafe) {
                        Text("占用空间").tag(false)
                        Text("可低影响清理").tag(true)
                    }.labelsHidden().frame(width: 126).accessibilityLabel("按空间排序")
                    Text("操作").frame(width: 110, alignment: .trailing)
                }.font(.caption.weight(.medium)).foregroundStyle(.secondary).padding(.horizontal, 15).padding(.vertical, 10)
                Divider()
                if visible.isEmpty {
                    ContentPlaceholder(icon: "macwindow", title: store.scanning ? "正在发现应用" : "没有匹配的应用",
                                       message: "应用排行包含标准安装目录和 macOS 自带应用。").padding(.horizontal, 16)
                }
                LazyVStack(spacing: 0) {
                    ForEach(visible) { item in
                        HStack(spacing: 12) {
                            Image(nsImage: NSWorkspace.shared.icon(forFile: item.id)).resizable().frame(width: 34, height: 34)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(item.app.string("name")).font(.callout.weight(.semibold)).lineLimit(1)
                                Text(displayPath(item.id)).lineLimit(1).truncationMode(.middle)
                                    .font(.caption).foregroundStyle(.secondary).help(item.id)
                            }
                            Spacer(minLength: 8)
                            VStack(alignment: .trailing, spacing: 5) {
                                Text(bytes(item.size) + (item.app["size_partial"] as? Bool == true ? "+" : ""))
                                    .font(.callout.weight(.semibold)).monospacedDigit()
                                ProportionBar(value: Double(item.size) / largest, color: lilac.opacity(0.65))
                                if item.count > 0 { Text(bytes(item.safe) + " 可清理").font(.caption).foregroundStyle(lilac) }
                            }.frame(width: 126)
                            HStack(spacing: 4) {
                                FinderButton(path: item.id, reveal: true)
                                FinderButton(path: URL(fileURLWithPath: item.id).deletingLastPathComponent().path)
                                Menu {
                                    Button("查看已索引的关联文件") { drill(item.id, "") }
                                    Button("审阅低影响项目") { drill(item.id, "safe") }.disabled(item.count == 0)
                                    Button("卸载应用…") { uninstall = item }.disabled(item.app["uninstall_snapshot"] as? Object == nil)
                                } label: { Image(systemName: "ellipsis.circle").frame(width: 23) }
                                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().accessibilityLabel("应用操作")
                            }.frame(width: 110, alignment: .trailing)
                        }.padding(.horizontal, 15).padding(.vertical, 10)
                        Divider().padding(.leading, 61)
                    }
                }
            }.background(surface, in: RoundedRectangle(cornerRadius: 13))
        }.sheet(item: $uninstall) { app in
            ConfirmRemoval(title: "卸载 " + app.app.string("name"),
                           explanation: "将以下整个应用包移入可恢复隔离区。应用的关联数据可在文件明细中单独审阅。",
                           paths: [app.id], button: "确认移除应用包") {
                store.call("uninstall", ["path":app.id, "scan_id":app.scanID])
            }
        }
    }
}

enum WorkspaceForm: String, Identifiable { case project, task; var id: Self { self } }

struct WorkspaceView: View {
    @ObservedObject var store: Store
    let drill: (ProjectBucket) -> Void
    @State private var collapsed = Set<AgentGroup>()
    @State private var query = ""
    @State var bySafe = false
    @State var budget = ""
    @State var form: WorkspaceForm?
    @State var editingTask: Object = [:]
    var growth: [String: NSNumber] { store.data["growth"] as? [String: NSNumber] ?? [:] }
    var body: some View {
        let groups = workspacePartitions(projects: store.projects, files: store.files, bySafe: bySafe)
        return VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 20) {
                metric("工程占用", bytes(groups.reduce(0) { $0 + $1.stats.bytes }))
                Divider().frame(height: 29)
                metric("已识别工程", store.projects.count.formatted())
                Divider().frame(height: 29)
                metric("跨工程共享", bytes(groups.first { $0.id == .shared }?.stats.bytes ?? 0))
            }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                .background(surface, in: RoundedRectangle(cornerRadius: 13))
            HStack {
                TextField("搜索工程或 Agent", text: $query).textFieldStyle(.roundedBorder).frame(maxWidth: 240)
                Spacer()
                Picker("排序", selection: $bySafe) {
                    Text("占用空间").tag(false); Text("低影响清理").tag(true)
                }.frame(width: 170)
                Button("关联工程…") { editingTask = [:]; form = .project }
            }
            let matches = groups.map { group in
                let rows = group.buckets.filter { row in
                    query.isEmpty || group.id.title.localizedCaseInsensitiveContains(query) ||
                    row.title.localizedCaseInsensitiveContains(query) ||
                    row.id.projects.contains { $0.localizedCaseInsensitiveContains(query) }
                }
                return (group, rows)
            }.filter { !$0.1.isEmpty }
            if matches.isEmpty {
                ContentPlaceholder(icon: "square.3.layers.3d", title: store.scanning ? "正在识别工作区" : "未找到匹配的项目",
                                   message: "既白会从 Codex 与 Claude Code 的会话和标准工作目录识别项目。")
            }
            ForEach(matches, id: \.0.id) { group, rows in
                let largest = max(rows.map { $0.stats.bytes }.max() ?? 0, 1)
                VStack(alignment: .leading, spacing: 0) {
                    DisclosureGroup(isExpanded: Binding(get: { !collapsed.contains(group.id) }, set: { open in
                        if open { collapsed.remove(group.id) } else { collapsed.insert(group.id) }
                    })) {
                        LazyVStack(spacing: 0) {
                            ForEach(rows) { row in
                                Divider()
                                projectRow(row, largest: largest)
                            }
                        }.padding(.top, 8)
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "square.3.layers.3d").foregroundStyle(lilac)
                            Text(group.id.title).font(.headline)
                            Text("\(rows.count) 个分区").font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            Text(bytes(group.stats.bytes)).monospacedDigit().font(.callout.weight(.semibold))
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }.padding(14).background(surface, in: RoundedRectangle(cornerRadius: 13))
            }
            Panel {
                Text("项目空间预算").font(.headline)
                Text("每个分区的上限 · " + (store.data.number("budget") == 0 ? "未设置" : bytes(store.data.number("budget"))))
                HStack {
                    TextField("额度（MiB）", text: $budget).frame(width: 140)
                    Button("保存额度") {
                        if let value = Int64(budget), value >= 0 && value <= Int64.max / 1_048_576 {
                            store.call("settings", ["budget": value * 1_048_576])
                        } else { store.error = "请输入有效的非负整数。" }
                    }
                    Button("关闭预算") { store.call("settings", ["budget": 0]) }
                }
                ForEach(store.data.paths("alerts"), id: \.self) { Text("超出预算 · " + $0).foregroundStyle(.orange) }
                if !store.data.paths("alerts").isEmpty {
                    Button("暂停提醒一天") { store.call("settings", ["snooze_until": Int(Date().timeIntervalSince1970) + 86400]) }
                }
            }
            Panel {
                HStack {
                    Text("任务清单").font(.headline); Spacer()
                    Button("登记任务") { editingTask = [:]; form = .task }
                }
                ForEach(Array(store.data.rows("tasks").enumerated()), id: \.offset) { pair in
                    let task = pair.element
                    HStack {
                        VStack(alignment: .leading) {
                            Text(task.string("agent") + " · " + (task["closed"] as? Bool == true ? "已结束" : "进行中")).bold()
                            Text(task.string("root")).font(.caption).foregroundStyle(.secondary)
                        }; Spacer()
                        Button("编辑任务") { editingTask = task; form = .task }
                    }
                }
            }
        }.disabled(!store.busy.isEmpty).sheet(item: $form) { kind in
            WorkspaceEditor(store: store, kind: kind, task: editingTask)
        }
    }
    private func metric(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(size: 23, weight: .semibold, design: .rounded)).monospacedDigit()
        }
    }

    private func projectRow(_ row: ProjectBucket, largest: Int64) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "folder").foregroundStyle(lilac.opacity(0.8)).frame(width: 22)
            VStack(alignment: .leading, spacing: 4) {
                Text(row.title).font(.callout.weight(.semibold)).lineLimit(1)
                if let path = row.id.projects.first {
                    Text(displayPath(path) + (row.id.projects.count > 1 ? "  +\(row.id.projects.count - 1)" : ""))
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle).help(path)
                }
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 4) {
                Text(bytes(row.stats.bytes)).monospacedDigit().font(.callout.weight(.medium))
                ProportionBar(value: Double(row.stats.bytes) / Double(largest), color: lilac.opacity(0.55))
                if row.stats.safe + row.stats.review > 0 {
                    Text("\(row.stats.safe + row.stats.review) 项 · \(bytes(row.stats.safeBytes + row.stats.reviewBytes))")
                        .font(.caption).foregroundStyle(.secondary).help("低影响及待确认文件")
                }
                if let root = row.id.projects.first, row.id.projects.count == 1,
                   let delta = growth[root], delta.int64Value != 0 {
                    Text("变化 \(bytes(delta.int64Value))").font(.caption).foregroundStyle(.secondary)
                }
            }.frame(width: 144)
            if row.id.projects.count == 1, let root = row.id.projects.first {
                FinderButton(path: root)
            } else if row.id.projects.count > 1 {
                Menu {
                    ForEach(row.id.projects, id: \.self) { path in
                        Button(displayPath(path)) { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
                    }
                } label: { Image(systemName: "folder") }
                    .menuStyle(.borderlessButton).fixedSize().accessibilityLabel("打开共享项目目录")
            }
            Button("文件") { drill(row) }.disabled(row.paths.isEmpty)
        }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 10)
    }

}

struct WorkspaceEditor: View {
    @ObservedObject var store: Store
    let kind: WorkspaceForm
    let task: Object
    @Environment(\.dismiss) var dismiss
    @State var root = ""
    @State var agent = ""
    @State var inputs = ""
    @State var outputs = ""
    @State var temporary = ""
    @State var sessionID = ""
    @State var formError = ""
    @State var closed = false
    func lines(_ value: String) -> [String] {
        value.split(separator: "\n").map(String.init)
    }
    func pathsEditor(_ title: String, _ value: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.headline)
            TextEditor(text: value).font(.body.monospaced()).frame(height: 52).border(.secondary.opacity(0.4))
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(kind == .task ? "登记任务清单" : "登记或修正工程关联").font(.title2.bold())
            HStack { TextField("工程绝对路径", text: $root); Button("选取目录") {
                let p = NSOpenPanel(); p.canChooseDirectories = true; p.canChooseFiles = false
                if p.runModal() == .OK { root = p.url?.path ?? root }
            }}
            TextField(kind == .task ? "Agent 名称" : "Agent 名称（多个名称用逗号分隔）", text: $agent)
            if kind == .task {
                if agent.trimmingCharacters(in: .whitespaces) == "Claude Code" && task["id"] == nil {
                    TextField("Claude 会话 ID（可选，用于自动接收结束事件）", text: $sessionID)
                }
                Text("每行填写一个已授权的绝对路径。输入与正式输出会标记为保留。").foregroundStyle(.secondary)
                pathsEditor("输入", $inputs); pathsEditor("正式输出", $outputs); pathsEditor("临时产物", $temporary)
                Toggle("任务已结束", isOn: $closed)
            }
            if !formError.isEmpty { Text(formError).foregroundStyle(.orange) }
            HStack { Spacer(); Button("取消") { dismiss() }; Button("保存") {
                if kind == .task {
                    var args: Object = ["root":root,"agent":agent,"inputs":lines(inputs),"outputs":lines(outputs),"temporary":lines(temporary),"closed":closed]
                    let session = sessionID.trimmingCharacters(in: .whitespacesAndNewlines)
                    if task["id"] == nil && agent.trimmingCharacters(in: .whitespaces) == "Claude Code" && !session.isEmpty {
                        guard session.range(of: #"^[A-Za-z0-9_-]{1,128}$"#, options: .regularExpression) != nil else {
                            formError = "会话 ID 仅接受 1 至 128 个字母、数字、连字符或下划线。"
                            return
                        }
                        let identifier = "claude-session:" + session
                        guard !store.data.rows("tasks").contains(where: { $0.string("id") == identifier }) else {
                            formError = "该会话已登记，请从任务清单编辑已有记录。"
                            return
                        }
                        args["id"] = identifier
                    } else {
                        args["id"] = task["id"] ?? UUID().uuidString
                    }
                    store.call("task", args)
                } else {
                    var projects = store.data["configured_projects"] as? [String:[String]] ?? store.projects
                    projects[root] = agent.components(separatedBy: CharacterSet(charactersIn: ",，")).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                    store.call("settings", ["projects":projects])
                }
                dismiss()
            }.disabled(!root.hasPrefix("/") || agent.trimmingCharacters(in: .whitespaces).isEmpty).buttonStyle(.borderedProminent) }
        }.padding(24).frame(width: 560).onAppear {
            root = task.string("root"); agent = task.string("agent")
            inputs = task.paths("inputs").joined(separator: "\n"); outputs = task.paths("outputs").joined(separator: "\n")
            temporary = task.paths("temporary").joined(separator: "\n"); closed = task["closed"] as? Bool ?? false
        }
    }
}
