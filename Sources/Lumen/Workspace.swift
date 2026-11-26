import SwiftUI
import AppKit

struct AppRanking: View {
    @ObservedObject var store: Store
    let drill: (String, String) -> Void
    let cleanup: (String) -> Void
    @State private var bySafe = false
    @State private var query = ""
    @State private var systemOnly = false
    @State private var uninstall: RankedApp?

    var body: some View {
        let ranking = bySafe ? sortedApps(store.inventory.apps, bySafe: true) : store.inventory.apps
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
                    Text("操作").frame(width: 141, alignment: .trailing)
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
                                CleanupIcon(title: item.app.string("name") + " 清理详情") { cleanup(item.id) }
                                FinderButton(path: item.id, reveal: true)
                                FinderButton(path: URL(fileURLWithPath: item.id).deletingLastPathComponent().path)
                                Menu {
                                    Button("应用清理详情") { cleanup(item.id) }
                                    Button("查看已索引的关联文件") { drill(item.id, "") }
                                    Button("审阅低影响项目") { drill(item.id, "safe") }.disabled(item.count == 0)
                                    Button("卸载应用…") { uninstall = item }.disabled(item.app["uninstall_snapshot"] as? Object == nil)
                                } label: { Image(systemName: "ellipsis.circle").frame(width: 23) }
                                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().accessibilityLabel("应用操作")
                            }.frame(width: 141, alignment: .trailing)
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

private let taskSessionPrefixes = [
    "Claude Code": "claude-session:", "Codex": "codex-session:", "Gemini CLI": "gemini-session:",
]

private enum WorkspaceMode: String, CaseIterable {
    case agents = "按 Agent", projects = "按工程"
}

struct WorkspaceView: View {
    @ObservedObject var store: Store
    let drill: (ProjectBucket) -> Void
    @State private var mode = WorkspaceMode.projects
    @State private var expandedAgents = Set<String>()
    @State private var expandedProjects = Set<String>()
    @State private var query = ""
    @State private var bySafe = false
    @State private var budget = ""
    @State private var form: WorkspaceForm?
    @State private var editingTask: Object = [:]
    @State private var selectedCloseout: Object = [:]
    @State private var showingCloseout = false

