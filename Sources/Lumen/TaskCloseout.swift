import SwiftUI

private enum TaskCloseoutCategory: String, CaseIterable, Identifiable {
    case input, output, render, cache, model, temporary

    var id: Self { self }
    var title: String {
        switch self {
        case .input: "原料"
        case .output: "交付物"
        case .render: "临时渲染"
        case .cache: "缓存"
        case .model: "模型"
        case .temporary: "其他临时文件"
        }
    }
}

private struct TaskCloseoutItem: Identifiable {
    let raw: Object

    var id: String { raw.string("path") }
    var name: String { URL(fileURLWithPath: id).lastPathComponent }
    var category: TaskCloseoutCategory { TaskCloseoutCategory(rawValue: raw.string("category")) ?? .temporary }
    var indexed: Bool { raw["indexed"] as? Bool == true }
    var verdict: String { raw.string("decision") }
    var reviewable: Bool { indexed && (verdict == "review" || verdict == "safe") }
    var projects: [String] { raw.paths("projects") }
    var references: [Object] { raw.rows("references") }
    var history: [Object] { raw.rows("actions") }
    var restoredPath: String {
        history.last { $0.string("status") == "restored" }?.string("restored_to") ?? ""
    }

    var otherRoles: [String] {
        let labels = ["inputs": "原料", "outputs": "交付物", "temporary": "临时文件"]
        let roles = raw.paths("roles")
        return roles.count > 1 ? roles.compactMap { labels[$0] } : []
    }
}

struct TaskCloseoutView: View {
    let summary: Object
    var review: ((String) -> Void)? = nil
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        let items = summary.rows("items").map(TaskCloseoutItem.init)
        let grouped = Dictionary(grouping: items, by: \.category)

        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 5) {
                Text("任务收尾单").font(.title2.bold())
                Text([summary.string("agent"), displayPath(summary.string("root"))]
                    .filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.callout).foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            Divider()
            if items.isEmpty {
                ContentPlaceholder(icon: "checklist", title: "没有登记文件",
                                   message: "此任务结束时没有登记原料、交付物或临时文件。")
                Spacer(minLength: 0)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 19) {
                        ForEach(TaskCloseoutCategory.allCases) { category in
                            if let rows = grouped[category] {
                                VStack(alignment: .leading, spacing: 0) {
                                    HStack(spacing: 8) {
                                        Text(category.title).font(.headline)
                                        Text(rows.count.formatted()).font(.caption).foregroundStyle(.secondary)
                                    }.padding(.bottom, 7)
                                    ForEach(rows) { item in
                                        Divider()
                                        closeoutRow(item).padding(.vertical, 10)
                                    }
                                }
                            }
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            Divider()
            HStack {
                Spacer()
                Button("关闭") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }.padding(24).frame(minWidth: 570, idealWidth: 690, minHeight: 430, idealHeight: 560)
    }

    private func closeoutRow(_ item: TaskCloseoutItem) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(item.name).font(.callout.weight(.semibold))
                    Text(displayPath(item.id))
                        .font(.caption).foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 4) {
                    Text(verdicts[item.verdict] ?? "未核对").font(.callout.weight(.medium))
                    if item.raw["size"] != nil {
                        Text(bytes(item.raw.number("size"))).font(.caption).monospacedDigit().foregroundStyle(.secondary)
                    }
                }
                if item.indexed {
                    FinderButton(path: item.id, reveal: true)
                } else if !item.restoredPath.isEmpty {
                    FinderButton(path: item.restoredPath, reveal: true)
                }
                if let review, item.reviewable {
                    Button("清理审阅") {
                        dismiss()
                        review(item.id)
                    }
                }
            }
            Text(item.raw.string("reason")).font(.caption).foregroundStyle(.secondary)
            if !item.otherRoles.isEmpty {
                Text("登记用途 · " + item.otherRoles.joined(separator: "、"))
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(Array(item.history.enumerated()), id: \.offset) { _, action in
                actionNote(action).font(.caption).foregroundStyle(.secondary)
            }
            if !item.projects.isEmpty || !item.references.isEmpty {
                evidence(item)
            }
        }
    }

    private func evidence(_ item: TaskCloseoutItem) -> some View {
        DisclosureGroup("\(item.projects.count.formatted()) 个关联工程 · \(item.references.count.formatted()) 条已知引用") {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(item.projects, id: \.self) { project in
                    Text(displayPath(project)).textSelection(.enabled)
                }
                ForEach(Array(item.references.enumerated()), id: \.offset) { _, reference in
                    let source = displayPath(reference.string("source"))
                    let location = reference.string("location")
                    Text(location.isEmpty ? source : source + " · " + location)
                        .textSelection(.enabled)
                    if !reference.paths("projects").isEmpty {
                        Text(reference.paths("projects").map(displayPath).joined(separator: "、"))
                            .foregroundStyle(.secondary).textSelection(.enabled)
                    }
                }
            }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 5)
        }.font(.caption)
    }

    private func actionNote(_ item: Object) -> some View {
        let status = item.string("status")
        let detail = [fileStates[status] ?? "待核对", item.string("reason"),
                      displayPath(item.string("restored_to"))].filter { !$0.isEmpty }
        return Text(detail.joined(separator: " · ")).textSelection(.enabled)
    }
}
