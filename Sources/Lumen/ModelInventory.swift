import SwiftUI
import AppKit

private struct IndexedModel: Identifiable {
    let raw: Object
    var id: String { raw.string("id") }
    var name: String { raw.string("name") }
    var tool: String { raw.string("tool") }
    var path: String { raw.string("path") }
    var version: String { raw.string("version") }
    var quantization: String { raw.string("quantization") }
    var physical: Int64 { raw.number("physical_bytes") }
    var shared: Int64 { raw.number("shared_bytes") }
    var unique: Int64 { raw.number("unique_bytes") }
    var activeLabel: String {
        switch raw.string("active_state") {
        case "loaded": return "运行中"
        case "not_loaded": return "当前服务未加载"
        default: return "运行状态未知"
        }
    }
}

struct ModelInventoryView: View {
    @ObservedObject var store: Store
    @State private var query = ""
    @State private var tool = ""
    @State private var selected: IndexedModel?
    @State private var runtimeStates: [String: Object] = [:]
    @State private var runtimeMessage = ""

    var body: some View {
        let inventory = store.data["model_inventory"] as? Object ?? [:]
        let models = inventory.rows("models").map { row in
            IndexedModel(raw: row.merging(runtimeStates[row.string("id")] ?? [:]) { _, newer in newer })
        }
        let providers = Array(Set(models.map(\.tool))).sorted()
        let search = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let filtered = models.filter { item in
            (tool.isEmpty || item.tool == tool) &&
            (search.isEmpty || [item.name, item.tool, item.version, item.quantization, item.path]
                .contains(where: { $0.localizedCaseInsensitiveContains(search) }))
        }.sorted { $0.physical == $1.physical ? $0.id < $1.id : $0.physical > $1.physical }

        VStack(alignment: .leading, spacing: 14) {
            Panel {
                HStack(spacing: 20) {
                    metric("模型与修订", models.count.formatted())
                    Divider().frame(height: 30)
                    metric("去重占用", bytes(inventory.number("physical_bytes")))
                    Divider().frame(height: 30)
                    metric("共享占用", bytes(inventory.number("shared_bytes")))
                    Spacer(minLength: 4)
                }
            }
            HStack(spacing: 12) {
                TextField("搜索模型、版本或量化", text: $query)
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 244)
                Picker("模型工具", selection: $tool) {
                    Text("全部工具").tag("")
                    ForEach(providers, id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(width: 150).accessibilityLabel("按模型工具筛选")
                Spacer()
                Menu("核验运行状态") {
                    Button("Ollama") { checkRuntime("ollama", name: "Ollama") }.disabled(!providers.contains("Ollama"))
                    Button("LM Studio") { checkRuntime("lmstudio", name: "LM Studio") }.disabled(!providers.contains("LM Studio"))
                }.fixedSize().disabled(!store.busy.isEmpty ||
                    !providers.contains("Ollama") && !providers.contains("LM Studio"))
                Menu("添加模型目录…") {
                    ForEach(["Ollama", "Hugging Face", "LM Studio", "ComfyUI"], id: \.self) { name in
                        Button(name) { addFolder(for: name) }
                    }
                }.fixedSize().disabled(!store.busy.isEmpty)
            }
            if !runtimeMessage.isEmpty {
                Text(runtimeMessage).font(.caption).foregroundStyle(.secondary)
            }
            SavedModelRecoveryPanel(store: store)
            if filtered.isEmpty {
                ContentPlaceholder(icon: "cpu", title: store.scanning ? "正在盘点模型" : "未找到匹配的模型",
                    message: models.isEmpty ? "模型盘点覆盖已识别的本地存储。自定义目录可从扫描范围添加。" : "试试模型名称、量化或工具名称。")
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(filtered) { model in
                        if model.id != filtered.first?.id { Divider().padding(.leading, 53) }
                        modelRow(model).padding(.horizontal, 14).padding(.vertical, 10)
                    }
                }.background(surface, in: RoundedRectangle(cornerRadius: 13))
            }
            let issues = inventory.paths("issues")
            if !issues.isEmpty {
                Panel {
                    DisclosureGroup("盘点缺口（\(issues.count.formatted())）") {
                        LazyVStack(alignment: .leading, spacing: 8) {
                            ForEach(issues, id: \.self) { issue in
                                Text(issue).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                            }
                        }.padding(.top, 6)
                    }
                }
            }
        }.onChange(of: providers) { options in
            if !tool.isEmpty && !options.contains(tool) { tool = "" }
        }.onChange(of: store.data.string("scan_id")) { _ in
            runtimeStates = [:]
            runtimeMessage = ""
        }.sheet(item: $selected) { model in
            ModelDetailView(store: store, model: model)
        }
    }

