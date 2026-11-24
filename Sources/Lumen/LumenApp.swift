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
    case home = "空间总览", apps = "应用排行", agents = "Agent 工作区", review = "清理建议", large = "大文件清理", history = "清理记录", scopes = "扫描范围"
    var icon: String {
        switch self {
        case .home: return "square.grid.2x2"
        case .apps: return "macwindow"
        case .agents: return "square.3.layers.3d"
        case .review: return "sparkles"
        case .large: return "chart.bar.xaxis"
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
    @State var reviewPath = ""
    @State var selectedApplication: String?
    @State var verdict = ""
    @AppStorage("scheduledLocalInventory") var scheduled = false
    @State var lastScheduledScan = Date()
    @State var automaticConfirmation = false
    let ticker = Timer.publish(every: 60, on: .main, in: .common).autoconnect()
    func review(owner: String = "", project: String = "", verdict: String = "") {
        self.owner = owner
        self.project = project
        self.bucketPaths = nil
        self.reviewPath = ""
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
                        selectedApplication = nil
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
                if store.fixtureState != nil {
                    Button("返回本机工作区") { store.exitFixture(); page = .home }
                        .disabled(!store.busy.isEmpty)
                } else {
                    Button("创建演练样本") { store.fixture(); page = .home }
                        .disabled(!store.busy.isEmpty).buttonStyle(.plain).foregroundStyle(.secondary)
                }
            }.padding(16).frame(width: 180).background(Color.black.opacity(0.15))
            Divider()
            VStack(alignment: .leading, spacing: 0) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        HStack {
                            Text(page == .apps && selectedApplication != nil ? "应用清理详情" : page.rawValue)
                                .font(.system(size: 32, weight: .semibold))
                            Spacer()
                            if page == .home || page == .scopes {
                                scanControl
                            }
                        }
                        if store.scanning {
                            HStack {
                                ProgressView().controlSize(.small)
                                Text("正在扫描本机位置…")
                                Button("取消扫描") { store.cancelScan() }.buttonStyle(.link)
                            }.foregroundStyle(.secondary)
                        }
                        switch page {
                        case .home: home
                        case .apps:
                            if let path = selectedApplication,
                               let app = store.inventory.apps.first(where: { $0.id == path }) {
                                AppCleanupDetail(store: store, app: app) { selectedApplication = nil }.id(path)
                            } else {
                                AppRanking(store: store, drill: { review(owner: $0, verdict: $1) },
                                           cleanup: { selectedApplication = $0 })
                            }
                        case .agents: WorkspaceView(store: store, drill: { bucket in
                            review(project: bucket.title)
                            bucketPaths = bucket.paths
                        })
                        case .review: ReviewView(store: store, owner: $owner, project: $project, verdict: $verdict,
                                                 bucketPaths: $bucketPaths, path: $reviewPath)
                        case .large:
                            SpaceBrowser(store: store) { path, enroll in
                                review()
                                reviewPath = path
                                if enroll { store.scan(store.roots + [path], deep: false, renewRoots: [path]) }
                            }
                        case .history: HistoryView(store: store)
                        case .scopes: scopes
                        }
                    }.padding(26).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }.background(canvas).tint(lilac).preferredColorScheme(.dark)
        .task {
            store.startInventory { data in
                deep = data["deep"] as? Bool ?? false
            }
        }
        .onReceive(ticker) { now in
            guard scheduled, store.fixtureState == nil, now.timeIntervalSince(lastScheduledScan) >= 900, page == .home,
                  store.busy.isEmpty, NSApp.isActive, store.error == nil,
                  NSApp.modalWindow == nil, NSApp.keyWindow?.attachedSheet == nil else { return }
            lastScheduledScan = now
            store.scanLocal(deep: deep)
        }
        .onChange(of: store.busy) { command in
            if command == "scan" || command == "scan_local" { lastScheduledScan = Date() }
        }
        .sheet(item: $store.result) { report in ResultSheet(report: report) }
        .alert("操作未完成", isPresented: Binding(get: { store.error != nil }, set: { if !$0 { store.error = nil } })) {
            Button("好") { store.error = nil }
        } message: { Text(store.error ?? "") }
    }

    var scanControl: some View {
        Button {
            store.scanLocal(deep: deep)
        } label: { Label("重新扫描", systemImage: "arrow.clockwise") }
        .buttonStyle(.bordered).disabled(!store.busy.isEmpty)
    }

    var home: some View {
        SpaceOverview(store: store, review: { review(verdict: $0) },
                      applications: { page = .apps }, workspaces: { page = .agents })
    }

    var scopes: some View {
        VStack(alignment: .leading, spacing: 14) {
            if store.fixtureState == nil { SuggestedScopes(store: store, deep: deep) }
            Panel {
                Text("已授权目录").font(.headline)
                if store.roots.isEmpty { Text("尚未选择目录").foregroundStyle(.secondary) }
                ForEach(store.roots, id: \.self) { root in
                    HStack {
                        Text(displayPath(root)).font(.callout).lineLimit(1).truncationMode(.middle)
                            .help(root).accessibilityLabel(root)
                        Spacer()
                        FinderButton(path: root)
                        Button("撤回") { store.call("settings", ["roots": store.roots.filter { $0 != root }]) }
                    }
                }
                Divider()
                Toggle("深度扫描", isOn: $deep)
                Toggle("在总览空闲时每 15 分钟扫描", isOn: $scheduled).disabled(store.fixtureState != nil)
                HStack {
                    Button("添加目录并扫描") { store.addScope(deep: deep) }.disabled(store.fixtureState != nil)
                    Button("使用独立演练目录") { store.fixture() }.help("创建新的本地样本并将扫描范围切换为样本目录")
                }
            }.disabled(!store.busy.isEmpty)
            Panel {
                Text("覆盖缺口 · \(store.data.paths("issues").count)").font(.headline)
                if store.data.paths("issues").isEmpty { Text("当前索引没有记录覆盖缺口。").foregroundStyle(.secondary) }
                DisclosureGroup("查看扫描说明") {
                    ForEach(Array(store.data.paths("issues").prefix(30).enumerated()), id: \.offset) { _, issue in
                        Text(issue).font(.caption).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                    }
                    if store.data.paths("issues").count > 30 {
                        Text("显示前 30 条").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Panel {
                let rule = "vscode.1.95.3.main-log.v1"
                let quarantineRule = "task.declared-temporary.quarantine.v1"
                let rules = store.data.paths("automatic_rules")
                Text("Agent 任务收尾").font(.headline)
                Toggle("结束任务时隔离已登记的临时产物", isOn: Binding(
                    get: { rules.contains(quarantineRule) },
                    set: { enabled in
                        let next = rules.filter { $0 != quarantineRule } + (enabled ? [quarantineRule] : [])
                        store.call("settings", ["automatic_rules": next])
                    }
                ))
                Text("在「Agent 工作区」登记临时文件。手动结束任务或收到关联的 Claude 会话结束事件时，既白会核对文件变化及已知引用。可在清理记录里恢复隔离的文件。").foregroundStyle(.secondary)
                Divider()
                Text("VS Code 1.95.3 过期日志").font(.headline)
                if rules.contains(rule) {
                    Button("停用自动移除日志") { store.call("settings", ["automatic_rules": rules.filter { $0 != rule }]) }
                } else {
                    Button("启用自动移除日志…") { automaticConfirmation = true }
                }
            }.disabled(!store.busy.isEmpty)
            .alert("允许在任务结束事件后自动删除日志？", isPresented: $automaticConfirmation) {
                Button("取消", role: .cancel) {}
                Button("确认启用", role: .destructive) {
                    let next = Set(store.data.paths("automatic_rules")).union(["vscode.1.95.3.main-log.v1"])
                    store.call("settings", ["automatic_rules": next.sorted()])
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
