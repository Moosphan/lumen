import AppKit
import SwiftUI

@main struct LumenApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene {
        Window("既白 Lumen", id: "main") {
            Workbench().frame(minWidth: 930, minHeight: 620)
        }
        .defaultSize(width: 1160, height: 780)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let lilac = Color(red: 0.80, green: 0.71, blue: 0.96)
let canvas = Color(red: 0.086, green: 0.088, blue: 0.104)
let surface = Color(red: 0.122, green: 0.124, blue: 0.145)

struct Panel<Content: View>: View {
    @ViewBuilder let content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 12, content: content)
            .padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(surface, in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(.white.opacity(0.075)))
    }
}

enum Page: String, CaseIterable {
    case home = "空间总览", apps = "应用排行", agents = "Agent 工作区", review = "清理建议", history = "清理记录", scopes = "扫描范围"
    var icon: String {
        switch self {
        case .home: return "square.grid.2x2"
        case .apps: return "macwindow"
        case .agents: return "square.3.layers.3d"
        case .review: return "sparkles"
        case .history: return "clock.arrow.circlepath"
        case .scopes: return "folder.badge.gearshape"
        }
    }
}

struct Workbench: View {
    @StateObject var store = Store()
    @State var page = Page.home
    @State var deep = false
    @State var owner = ""
    @State var project = ""
    @State var bucketPaths: Set<String>?
    @State var expandedGroups = Set<AgentGroup>()
    @State var verdict = ""
    @AppStorage("scheduledLocalInventory") var scheduled = false
    @State var lastScheduledScan = Date()
    @State var automaticConfirmation = false
    let ticker = Timer.publish(every: 60, on: .main, in: .common).autoconnect()
    var safe: [Entry] { store.files.filter { $0.decision == "safe" } }
    func review(owner: String = "", project: String = "", verdict: String = "") {
        self.owner = owner
        self.project = project
        self.bucketPaths = nil
        self.verdict = verdict
        page = .review
    }

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Image(systemName: "circle.hexagongrid.fill").foregroundStyle(lilac)
                    Text("既白 Lumen").bold()
                }.font(.title2).padding(.bottom, 26)
                Text("工作台").font(.caption).foregroundStyle(.secondary)
                ForEach(Page.allCases, id: \.self) { link in
                    if link == .review {
                        Text("清理中心").font(.caption).foregroundStyle(.secondary).padding(.top, 20)
                    }
                    Button {
                        if link == .review { review() } else { page = link }
                    } label: {
                        Label(link.rawValue, systemImage: link.icon)
                            .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                            .background(page == link ? lilac.opacity(0.15) : .clear, in: RoundedRectangle(cornerRadius: 8))
                            .foregroundStyle(page == link ? lilac : .primary)
                    }.buttonStyle(.plain)
                }
                Spacer()
                Divider()
                Label("本地设备", systemImage: "laptopcomputer").foregroundStyle(.secondary).padding(.vertical, 6)
            }.padding(16).frame(width: 180).background(Color.black.opacity(0.15))
            Divider()
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Label("工作台   /   " + page.rawValue, systemImage: "sidebar.left")
                    Spacer()
                    Circle().fill(.green).frame(width: 6, height: 6)
                    Text(store.fixtureState == nil ? "本地模式" : "独立演练")
                    if store.fixtureState != nil { Button("返回本机工作区") { store.exitFixture(); page = .home }.disabled(!store.busy.isEmpty) }
                }.font(.caption).foregroundStyle(.secondary).padding(.horizontal, 26).frame(height: 42)
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        HStack {
                            Text(page.rawValue).font(.largeTitle.bold())
                            Spacer()
                            if page == .home || page == .scopes {
                                scanControl
                            }
                        }
                        if !store.busy.isEmpty {
                            HStack {
                                ProgressView().controlSize(.small)
                                Text(store.busy == "scan" ? "正在盘点授权目录…" : "正在处理本地数据…")
                                if store.busy == "scan" { Button("取消扫描") { store.cancelScan() } }
                            }.foregroundStyle(.secondary)
                        } else if !store.notice.isEmpty {
                            Text(store.notice).font(.callout).foregroundStyle(lilac)
                        }
                        switch page {
                        case .home: home
                        case .apps: AppRanking(store: store) { review(owner: $0, verdict: $1) }
                        case .agents: WorkspaceView(store: store, drill: { bucket in
                            review(project: bucket.title)
                            bucketPaths = bucket.paths
                        }, expanded: $expandedGroups)
                        case .review: ReviewView(store: store, owner: $owner, project: $project, verdict: $verdict, bucketPaths: $bucketPaths)
                        case .history: HistoryView(store: store)
                        case .scopes: scopes
                        }
                    }.padding(26).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }.background(canvas).tint(lilac).preferredColorScheme(.dark)
        .task { store.call("status") { data in deep = data["deep"] as? Bool ?? false } }
        .onReceive(ticker) { now in
            guard scheduled, store.fixtureState == nil, now.timeIntervalSince(lastScheduledScan) >= 900, page == .home,
                  !store.roots.isEmpty, store.busy.isEmpty, NSApp.isActive, store.error == nil,
                  NSApp.modalWindow == nil, NSApp.keyWindow?.attachedSheet == nil else { return }
            lastScheduledScan = now
            store.scan(store.roots, deep: deep)
        }
        .onChange(of: store.busy) { command in
            if command == "scan" { lastScheduledScan = Date() }
        }
        .sheet(item: $store.result) { report in ResultSheet(report: report) }
        .alert("操作未完成", isPresented: Binding(get: { store.error != nil }, set: { if !$0 { store.error = nil } })) {
            Button("好") { store.error = nil }
        } message: { Text(store.error ?? "") }
    }

    var scanControl: some View {
        Button {
            if store.roots.isEmpty { store.addScope(deep: deep) } else { store.scan(store.roots, deep: deep) }
        } label: { Label(store.roots.isEmpty ? "选择目录并扫描" : "重新扫描", systemImage: "viewfinder") }
        .disabled(!store.busy.isEmpty || (store.fixtureState != nil && store.roots.isEmpty))
    }

    var home: some View {
        VStack(alignment: .leading, spacing: 15) {
            ZStack(alignment: .leading) {
                LinearGradient(colors: [.black.opacity(0.3), lilac.opacity(0.25)], startPoint: .leading, endPoint: .trailing)
                if let url = resource("storage.jpg", source: "设计稿/assets/storage.jpg"), let image = NSImage(contentsOf: url) {
                    GeometryReader { proxy in
                        Image(nsImage: image).resizable().scaledToFill().frame(width: proxy.size.width * 0.57, height: proxy.size.height).clipped()
                            .mask(LinearGradient(colors: [.clear, .black, .black], startPoint: .leading, endPoint: .trailing))
                            .frame(width: proxy.size.width, alignment: .trailing)
                    }
                }
                VStack(alignment: .leading, spacing: 10) {
                    Label("预计可低影响清理", systemImage: "sparkles").foregroundStyle(lilac)
                    Text(bytes(safe.reduce(0) { $0 + $1.size })).font(.system(size: 42, weight: .semibold, design: .rounded))
                    Text("\(safe.count) 个候选文件 · \(store.roots.count) 个扫描目录").foregroundStyle(.secondary)
                    Button("审阅清理项目") { review(verdict: "safe") }.buttonStyle(.borderedProminent).disabled(safe.isEmpty)
                }.padding(22)
            }.frame(height: 205).clipShape(RoundedRectangle(cornerRadius: 16))
            if store.roots.isEmpty {
                Panel {
                    Text("从一个目录开始").font(.headline)
                    Text("选择下载、素材或工程目录，或先用独立样本体验完整流程。").foregroundStyle(.secondary)
                    HStack {
                        Button("选择扫描目录") { store.addScope(deep: deep) }.disabled(store.fixtureState != nil)
                        Button("创建演练样本") { store.fixture() }
                    }.disabled(!store.busy.isEmpty)
                }
            }
            DiskPanel(path: store.roots.first ?? NSHomeDirectory(), generation: store.generation)
            HStack(alignment: .top, spacing: 12) {
                Panel {
                    Label("应用占用排行", systemImage: Page.apps.icon).font(.headline)
                    let top = rankedApps(store.data.rows("apps"), files: store.files, scanID: store.data.string("scan_id"), bySafe: false).prefix(3)
                    if top.isEmpty { Text("尚无应用占用记录").foregroundStyle(.secondary) }
                    ForEach(top) { app in
                        Button { review(owner: app.id) } label: {
                            HStack {
                                Image(nsImage: NSWorkspace.shared.icon(forFile: app.id)).resizable().frame(width: 22, height: 22)
                                Text(app.app.string("name")).lineLimit(1)
                                Spacer(); Text(bytes(app.size)).monospacedDigit()
                            }
                        }.buttonStyle(.plain)
                    }
                    Button("查看应用排行") { page = .apps }
                }
                Panel {
                    Label("Agent 工作区", systemImage: Page.agents.icon).font(.headline)
                    let top = workspacePartitions(projects: store.projects, files: store.files, bySafe: false).prefix(3)
                    if top.isEmpty { Text("尚无工作区占用记录").foregroundStyle(.secondary) }
                    ForEach(top) { group in
                        Button { expandedGroups.insert(group.id); page = .agents } label: {
                            HStack {
                                Text(group.id.title).lineLimit(1); Spacer(); Text(bytes(group.stats.bytes)).monospacedDigit()
                            }
                        }.buttonStyle(.plain)
                    }
                    Button("查看项目分区") { page = .agents }
                }
            }
            Button("扫描范围与覆盖缺口（\(store.data.paths("issues").count)）") { page = .scopes }.buttonStyle(.link)
        }
    }

    var scopes: some View {
        VStack(alignment: .leading, spacing: 14) {
            Panel {
                Text("已授权目录").font(.headline)
                if store.roots.isEmpty { Text("尚未选择目录").foregroundStyle(.secondary) }
                ForEach(store.roots, id: \.self) { root in
                    HStack {
                        Text(root).textSelection(.enabled)
                        Spacer()
                        Button("撤回") { store.call("settings", ["roots": store.roots.filter { $0 != root }]) }
                    }
                }
                Divider()
                Toggle("深度扫描", isOn: $deep)
                Toggle("每 15 分钟在总览空闲时重扫已授权目录", isOn: $scheduled).disabled(store.fixtureState != nil)
                HStack {
                    Button("添加目录并扫描") { store.addScope(deep: deep) }.disabled(store.fixtureState != nil)
                    Button("使用独立演练目录") { store.fixture() }.help("创建新的本地样本并将扫描范围切换为样本目录")
                }
            }.disabled(!store.busy.isEmpty)
            Panel {
                Text("覆盖缺口 · \(store.data.paths("issues").count)").font(.headline)
                if store.data.paths("issues").isEmpty { Text("当前索引没有记录覆盖缺口。").foregroundStyle(.secondary) }
                ForEach(Array(store.data.paths("issues").enumerated()), id: \.offset) { issue in
                    Text(issue.element).textSelection(.enabled)
                }
            }
            Panel {
                let rule = "vscode.1.95.3.main-log.v1"
                let rules = store.data.paths("automatic_rules")
                Text("任务结束后的自动清理").font(.headline)
                Text("导入显式任务结束事件时，可自动清理命中已验证 VS Code 1.95.3 轮转日志规则的文件。").foregroundStyle(.secondary)
                if rules.contains(rule) {
                    Button("停用自动清理") { store.call("settings", ["automatic_rules": rules.filter { $0 != rule }]) }
                } else {
                    Button("启用此规则…") { automaticConfirmation = true }
                }
            }.disabled(!store.busy.isEmpty)
            .alert("允许在任务结束事件后自动删除日志？", isPresented: $automaticConfirmation) {
                Button("取消", role: .cancel) {}
                Button("确认启用", role: .destructive) {
                    store.call("settings", ["automatic_rules": ["vscode.1.95.3.main-log.v1"]])
                }
            } message: {
                Text("导入结束事件后，引擎可直接移除授权目录内符合该版本规则且经复核的旧日志，无法通过既白恢复。")
            }
            ForEach(["keep", "exclude"], id: \.self) { key in
                Panel {
                    HStack {
                        Text(key == "keep" ? "长期保留" : "排除路径").font(.headline)
                        Spacer()
                        Button("添加目录…") { store.markDirectory(key) }.disabled(store.roots.isEmpty)
                    }
                    if store.data.paths(key).isEmpty { Text("暂无标记").foregroundStyle(.secondary) }
                    ForEach(store.data.paths(key), id: \.self) { path in
                        HStack { Text(path).textSelection(.enabled); Spacer(); Button("移除标记") {
                            store.call("settings", [key: store.data.paths(key).filter { $0 != path }])
                        }}
                    }
                }.disabled(!store.busy.isEmpty)
            }
        }
    }
}

struct DiskPanel: View {
    var path: String
    var generation: Int
    var body: some View {
        let disk = try? FileManager.default.attributesOfFileSystem(forPath: path)
        let total = (disk?[.systemSize] as? NSNumber)?.int64Value ?? 0
        let free = (disk?[.systemFreeSize] as? NSNumber)?.int64Value ?? 0
        return Panel {
            HStack { Label("卷空间", systemImage: "internaldrive").font(.headline); Spacer()
                Text(total > 0 ? "可用 \(bytes(free)) / \(bytes(total))" : "读数不可用").monospacedDigit()
            }
            if total > 0 { ProgressView(value: Double(max(0, total-free)), total: Double(total)) }
            Text(path).font(.caption).foregroundStyle(.secondary)
        }
    }
}