    private func modelRow(_ model: IndexedModel) -> some View {
        let subtitle = [model.tool, model.version, model.quantization].filter { !$0.isEmpty }.joined(separator: " · ")
        return HStack(spacing: 11) {
            Image(systemName: "cpu").foregroundStyle(lilac)
                .frame(width: 29, height: 29).background(lilac.opacity(0.09), in: RoundedRectangle(cornerRadius: 8))
            Button { selected = model } label: {
                VStack(alignment: .leading, spacing: 4) {
                    Text(model.name).font(.callout.weight(.semibold)).lineLimit(1)
                    Text(subtitle).font(.caption).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle).help(subtitle)
                }
            }.buttonStyle(.plain).accessibilityLabel(model.name)
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 4) {
                Text(bytes(model.physical)).font(.callout.weight(.semibold)).monospacedDigit()
                if model.shared > 0 { Text("共享 " + bytes(model.shared)).font(.caption).foregroundStyle(.secondary) }
                if model.raw.string("active_state") == "loaded" {
                    Text(model.activeLabel).font(.caption).foregroundStyle(lilac)
                }
            }
            FinderButton(path: model.path, reveal: true)
            Button { selected = model } label: {
                Image(systemName: "info.circle")
            }.buttonStyle(.borderless).help("模型详情").accessibilityLabel("模型详情")
        }
    }

    private func addFolder(for tool: String) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.prompt = "添加模型目录"
        guard panel.runModal() == .OK, let path = panel.url?.resolvingSymlinksInPath().path else { return }
        var locations = store.data.rows("model_roots").filter { $0.string("path") != path }
        locations.append(["path": path, "tool": tool])
        let roots = Array(Set(store.roots + [path])).sorted()
        store.call("settings", ["roots": roots, "renew_roots": [path],
                                "model_roots": locations])
    }

    private func checkRuntime(_ tool: String, name: String) {
        runtimeMessage = "正在核验 \(name) 本地服务…"
        store.call("model_runtime", ["tool": tool]) { response in
            let updates = response["models"] as? [String: Object] ?? [:]
            runtimeStates.merge(updates) { _, state in state }
            runtimeMessage = response.string("status") == "ok"
                ? "\(name) 运行状态已核验"
                : "无法核验 \(name) 的本地服务。受影响的模型显示为状态未知。"
        }
    }

    private func metric(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(size: 23, weight: .semibold, design: .rounded)).monospacedDigit()
        }
    }
}

private struct ModelDetailView: View {
    @ObservedObject var store: Store
    let model: IndexedModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(model.name).font(.title2.weight(.semibold))
                    Text([model.tool, model.version, model.quantization].filter { !$0.isEmpty }.joined(separator: " · "))
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.cancelAction).disabled(store.busy == "model_recover")
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 15) {
                    Panel {
                        HStack {
                            Label("保留模型", systemImage: "lock").foregroundStyle(lilac)
                            Spacer()
                            Text("独占 \(bytes(model.unique)) · 共享 \(bytes(model.shared))")
                                .font(.callout.monospacedDigit())
                        }
                        Text(model.raw.string("source")).font(.callout).foregroundStyle(.secondary)
                        Text(model.activeLabel).font(.callout).foregroundStyle(.secondary)
                        let evidence = model.raw.string("active_source")
                        if !evidence.isEmpty { Text(evidence).font(.caption).foregroundStyle(.secondary) }
                        let digest = model.raw.string("digest")
                        if !digest.isEmpty {
                            Text(digest).font(.caption.monospaced()).textSelection(.enabled)
                        }
                    }
                    if model.tool == "Hugging Face" {
                        ModelRecoveryPanel(store: store, modelID: model.id)
                    }
                    records("模型文件", values: model.raw.paths("files"), finder: true)
                    records("共享权重", values: model.raw.paths("shared_files"), finder: true)
                    records("修订引用", values: model.raw.paths("refs"))
                    records("工作流引用", values: model.raw.paths("workflow_refs"))
                    records("待核验信息", values: model.raw.paths("issues"))
                }
            }
        }.padding(22).frame(width: 640, height: 470)
    }

    @ViewBuilder
    private func records(_ title: String, values: [String], finder: Bool = false) -> some View {
        if !values.isEmpty {
            Panel {
                Text(title).font(.headline)
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(values, id: \.self) { value in
                        HStack(alignment: .top) {
                            Text(value.hasPrefix("/") ? displayPath(value) : value).font(.caption)
                                .textSelection(.enabled)
                            Spacer()
                            if finder { FinderButton(path: value, reveal: true) }
                        }
                    }
                }
            }
        }
    }
}
