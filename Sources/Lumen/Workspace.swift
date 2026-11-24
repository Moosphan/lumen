import SwiftUI
import AppKit

struct AppRanking: View {
    @ObservedObject var store: Store
    let drill: (String, String) -> Void
    @State var order = "独占占用"
    @State var uninstall: RankedApp?

    var ranking: [RankedApp] {
        rankedApps(store.data.rows("apps"), files: store.files, scanID: store.data.string("scan_id"), bySafe: order != "独占占用")
    }

    var body: some View {
        Panel {
            HStack {
                Text("范围内的应用").font(.headline)
                Spacer(); Picker("排序", selection: $order) {
                    Text("独占占用").tag("独占占用"); Text("低影响清理").tag("低影响清理")
                }.frame(width: 180)
            }
            if ranking.isEmpty { Text("未在授权范围内识别到应用包。").foregroundStyle(.secondary) }
            if !store.data.paths("issues").isEmpty { Text("扫描存在覆盖缺口，统计仅含已读取的文件。").foregroundStyle(lilac) }
            ForEach(ranking) { item in
                Divider()
                HStack(spacing: 12) {
                    Image(nsImage: NSWorkspace.shared.icon(forFile: item.id)).resizable().frame(width: 38, height: 38)
                    VStack(alignment: .leading) {
                        Text(item.app.string("name")).bold()
                        Text(item.id).lineLimit(1).truncationMode(.middle).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    VStack(alignment: .trailing) {
                        Text(bytes(item.size)).monospacedDigit()
                        Text("\(item.count) 项低影响 · \(bytes(item.safe))").font(.caption).foregroundStyle(lilac)
                    }
                    Menu("管理") {
                        Button("文件明细") { drill(item.id, "") }
                        Button("卸载应用…") { uninstall = item }.disabled(item.app["uninstall_snapshot"] as? Object == nil)
                    }.fixedSize()
                    Button("审阅") { drill(item.id, "safe") }.disabled(item.count == 0)
                }
            }
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
    @Binding var expanded: Set<AgentGroup>
    @State var bySafe = false
    @State var budget = ""
    @State var form: WorkspaceForm?
    @State var editingTask: Object = [:]
    var growth: [String: NSNumber] { store.data["growth"] as? [String: NSNumber] ?? [:] }
    var body: some View {
        let groups = workspacePartitions(projects: store.projects, files: store.files, bySafe: bySafe)
        return VStack(alignment: .leading, spacing: 14) {
            Panel {
                Label("工程与工具数据", systemImage: "square.3.layers.3d").foregroundStyle(lilac)
                Text(bytes(groups.reduce(0) { $0 + $1.stats.bytes })).font(.largeTitle.bold())
                let shared = groups.first { $0.id == .shared }?.stats.bytes ?? 0
                Text("\(store.projects.count) 个工程 · 跨工程共享 \(bytes(shared)) · 同一文件计一次").foregroundStyle(.secondary)
            }
            Panel {
                HStack {
                    Text("Agent 分区").font(.headline); Spacer()
                    Picker("排序", selection: $bySafe) {
                        Text("占用空间").tag(false); Text("可回收量").tag(true)
                    }.frame(width: 160)
                    Button("登记工程与 Agent") { editingTask = [:]; form = .project }
                }
                if groups.isEmpty { Text("当前范围内没有关联工程或 Agent 工具数据。").foregroundStyle(.secondary) }
                ForEach(groups) { group in
                    DisclosureGroup(isExpanded: Binding(get: { expanded.contains(group.id) }, set: {
                        if $0 { expanded.insert(group.id) } else { expanded.remove(group.id) }
                    })) {
                        ForEach(group.buckets) { row in
                            HStack {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(row.title).bold()
                                    if !row.id.agents.isEmpty { Text(row.id.agents.joined(separator: "、")).foregroundStyle(lilac) }
                                    if !row.id.projects.isEmpty {
                                        Text(row.id.projects.joined(separator: "\n")).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                                    }
                                }; Spacer()
                                VStack(alignment: .trailing) {
                                    Text(bytes(row.stats.bytes)).monospacedDigit()
                                    if row.id.projects.count == 1, let root = row.id.projects.first, let delta = growth[root] {
                                        Text("变化 \(bytes(delta.int64Value))").font(.caption)
                                    }
                                }
                                Button("文件") { drill(row) }.disabled(row.paths.isEmpty)
                            }.padding(.vertical, 5)
                        }
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(group.id.title).font(.headline)
                                Text("可回收 \(group.stats.safe) 项 · \(bytes(group.stats.safeBytes))    待确认 \(group.stats.review) 项")
                                    .font(.caption).foregroundStyle(.secondary)
                            }; Spacer()
                            Text(bytes(group.stats.bytes)).monospacedDigit()
                        }
                    }; Divider()
                }
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
                Text("每行填写一个已授权的绝对路径。输入与正式输出会标记为保留。").foregroundStyle(.secondary)
                pathsEditor("输入", $inputs); pathsEditor("正式输出", $outputs); pathsEditor("临时产物", $temporary)
                Toggle("任务已结束", isOn: $closed)
            }
            HStack { Spacer(); Button("取消") { dismiss() }; Button("保存") {
                if kind == .task {
                    var args: Object = ["root":root,"agent":agent,"inputs":lines(inputs),"outputs":lines(outputs),"temporary":lines(temporary),"closed":closed]
                    args["id"] = task["id"] ?? UUID().uuidString
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
