import Foundation

struct RankedApp: Identifiable {
    let app: Object
    let size: Int64
    let safe: Int64
    let count: Int
    let scanID: String
    var id: String { app.string("path") }
}

struct FileIdentity: Hashable {
    let device: Int64
    let inode: Int64
    init(_ entry: Entry) {
        device = entry.raw.number("dev")
        inode = entry.raw.number("ino")
    }
}

func rankedApps(_ apps: [Object], files: [Entry], scanID: String, bySafe: Bool) -> [RankedApp] {
    let owners = Dictionary(grouping: files.filter { !$0.raw.string("app_path").isEmpty }) { $0.raw.string("app_path") }
    let ranked = apps.map { app in
        let owned = owners[app.string("path")] ?? []
        let exclusive = owned.filter {
            $0.raw.number("nlink") == 1 && $0.raw.paths("projects").count <= 1 && $0.raw.paths("shared_owners").isEmpty
        }
        let safe = exclusive.filter { $0.decision == "safe" }
        return RankedApp(app: app, size: app.number("inventory_bytes") + exclusive.reduce(0) { $0 + $1.allocated },
                         safe: safe.reduce(0) { $0 + $1.allocated }, count: safe.count, scanID: scanID)
    }
    return sortedApps(ranked, bySafe: bySafe)
}

func sortedApps(_ ranked: [RankedApp], bySafe: Bool) -> [RankedApp] {
    ranked.sorted {
        let a = bySafe ? $0.safe : $0.size, b = bySafe ? $1.safe : $1.size
        return a == b ? $0.id < $1.id : a > b
    }
}

struct WorkspaceStats {
    var bytes: Int64 = 0
    var sharedBytes: Int64 = 0
    var count = 0
    var safeBytes: Int64 = 0
    var safe = 0
    var review = 0
    var reviewBytes: Int64 = 0

    mutating func add(_ other: WorkspaceStats) {
        bytes += other.bytes
        sharedBytes += other.sharedBytes
        count += other.count
        safeBytes += other.safeBytes
        safe += other.safe
        review += other.review
        reviewBytes += other.reviewBytes
    }
}

struct WorkspaceFiles {
    var stats = WorkspaceStats()
    var paths = Set<String>()

    mutating func add(_ other: WorkspaceFiles) {
        stats.add(other.stats)
        if paths.isEmpty { paths = other.paths }
        else { paths.formUnion(other.paths) }
    }

    mutating func record(_ amount: WorkspaceStats, paths newPaths: [String]) {
        stats.add(amount)
        paths.formUnion(newPaths)
    }
}

enum AgentGroup: Hashable {
    case single(String), joint, shared, unassigned
    var title: String {
        switch self {
        case .single(let agent): return agent
        case .joint: return "多 Agent 共同工作区"
        case .shared: return "跨项目共享文件"
        case .unassigned: return "未关联 Agent"
        }
    }
    init(agents: [String]) {
        if agents.count == 1 { self = .single(agents[0]) }
        else { self = agents.isEmpty ? .unassigned : .joint }
    }
}

struct ProjectBucket: Identifiable {
    struct Key: Hashable {
        let projects: [String]
        let agents: [String]
    }
    let id: Key
    var stats = WorkspaceStats()
    var paths = Set<String>()
    var categoryFiles: [String: WorkspaceFiles] = [:]
    var detailTitle = ""
    var unscopedToolFiles: WorkspaceFiles?
    var title: String {
        if !detailTitle.isEmpty { return detailTitle }
        if id.projects.isEmpty { return "工具数据 · " + id.agents.joined(separator: "、") }
        return id.projects.map { URL(fileURLWithPath: $0).lastPathComponent }.joined(separator: "、")
    }
    var group: AgentGroup {
        id.projects.count > 1 ? .shared : AgentGroup(agents: id.agents)
    }

    mutating func record(_ amount: WorkspaceStats, paths newPaths: [String], kind: String?) {
        stats.add(amount)
        paths.formUnion(newPaths)
        if let kind { categoryFiles[kind, default: WorkspaceFiles()].record(amount, paths: newPaths) }
    }

    mutating func recordUnscoped(_ amount: WorkspaceStats, paths newPaths: [String]) {
        if unscopedToolFiles == nil { unscopedToolFiles = WorkspaceFiles() }
        unscopedToolFiles?.record(amount, paths: newPaths)
    }
}