    private var growth: [String: NSNumber] { store.data["growth"] as? [String: NSNumber] ?? [:] }
    private var search: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 20) {
                metric("工程总占用", bytes(store.inventory.workspaceStats.bytes))
                Divider().frame(height: 29)
                metric("已识别工程", store.inventory.workspaceProjects.count.formatted())
                Divider().frame(height: 29)
                metric("已识别工具", Set(store.inventory.workspaceAgents.map { agent in
                    let family = agent.raw.string("family")
                    return family.isEmpty ? agent.id : family
                }).count.formatted())
                Divider().frame(height: 29)
                metric("共享文件占用", bytes(store.inventory.workspaceStats.sharedBytes))
                    .help("含跨工程共享的文件及硬链接")
            }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                .background(surface, in: RoundedRectangle(cornerRadius: 13))
            HStack {
                Picker("工作区视角", selection: $mode) {
                    ForEach(WorkspaceMode.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                }.pickerStyle(.segmented).labelsHidden().frame(width: 158).accessibilityLabel("工作区视角")
                TextField("搜索工程或 Agent", text: $query).textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 220)
                Spacer(minLength: 10)
                Picker("排序", selection: $bySafe) {
                    Text("占用空间").tag(false)
                    Text("低影响清理").tag(true)
                }.frame(width: 148)
                Button("关联工程…") { editingTask = [:]; form = .project }
            }
            if mode == .agents { agentList } else { projectList }
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
                let closeouts = Dictionary(uniqueKeysWithValues: store.data.rows("task_closeouts").map {
                    ($0.string("id"), $0)
                })
                ForEach(Array(store.data.rows("tasks").enumerated()), id: \.offset) { pair in
                    let task = pair.element
                    HStack {
                        VStack(alignment: .leading) {
                            Text(task.string("agent") + " · " + (task["closed"] as? Bool == true ? "已结束" : "进行中")).bold()
                            Text(task.string("root")).font(.caption).foregroundStyle(.secondary)
                        }; Spacer()
                        if let closeout = closeouts[task.string("id")] {
                            Button("查看收尾单") { selectedCloseout = closeout; showingCloseout = true }
                        }
                        Button("编辑任务") { editingTask = task; form = .task }
                    }
                }
            }
            agentObservations
        }.disabled(!store.busy.isEmpty).sheet(item: $form) { kind in
            WorkspaceEditor(store: store, kind: kind, task: editingTask)
        }.sheet(isPresented: $showingCloseout) {
            TaskCloseoutView(summary: selectedCloseout, review: { path in
                showingCloseout = false
                let key = ProjectBucket.Key(projects: [selectedCloseout.string("root")],
                                            agents: [selectedCloseout.string("agent")])
                drill(ProjectBucket(id: key, paths: [path], detailTitle: "任务收尾单"))
            })
        }
    }

    private var agentObservations: some View {
        let titles = ["turn_finished": "回复结束", "session_ended": "会话结束", "interrupted": "运行中断"]
        let observations = store.data.rows("agent_observations")
        let tasks = Set(store.data.rows("tasks").map { $0.string("id") })
        return Panel {
            HStack {
                Text("会话观察").font(.headline)
                Spacer()
                Button("刷新观察") { store.call("status") }
            }
            if observations.isEmpty {
                Text("接入 Codex 或 Gemini CLI 的本地 Hook 后，可在这里查看观察记录。")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                DisclosureGroup("最近观察（\(observations.count)）") {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(Array(observations.enumerated()), id: \.offset) { _, observation in
                            let name = observation.string("agent")
                            let session = observation.string("session_id")
                            let id = (taskSessionPrefixes[name] ?? "") + session
                            HStack {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text("\(name) · \(titles[observation.string("stage")] ?? "观察")")
                                        .font(.callout.weight(.medium))
                                    pathLabel(observation.string("root"))
                                }
                                Spacer()
                                if tasks.contains(id) {
                                    Text("已登记").font(.caption).foregroundStyle(.secondary)
                                } else {
                                    Button("登记为任务…") {
                                        editingTask = ["root": observation.string("root"), "agent": name,
                                                       "session_id": session]
                                        form = .task
                                    }
                                }
                            }
                        }
                    }.padding(.top, 8)
                }
            }
        }
    }

    private func matches(_ text: String) -> Bool {
        search.isEmpty || text.localizedCaseInsensitiveContains(search)
    }

    private func compare(_ lhs: WorkspaceStats, name: String, _ rhs: WorkspaceStats, other: String) -> Bool {
        let a = bySafe ? lhs.safeBytes : lhs.bytes
        let b = bySafe ? rhs.safeBytes : rhs.bytes
        return a == b ? name < other : a > b
    }

    private var agentList: some View {
        let agents = store.inventory.workspaceAgents.filter { agent in
            matches(agent.id) || agent.projects.contains(where: matches) || agent.stores.contains { matches($0.id) }
        }.sorted { compare($0.stats, name: $0.id, $1.stats, other: $1.id) }
        return VStack(alignment: .leading, spacing: 8) {
            let types = [("app", "桌面应用"), ("desktop", "桌面 Agent"), ("ide", "IDE"),
                         ("extension", "编辑器扩展"), ("cli", "CLI"), ("workspace-manager", "工作区管理器")]
            let counts = Dictionary(grouping: agents, by: { $0.raw.string("kind") }).mapValues(\.count)
            let labels = types.compactMap { key, label in counts[key].map { "\(label) \($0)" } }
            if !labels.isEmpty {
                Text(labels.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
            }
            workspaceList(agents, empty: "未找到匹配的 Agent") { agent in
                agentRow(agent)
            }
        }
    }

    private var projectList: some View {
        let projects = store.inventory.workspaceProjects.values.filter { project in
            matches(project.id) || project.agents.contains(where: matches)
        }.sorted { compare($0.detail.stats, name: $0.id, $1.detail.stats, other: $1.id) }
        return workspaceList(projects, empty: "未找到匹配的工程") { project in
            projectRow(project)
        }
    }

    private func workspaceList<Item: Identifiable, Row: View>(_ rows: [Item], empty: String,
                                                              @ViewBuilder row: @escaping (Item) -> Row) -> some View {
        Group {
            if rows.isEmpty {
                ContentPlaceholder(icon: "square.3.layers.3d", title: store.scanning ? "正在识别工作区" : empty,
                    message: search.isEmpty ? "扫描本机位置后可查看识别到的 Agent 和工程，也可以手动关联工程。" : "试试其他 Agent 名称或工程路径。")
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(rows) { item in
                        if item.id != rows.first?.id { Divider() }
                        row(item).padding(.horizontal, 14).padding(.vertical, 10)
                    }
                }.background(surface, in: RoundedRectangle(cornerRadius: 13))
            }
        }
    }

    private func open(_ id: String, in mode: WorkspaceMode) {
        query = id
        self.mode = mode
        switch mode {
        case .agents: expandedAgents.insert(id)
        case .projects: expandedProjects.insert(id)
        }
    }

    private func expansion(_ id: String, in values: Binding<Set<String>>) -> Binding<Bool> {
        Binding(get: { values.wrappedValue.contains(id) }, set: { open in
            if open { values.wrappedValue.insert(id) } else { values.wrappedValue.remove(id) }
        })
    }

    private func agentRow(_ agent: WorkspaceAgent) -> some View {
        let installations = agent.raw.rows("installations")
        let partial = agent.raw["partial"] as? Bool == true || agent.stores.contains { $0.raw["partial"] as? Bool == true }
        let installed = agent.installed ? "已安装" : "安装未核验"
        let associated = agent.projects.isEmpty ? "工程待关联" : "\(agent.projects.count) 个关联工程"
        let data = agent.stores.isEmpty ? (agent.toolBuckets.isEmpty ? "数据目录未识别" : "已识别工具文件") : "\(agent.stores.count) 处数据目录"
        return DisclosureGroup(isExpanded: expansion(agent.id, in: $expandedAgents)) {
            LazyVStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 12) {
                    capability("支持任务事件", pending: "任务事件未接入", enabled: agent.raw["supports_events"] as? Bool == true)
                    capability("有已验证清理规则", pending: "清理规则待验证", enabled: agent.raw["has_verified_cleanup"] as? Bool == true)
                }.padding(.bottom, 3)
                if !installations.isEmpty {
                    sectionLabel("安装位置")
                    ForEach(Array(installations.enumerated()), id: \.offset) { _, install in
                        installationRow(install)
                    }
                }
                if !agent.projects.isEmpty {
                    sectionLabel("关联工程")
                    ForEach(agent.projects, id: \.self) { root in
                        let project = store.inventory.workspaceProjects[root]
                        HStack(spacing: 10) {
                            if project != nil {
                                Button { open(root, in: .projects) } label: {
                                    Label(URL(fileURLWithPath: root).lastPathComponent, systemImage: "folder")
                                        .lineLimit(1).truncationMode(.middle)
                                }.buttonStyle(.link)
                            }
                            pathLabel(root)
                            Spacer(minLength: 8)
                            if let project {
                                Text(bytes(project.detail.stats.bytes)).monospacedDigit().font(.caption)
                                CleanupIcon(title: "审阅工程清理项目") { drill(project.detail) }.disabled(project.detail.paths.isEmpty)
                            }
                            FinderButton(path: root, reveal: true)
                        }
                    }
                }
                if !agent.stores.isEmpty || !agent.toolBuckets.isEmpty {
                    sectionLabel("工具数据")
                    ForEach(agent.stores) { location in agentStoreRow(location) }
                    ForEach(agent.toolBuckets) { bucket in
                        HStack {
                            Text(agent.stores.isEmpty ? "已索引的工具文件" : "其他工具文件").font(.callout)
                            if bucket.id.agents.count > 1 {
                                Text(bucket.id.agents.joined(separator: "、")).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(bytes(bucket.stats.bytes)).monospacedDigit().font(.caption)
                            CleanupIcon(title: "审阅工具文件") { drill(bucket) }.disabled(bucket.paths.isEmpty)
                        }
                    }
                }
            }.padding(.top, 10).padding(.leading, 6)
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "terminal").foregroundStyle(lilac).frame(width: 22)
                VStack(alignment: .leading, spacing: 4) {
                    Text(agent.id).font(.headline)
                    Text([installed, associated, data].joined(separator: " · "))
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 10)
                VStack(alignment: .trailing, spacing: 3) {
                    space(agent.stats.bytes, shared: agent.stats.sharedBytes, partial: partial)
                    candidates(agent.stats)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func projectRow(_ project: WorkspaceProject) -> some View {
        let detail = project.detail
        let isExpanded = expandedProjects.contains(project.id)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Button {
                    if isExpanded { expandedProjects.remove(project.id) }
                    else { expandedProjects.insert(project.id) }
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                            .font(.caption.weight(.semibold)).foregroundStyle(.secondary).frame(width: 12)
                        Image(systemName: "folder").foregroundStyle(lilac).frame(width: 22)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(detail.title).font(.headline).lineLimit(1)
                            pathLabel(project.id)
                            if !project.agents.isEmpty {
                                let agents = project.agents.joined(separator: "、")
                                Text(agents).font(.caption).foregroundStyle(.secondary).lineLimit(1).help(agents)
                            }
                        }
                    }
                }.buttonStyle(.plain).accessibilityLabel(detail.title)
                    .accessibilityValue(isExpanded ? "已展开" : "已折叠")
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 3) {
                    space(detail.stats.bytes, shared: detail.stats.sharedBytes)
                    candidates(detail.stats)
                }
                FinderButton(path: project.id, reveal: true)
                CleanupIcon(title: "审阅工程清理项目") { drill(detail) }.disabled(detail.paths.isEmpty)
            }.frame(maxWidth: .infinity, alignment: .leading)
            if isExpanded { projectDetails(project) }
        }
    }

    private func projectDetails(_ project: WorkspaceProject) -> some View {
        LazyVStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                if project.agents.isEmpty {
                    Text("待关联 Agent").font(.caption).foregroundStyle(.secondary)
                } else {
                    Menu("关联 Agent（\(project.agents.count)）") {
                        ForEach(project.agents, id: \.self) { name in
                            Button(name) { open(name, in: .agents) }
                        }
                    }.fixedSize()
                }
                if !project.related.isEmpty {
                    Menu("共享工程（\(project.related.count)）") {
                        ForEach(project.related, id: \.self) { root in
                            Button(displayPath(root)) { open(root, in: .projects) }
                        }
                    }.fixedSize()
                }
                if let delta = growth[project.id], delta.int64Value != 0 {
                    Text("变化 \(bytes(delta.int64Value))").font(.caption).foregroundStyle(.secondary)
                }
            }
            if !project.categories.isEmpty {
                sectionLabel("数据类别")
                ForEach(project.categories) { category in
                    let stats = category.detail.stats
                    HStack(spacing: 8) {
                        Text(WorkspaceCategory.title(category.kind)).font(.callout)
                        Text("\(stats.count) 项").font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        candidates(stats)
                        Text(bytes(stats.bytes)).monospacedDigit().frame(minWidth: 66, alignment: .trailing)
                        CleanupIcon(title: "审阅\(WorkspaceCategory.title(category.kind))文件") { drill(category.detail) }
                    }
                }
            }
        }.padding(.top, 10).padding(.leading, 28)
    }

    private func installationRow(_ install: Object) -> some View {
        let path = install.string("path")
        let details = [install.string("version"), install.string("evidence")].filter { !$0.isEmpty }.joined(separator: " · ")
        return HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                if path.hasPrefix("/") { pathLabel(path) }
                if !details.isEmpty { Text(details).font(.caption).foregroundStyle(.secondary) }
            }
            Spacer()
            if path.hasPrefix("/") { FinderButton(path: path, reveal: true) }
        }
    }

    private func agentStoreRow(_ location: WorkspaceAgentStore) -> some View {
        let source = location.raw
        let title = source.string("title")
        let evidence = source.string("evidence")
        let issue = source.string("issue")
        let partial = source["partial"] as? Bool == true
        return HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title.isEmpty ? URL(fileURLWithPath: location.id).lastPathComponent : title).font(.callout.weight(.medium))
                pathLabel(location.id)
                if !evidence.isEmpty && evidence != title {
                    Text(evidence).font(.caption).foregroundStyle(.secondary)
                }
                if !issue.isEmpty { Text(issue).font(.caption).foregroundStyle(.orange) }
                else if partial { Text("部分位置未核验").font(.caption).foregroundStyle(.orange) }
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 3) {
                space(source.number("bytes"), shared: source.number("shared_bytes"), partial: partial)
                Text("\(source.number("count")) 项").font(.caption).foregroundStyle(.secondary)
            }
            FinderButton(path: location.id, reveal: true)
            CleanupIcon(title: "审阅工具数据") { drill(location.detail) }.disabled(location.detail.paths.isEmpty)
        }
    }

    private func sectionLabel(_ value: String) -> some View {
        Text(value).font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding(.top, 4)
    }

    private func capability(_ title: String, pending: String, enabled: Bool) -> some View {
        Label(enabled ? title : pending, systemImage: enabled ? "checkmark.circle" : "circle.dotted")
            .font(.caption).foregroundStyle(enabled ? lilac : .secondary)
    }

    private func pathLabel(_ path: String) -> some View {
        Text(displayPath(path)).font(.caption).foregroundStyle(.secondary)
            .lineLimit(1).truncationMode(.middle).help(path)
    }

    private func candidates(_ stats: WorkspaceStats) -> some View {
        Text("候选 \(stats.safe + stats.review) 项 · \(bytes(stats.safeBytes + stats.reviewBytes))")
            .font(.caption).foregroundStyle(.secondary).help("低影响与待确认文件")
    }

    private func space(_ total: Int64, shared: Int64, partial: Bool = false) -> some View {
        VStack(alignment: .trailing, spacing: 3) {
            Text(bytes(total) + (partial ? "+" : "")).font(.callout.weight(.semibold)).monospacedDigit()
                .help(partial ? "部分项目未能读取，显示已盘点占用" : "按磁盘已分配块估算。APFS 克隆可能共享底层空间。")
            Text("独占 \(bytes(max(0, total - shared))) · 共享 \(bytes(shared))")
                .font(.caption).foregroundStyle(.secondary).monospacedDigit()
        }.fixedSize(horizontal: true, vertical: false)
    }

    private func metric(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(size: 23, weight: .semibold, design: .rounded)).monospacedDigit()
        }
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
                if taskSessionPrefixes[agent.trimmingCharacters(in: .whitespaces)] != nil && task["id"] == nil {
                    TextField("会话 ID（可选）", text: $sessionID)
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
                    if task["id"] == nil, let prefix = taskSessionPrefixes[agent.trimmingCharacters(in: .whitespaces)],
                       !session.isEmpty {
                        guard session.range(of: #"^[A-Za-z0-9_-]{1,128}$"#, options: .regularExpression) != nil else {
                            formError = "会话 ID 仅接受 1 至 128 个字母、数字、连字符或下划线。"
                            return
                        }
                        let identifier = prefix + session
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
            sessionID = task.string("session_id")
        }
    }
}
