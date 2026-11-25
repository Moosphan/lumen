import AppKit
import SwiftUI

private struct RecoveryFile: Identifiable {
    let raw: Object
    var id: String {
        let saved = raw.string("id")
        return saved.isEmpty ? raw.string("filename") : saved
    }
    var name: String { raw.string("filename") }
}

struct ModelRecoveryPanel: View {
    @ObservedObject var store: Store
    let modelID: String
    @State private var options: [RecoveryFile] = []
    @State private var checked = false
    @State private var optionScan = ""
    @State private var result: Object = [:]

    var body: some View {
        Panel {
            HStack {
                Label("重新获取模型文件", systemImage: "arrow.down.circle").font(.headline)
                Spacer()
                Button("检查固定修订") {
                    let scan = store.data.string("scan_id")
                    store.call("model_recovery_options", ["scan_id": scan, "model_id": modelID]) { reply in
                        options = reply.rows("options").map(RecoveryFile.init)
                        checked = true
                        optionScan = scan
                    }
                }.disabled(!store.busy.isEmpty)
            }
            if checked && options.isEmpty {
                Text("当前盘点中没有具备固定修订及 SHA-256 证据的权重文件。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(options) { file in
                recoveryRow(file) {
                    Button("保存恢复凭据") {
                        store.call("model_pin", request(for: file)) { reply in
                            guard let recipe = reply["recipe"] as? Object else { return }
                            let id = recipe.string("id")
                            store.data["model_recovery_recipes"] = store.data.rows("model_recovery_recipes")
                                .filter { $0.string("id") != id } + [recipe]
                            result = ["status": "pinned"]
                        }
                    }
                    Button("获取并校验…") {
                        chooseRecoveryFolder(store, fields: request(for: file)) {
                            result = $0
                        }
                    }
                }.disabled(!store.busy.isEmpty || optionScan != store.data.string("scan_id"))
            }
            recoveryStatus(store, result: result)
        }
    }

    private func request(for file: RecoveryFile) -> Object {
        ["scan_id": optionScan, "model_id": modelID, "filename": file.name,
         "digest": file.raw.string("digest"), "revision": file.raw.string("revision"), "bytes": file.raw.number("bytes")]
    }
}

struct SavedModelRecoveryPanel: View {
    @ObservedObject var store: Store
    @State private var result: Object = [:]

    var body: some View {
        let files = store.data.rows("model_recovery_recipes").map(RecoveryFile.init)
        if !files.isEmpty {
            Panel {
                DisclosureGroup("模型恢复凭据（\(files.count)）") {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        Text("按保存的固定修订下载，在所选目录生成已校验的副本。")
                            .font(.caption).foregroundStyle(.secondary)
                        ForEach(files) { file in
                            recoveryRow(file) {
                                Button("移除凭据") {
                                    store.call("model_forget", ["recipe_id": file.id]) { _ in
                                        store.data["model_recovery_recipes"] = store.data.rows("model_recovery_recipes")
                                            .filter { $0.string("id") != file.id }
                                    }
                                }
                                Button("重新获取…") {
                                    chooseRecoveryFolder(store, fields: ["recipe_id": file.id]) { result = $0 }
                                }
                            }.disabled(!store.busy.isEmpty)
                        }
                    }.padding(.top, 8)
                }
                recoveryStatus(store, result: result)
            }
        }
    }
}

@MainActor
private func chooseRecoveryFolder(_ store: Store, fields: Object, done: @escaping (Object) -> Void) {
    let panel = NSOpenPanel()
    panel.title = "选择下载目录或已授权的普通文件夹"
    panel.prompt = "在此获取"
    panel.canChooseDirectories = true
    panel.canChooseFiles = false
    panel.canCreateDirectories = true
    guard panel.runModal() == .OK, let directory = panel.url?.resolvingSymlinksInPath().path else { return }
    store.call("model_recover", fields.merging(["destination": directory]) { _, latest in latest }, done: done)
}

private func recoveryRow<Trailing: View>(_ file: RecoveryFile, @ViewBuilder buttons: () -> Trailing) -> some View {
    HStack(alignment: .center, spacing: 10) {
        VStack(alignment: .leading, spacing: 4) {
            Text(file.name).font(.callout.weight(.medium)).lineLimit(1).truncationMode(.middle).help(file.name)
            Text("\(file.raw.string("repository")) · \(String(file.raw.string("revision").prefix(12)))")
                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Text(file.raw.string("digest")).font(.caption2.monospaced()).foregroundStyle(.secondary)
                .lineLimit(1).truncationMode(.middle).help(file.raw.string("digest")).textSelection(.enabled)
        }
        Spacer(minLength: 6)
        Text(bytes(file.raw.number("bytes"))).font(.caption.monospacedDigit())
        buttons()
    }
}

@ViewBuilder
@MainActor
private func recoveryStatus(_ store: Store, result: Object) -> some View {
    if store.busy == "model_recover" {
        HStack {
            ProgressView("正在下载并核对内容摘要…").controlSize(.small)
            Spacer()
            Button("取消获取") { store.cancelModelRecovery() }
        }
    } else {
        switch result.string("status") {
        case "verified":
            HStack {
                Text("文件校验通过。模型工具可见性待核验。").font(.caption).foregroundStyle(.secondary)
                Spacer()
                FinderButton(path: result.string("path"), reveal: true)
            }
        case "pinned":
            Text("已保存恢复凭据。").font(.caption).foregroundStyle(.secondary)
        case "cancelled":
            Text("已取消获取。").font(.caption).foregroundStyle(.secondary)
        default:
            EmptyView()
        }
    }
}