struct AgentPartition: Identifiable {
    let id: AgentGroup
    let buckets: [ProjectBucket]
    let stats: WorkspaceStats
}

func workspacePartitions(projects: [String: [String]], files: [Entry], bySafe: Bool,
                         storeRoots: Set<String> = []) -> [AgentPartition] {
    func bucketKey(_ roots: Set<String>, agents: Set<String>) -> ProjectBucket.Key {
        ProjectBucket.Key(projects: roots.sorted(), agents: agents.sorted())
    }
    var buckets: [ProjectBucket.Key: ProjectBucket] = [:]
    var knownKeys: [String: ProjectBucket.Key] = [:]
    for (root, agents) in projects {
        let key = bucketKey([root], agents: Set(agents))
        knownKeys[root] = key
        buckets[key] = ProjectBucket(id: key)
    }
    // Combine project memberships across every path to an inode before assigning its bytes.
    for links in Dictionary(grouping: files, by: FileIdentity.init).values {
        guard let first = links.first else { continue }
        let key: ProjectBucket.Key
        let projectPaths = first.raw.paths("projects")
        let rowAgents = first.raw.paths("agents")
        if links.count == 1 && projectPaths.count == 1, let known = knownKeys[projectPaths[0]],
           rowAgents.isEmpty || rowAgents == known.agents {
            key = known
        } else {
            let roots = Set(links.flatMap { $0.raw.paths("projects") })
            let fileAgents = Set(links.flatMap { $0.raw.paths("agents") })
            let owners: [String] = roots.flatMap { projects[$0] ?? [] }
            let agents = fileAgents.union(owners)
            guard !roots.isEmpty || !agents.isEmpty else { continue }
            key = bucketKey(roots, agents: agents)
        }
        let decision = first.decision
        let consistent = links.dropFirst().allSatisfy { $0.decision == decision }
        let safe = consistent && decision == "safe"
        let review = consistent && decision == "review"
        let shared = key.projects.count > 1 || links.count > 1 || first.raw.number("nlink") > 1
            || first.raw["shared_container"] as? Bool == true || !first.raw.paths("shared_owners").isEmpty
            || first.raw.paths("model_versions").count > 1
        let amount = WorkspaceStats(bytes: first.allocated, sharedBytes: shared ? first.allocated : 0,
                                    count: 1, safeBytes: safe ? first.allocated : 0,
                                    safe: safe ? 1 : 0, review: review ? 1 : 0, reviewBytes: review ? first.allocated : 0)
        let kind = key.projects.isEmpty ? nil : first.raw.string("kind")
        buckets[key, default: ProjectBucket(id: key)].record(amount, paths: links.map(\.path), kind: kind)
        if key.projects.isEmpty && !storeRoots.isEmpty {
            let unscoped = links.filter { !storeRoots.contains($0.raw.string("agent_store_path")) }
            // Keep unscoped aliases visible without counting the inode again outside known stores.
            let residual = unscoped.count == links.count ? amount : WorkspaceStats()
            if !unscoped.isEmpty {
                buckets[key, default: ProjectBucket(id: key)].recordUnscoped(residual, paths: unscoped.map(\.path))
            }
        }
    }
    let partitions = Dictionary(grouping: buckets.values, by: \.group).map { group, rows in
        AgentPartition(id: group, buckets: rows, stats: rows.reduce(into: WorkspaceStats()) { $0.add($1.stats) })
    }
    return sortedPartitions(partitions, bySafe: bySafe)
}

struct WorkspaceCategory: Identifiable {
    let kind: String
    let detail: ProjectBucket
    var id: String { kind }

    static func title(_ kind: String) -> String {
        switch kind {
        case "video": return "视频"
        case "image": return "图片"
        case "audio": return "音频"
        case "html": return "网页"
        case "model": return "模型"
        case "cache": return "缓存"
        case "build": return "构建产物"
        case "log": return "日志"
        case "symlink": return "符号链接"
        case "other", "": return "其他文件"
        default: return kind
        }
    }
}

struct WorkspaceProject: Identifiable {
    let id: String
    let agents: [String]
    let detail: ProjectBucket
    let related: [String]
    let categories: [WorkspaceCategory]
}

