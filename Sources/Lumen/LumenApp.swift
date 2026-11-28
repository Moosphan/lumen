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
    case home = "空间总览", apps = "应用排行", agents = "Agent 工作区", models = "模型存储"
    case review = "清理建议", large = "大文件清理", history = "清理记录", scopes = "扫描范围"
    var icon: String {
        switch self {
        case .home: return "square.grid.2x2"
        case .apps: return "macwindow"
        case .agents: return "square.3.layers.3d"
        case .models: return "cpu"
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
    @State private var showingUnclassified = false
    @State var verdict = ""
    @AppStorage("scheduledLocalInventory") var scheduled = false
    @State var lastScheduledScan = Date()
    @State var automaticConfirmation = false
    let ticker = Timer.publish(every: 60, on: .main, in: .common).autoconnect()
    private var onUnclassifiedPage: Bool { page == .home && showingUnclassified }
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
                        showingUnclassified = false
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
                    Button("返回本机工作区") { store.exitFixture(); showingUnclassified = false; page = .home }
                        .disabled(!store.busy.isEmpty)
                } else {
                    Button("创建演练样本") { store.fixture(); showingUnclassified = false; page = .home }
                        .disabled(!store.busy.isEmpty).buttonStyle(.plain).foregroundStyle(.secondary)
                }
            }.padding(16).frame(width: 180).background(Color.black.opacity(0.15))
            Divider()
            VStack(alignment: .leading, spacing: 0) {
                GeometryReader { viewport in
                    ScrollViewReader { scroll in
                        ScrollView {
                            VStack(alignment: .leading, spacing: 16) {
                                VStack(alignment: .leading, spacing: 14) {
                                    if onUnclassifiedPage {
                                        Button {
                                            showingUnclassified = false
                                        } label: {
                                            Label("返回空间总览", systemImage: "chevron.left")
                                        }.buttonStyle(.link).keyboardShortcut(.cancelAction)
                                    }
                                    HStack {
                                        Text(pageTitle).font(.system(size: 32, weight: .semibold))
                                        Spacer()
                                        if !onUnclassifiedPage && (page == .home || page == .scopes)
                                            && (!store.needsDiskAccess || store.fixtureState != nil) {
                                            scanControl
                                        }
                                    }
                                }.id("workspace-heading")
                                if store.scanning {
                                    HStack {
                                        ProgressView().controlSize(.small)
                                        Text("正在扫描本机位置…")
                                        Button("取消扫描") { store.cancelScan() }.buttonStyle(.link)
                                    }.foregroundStyle(.secondary)
                                }
                                if store.needsDiskAccess && store.fixtureState == nil && !onUnclassifiedPage {
                                    diskAccessPanel
                                }
                                if !store.needsDiskAccess || store.fixtureState != nil || page == .home || page == .apps {
                                    switch page {
                                    case .home:
                                        if onUnclassifiedPage {
                                            UnclassifiedUsageDetail(store: store)
                                        } else {
                                            home
                                        }
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
                                    case .models: ModelInventoryView(store: store)
                                    case .review: ReviewView(store: store, owner: $owner, project: $project, verdict: $verdict,
                                                             bucketPaths: $bucketPaths, path: $reviewPath)
                                    case .large:
                                        SpaceBrowser(store: store, canvasHeight: max(320, viewport.size.height - 226),
                                                     didOpenLevel: { scroll.scrollTo("workspace-heading", anchor: .top) },
                                                     reviewMedia: { group in
                                            review(project: MediaReviewGroup.titles[group.id] ?? group.id)
                                            bucketPaths = group.paths
                                        }) { path, enroll in
                                            review()
                                            reviewPath = path
                                            if enroll { store.scan(store.roots + [path], deep: false, renewRoots: [path]) }
                                        }
                                    case .history: HistoryView(store: store)
                                    case .scopes: scopes
                                    }
                                }
                            }.padding(26).frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .onChange(of: showingUnclassified) { _ in
                            scroll.scrollTo("workspace-heading", anchor: .top)
                        }
                    }
                }
            }
        }.background(canvas).tint(lilac).preferredColorScheme(.dark)
        .task { startInventory() }
        .onReceive(ticker) { now in
            guard scheduled, !store.needsDiskAccess, store.fixtureState == nil, now.timeIntervalSince(lastScheduledScan) >= 900, page == .home,
                  !showingUnclassified, store.busy.isEmpty, NSApp.isActive, store.error == nil,
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

    private var pageTitle: String {
        if onUnclassifiedPage { return "未归类占用明细" }
        if page == .apps && selectedApplication != nil { return "应用清理详情" }
        return page.rawValue
    }

    var scanControl: some View {
        Button {
            store.scanLocal(deep: deep)
        } label: { Label("重新扫描", systemImage: "arrow.clockwise") }
        .buttonStyle(.bordered).disabled(!store.busy.isEmpty)
    }

    private func startInventory() {
        store.startInventory { data in
            if let preference = data["deep"] as? Bool { deep = preference }
        }
    }

    var diskAccessPanel: some View {
        Panel {
            Label("当前仅盘点应用程序", systemImage: "lock.shield")
                .font(.title2.weight(.semibold)).foregroundStyle(lilac)
            Text("扫描进程暂未能确认完全磁盘访问。如果系统开关已经开启，请退出并重新打开当前版本。若同时保留了多个版本，可在 Finder 中核对系统设置授权的是哪一个。")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if Bundle.main.bundleURL.pathExtension == "app" {
                Text(displayPath(Bundle.main.bundleURL.path))
                    .font(.caption.monospaced()).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle).textSelection(.enabled)
            }
            HStack(spacing: 12) {
                Button("打开系统设置") { store.openDiskAccessSettings() }
                    .buttonStyle(.borderedProminent)
                Button("再次检测并扫描") { startInventory() }
                    .buttonStyle(.bordered)
                if Bundle.main.bundleURL.pathExtension == "app" {
                    Button("在 Finder 中显示当前版本") {
                        NSWorkspace.shared.activateFileViewerSelecting([Bundle.main.bundleURL])
                    }.buttonStyle(.link)
                }
            }.disabled(!store.busy.isEmpty)
        }
    }

    var home: some View {
        SpaceOverview(store: store, review: { review(verdict: $0) },
                      applications: { page = .apps }, workspaces: { page = .agents },
                      unclassified: { refresh in
                          showingUnclassified = true
                          if (refresh || store.diskUsage == nil) && !store.needsDiskAccess {
                              store.call("scan_disk_usage")
                          }
                      })
    }

    var scopes: some View {
        VStack(alignment: .leading, spacing: 14) {
            if store.fixtureState == nil {
                Panel {
                    Label("磁盘访问权限", systemImage: "lock.shield").font(.headline)
                    Text("在系统设置中管理完全磁盘访问。每次扫描前会重新检测权限。")
                        .font(.callout).foregroundStyle(.secondary)
                    Button("管理系统权限") { store.openDiskAccessSettings() }
                }
            }
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
            MCPCleanupPanel(store: store)
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