struct WorkspaceAgentStore: Identifiable {
    let raw: Object
    let detail: ProjectBucket
    var id: String { raw.string("path") }
}

struct WorkspaceAgent: Identifiable {
    let id: String
    let raw: Object
    let projects: [String]
    let stores: [WorkspaceAgentStore]
    let toolBuckets: [ProjectBucket]
    let stats: WorkspaceStats
    var installed: Bool { raw["installed"] as? Bool == true }
}

private struct WorkspaceListing {
    let agents: [WorkspaceAgent]
    let projects: [String: WorkspaceProject]
    let stats: WorkspaceStats

    init(partitions: [AgentPartition], files: [Entry], registered: [String: [String]], reported: [Object],
         storeRoots: Set<String>) {
        var bucketsByRoot: [String: [ProjectBucket]] = [:]
        var projectsByAgent: [String: Set<String>] = [:]
        var statsByAgent: [String: WorkspaceStats] = [:]
        var toolsByAgent: [String: [ProjectBucket]] = [:]
        var totals = WorkspaceStats()

        for bucket in partitions.flatMap(\.buckets) {
            if !bucket.id.projects.isEmpty { totals.add(bucket.stats) }
            for root in bucket.id.projects { bucketsByRoot[root, default: []].append(bucket) }
            for name in bucket.id.agents where !name.isEmpty {
                var stats = bucket.stats
                if bucket.id.agents.count > 1 { stats.sharedBytes = stats.bytes }
                statsByAgent[name, default: WorkspaceStats()].add(stats)
                if bucket.id.projects.isEmpty {
                    if let files = bucket.unscopedToolFiles {
                        let detail = ProjectBucket(id: bucket.id, stats: files.stats, paths: files.paths)
                        toolsByAgent[name, default: []].append(detail)
                    } else if storeRoots.isEmpty {
                        toolsByAgent[name, default: []].append(bucket)
                    }
                }
            }
        }

        var projectRows: [String: WorkspaceProject] = [:]
        for (root, buckets) in bucketsByRoot {
            let names = Array(Set(registered[root] ?? [])).filter { !$0.isEmpty }.sorted()
            let key = ProjectBucket.Key(projects: [root], agents: names)
            var related = Set<String>()
            var categories: [String: WorkspaceFiles] = [:]
            var indexed = WorkspaceFiles()
            for bucket in buckets {
                indexed.add(WorkspaceFiles(stats: bucket.stats, paths: bucket.paths))
                related.formUnion(bucket.id.projects)
                for (kind, files) in bucket.categoryFiles { categories[kind, default: WorkspaceFiles()].add(files) }
            }
            related.remove(root)
            let detail = ProjectBucket(id: key, stats: indexed.stats, paths: indexed.paths)
            var kinds: [WorkspaceCategory] = []
            let projectTitle = detail.title
            for (kind, files) in categories {
                let title = projectTitle + " · " + WorkspaceCategory.title(kind)
                let selection = ProjectBucket(id: key, stats: files.stats, paths: files.paths, detailTitle: title)
                kinds.append(WorkspaceCategory(kind: kind, detail: selection))
            }
            kinds.sort { lhs, rhs in
                let a = lhs.detail.stats.bytes, b = rhs.detail.stats.bytes
                return a == b ? lhs.kind < rhs.kind : a > b
            }
            projectRows[root] = WorkspaceProject(id: root, agents: names, detail: detail, related: related.sorted(), categories: kinds)
            for name in names { projectsByAgent[name, default: []].insert(root) }
        }

        var evidenceByAgent: [String: Object] = [:]
        for row in reported where !row.string("name").isEmpty { evidenceByAgent[row.string("name")] = row }
        var pathsByStore: [String: Set<String>] = [:]
        if !storeRoots.isEmpty {
            for file in files {
                let path = file.raw.string("agent_store_path")
                if storeRoots.contains(path) { pathsByStore[path, default: []].insert(file.path) }
            }
        }

        let names = Set(evidenceByAgent.keys).union(projectsByAgent.keys).union(statsByAgent.keys)
        var agentRows: [WorkspaceAgent] = []
        for name in names.sorted() {
            let raw = evidenceByAgent[name] ?? [:]
            let roots = (projectsByAgent[name] ?? []).union(raw.paths("projects")).sorted()
            let key = ProjectBucket.Key(projects: [], agents: [name])
            var seen = Set<String>()
            var stores: [WorkspaceAgentStore] = []
            for row in raw.rows("stores") {
                let path = row.string("path")
                guard storeRoots.contains(path), seen.insert(path).inserted else { continue }
                let label = row.string("title")
                let title = name + " · " + (label.isEmpty ? URL(fileURLWithPath: path).lastPathComponent : label)
                let selection = ProjectBucket(id: key, paths: pathsByStore[path] ?? [], detailTitle: title)
                stores.append(WorkspaceAgentStore(raw: row, detail: selection))
            }
            var stats = statsByAgent[name] ?? WorkspaceStats()
            stats.bytes = (raw["bytes"] as? NSNumber)?.int64Value ?? stats.bytes
            stats.sharedBytes = (raw["shared_bytes"] as? NSNumber)?.int64Value ?? stats.sharedBytes
            stats.count = (raw["count"] as? NSNumber)?.intValue ?? stats.count
            let agent = WorkspaceAgent(id: name, raw: raw, projects: roots, stores: stores,
                                       toolBuckets: toolsByAgent[name] ?? [], stats: stats)
            agentRows.append(agent)
        }
        agents = agentRows
        projects = projectRows
        stats = totals
    }
}

func sortedPartitions(_ partitions: [AgentPartition], bySafe: Bool) -> [AgentPartition] {
    func value(_ stats: WorkspaceStats) -> Int64 { bySafe ? stats.safeBytes : stats.bytes }
    return partitions.map { partition in
        let sorted = partition.buckets.sorted {
            if value($0.stats) != value($1.stats) { return value($0.stats) > value($1.stats) }
            if $0.title != $1.title { return $0.title < $1.title }
            return $0.id.projects.lexicographicallyPrecedes($1.id.projects)
        }
        return AgentPartition(id: partition.id, buckets: sorted, stats: partition.stats)
    }.sorted {
        value($0.stats) == value($1.stats) ? $0.id.title < $1.id.title : value($0.stats) > value($1.stats)
    }
}

struct InventoryPresentation {
    let files: [Entry]
    let apps: [RankedApp]
    let workspaceAgents: [WorkspaceAgent]
    let workspaceProjects: [String: WorkspaceProject]
    let workspaceStats: WorkspaceStats
    let safeCount: Int
    let reviewCount: Int
    let kinds: [String]
    let appFiles: [String: [Entry]]

    init(_ dashboard: Object = [:]) {
        let entries = dashboard.rows("files").map(Entry.init)
        files = entries
        appFiles = Dictionary(grouping: entries.filter { !$0.raw.string("app_path").isEmpty }) { $0.raw.string("app_path") }
        apps = rankedApps(dashboard.rows("apps"), files: entries, scanID: dashboard.string("scan_id"), bySafe: false)
        let reported = dashboard.rows("agent_inventory")
        var registered = dashboard["projects"] as? [String: [String]] ?? [:]
        for agent in reported {
            let name = agent.string("name")
            guard !name.isEmpty else { continue }
            for root in agent.paths("projects") where root.hasPrefix("/") && registered[root]?.contains(name) != true {
                registered[root, default: []].append(name)
            }
        }
        let locations: [Object] = reported.flatMap { $0.rows("stores") }
        let storeRoots = Set(locations.map { $0.string("path") }.filter { $0.hasPrefix("/") })
        let partitions = workspacePartitions(projects: registered, files: entries, bySafe: false, storeRoots: storeRoots)
        let listing = WorkspaceListing(partitions: partitions, files: entries, registered: registered, reported: reported,
                                       storeRoots: storeRoots)
        workspaceAgents = listing.agents
        workspaceProjects = listing.projects
        workspaceStats = listing.stats
        var safe = 0
        var review = 0
        var types = Set<String>()
        for entry in entries {
            if entry.decision == "safe" { safe += 1 }
            if entry.decision == "review" { review += 1 }
            types.insert(entry.raw.string("kind"))
        }
        safeCount = safe
        reviewCount = review
        kinds = types.filter { !$0.isEmpty }.sorted()
    }
}
