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
    return apps.map { app in
        let owned = owners[app.string("path")] ?? []
        let exclusive = owned.filter {
            $0.raw.number("nlink") == 1 && $0.raw.paths("projects").count <= 1 && $0.raw.paths("shared_owners").isEmpty
        }
        let safe = exclusive.filter { $0.decision == "safe" }
        return RankedApp(app: app, size: exclusive.reduce(0) { $0 + $1.allocated },
                         safe: safe.reduce(0) { $0 + $1.allocated }, count: safe.count, scanID: scanID)
    }.sorted {
        let a = bySafe ? $0.safe : $0.size, b = bySafe ? $1.safe : $1.size
        return a == b ? $0.id < $1.id : a > b
    }
}

struct WorkspaceStats {
    var bytes: Int64 = 0
    var safeBytes: Int64 = 0
    var safe = 0
    var review = 0
    var reviewBytes: Int64 = 0

    mutating func add(_ other: WorkspaceStats) {
        bytes += other.bytes
        safeBytes += other.safeBytes
        safe += other.safe
        review += other.review
        reviewBytes += other.reviewBytes
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
    var title: String {
        if id.projects.isEmpty { return "工具数据 · " + id.agents.joined(separator: "、") }
        return id.projects.map { URL(fileURLWithPath: $0).lastPathComponent }.joined(separator: "、")
    }
    var group: AgentGroup {
        id.projects.count > 1 ? .shared : AgentGroup(agents: id.agents)
    }
}

struct AgentPartition: Identifiable {
    let id: AgentGroup
    let buckets: [ProjectBucket]
    let stats: WorkspaceStats
}

func workspacePartitions(projects: [String: [String]], files: [Entry], bySafe: Bool) -> [AgentPartition] {
    func bucketKey(_ roots: Set<String>, agents: Set<String>) -> ProjectBucket.Key {
        ProjectBucket.Key(projects: roots.sorted(), agents: agents.sorted())
    }
    var buckets: [ProjectBucket.Key: ProjectBucket] = [:]
    for (root, agents) in projects {
        let key = bucketKey([root], agents: Set(agents))
        buckets[key] = ProjectBucket(id: key)
    }
    // Combine project memberships across every path to an inode before assigning its bytes.
    for links in Dictionary(grouping: files, by: FileIdentity.init).values {
        guard let first = links.first else { continue }
        let roots = Set(links.flatMap { $0.raw.paths("projects") })
        let agents = roots.isEmpty ? Set(links.flatMap { $0.raw.paths("agents") }) : Set(roots.flatMap { projects[$0] ?? [] })
        guard !roots.isEmpty || !agents.isEmpty else { continue }
        let key = bucketKey(roots, agents: agents)
        let decisions = Set(links.map(\.decision))
        let safe = decisions == ["safe"]
        let review = decisions == ["review"]
        let amount = WorkspaceStats(bytes: first.allocated, safeBytes: safe ? first.allocated : 0,
                                    safe: safe ? 1 : 0, review: review ? 1 : 0, reviewBytes: review ? first.allocated : 0)
        buckets[key, default: ProjectBucket(id: key)].stats.add(amount)
        buckets[key, default: ProjectBucket(id: key)].paths.formUnion(links.map(\.path))
    }
    func value(_ stats: WorkspaceStats) -> Int64 { bySafe ? stats.safeBytes : stats.bytes }
    return Dictionary(grouping: buckets.values, by: \.group).map { group, rows in
        let sorted = rows.sorted {
            if value($0.stats) != value($1.stats) { return value($0.stats) > value($1.stats) }
            if $0.title != $1.title { return $0.title < $1.title }
            return $0.id.projects.lexicographicallyPrecedes($1.id.projects)
        }
        let total = rows.reduce(into: WorkspaceStats()) { $0.add($1.stats) }
        return AgentPartition(id: group, buckets: sorted, stats: total)
    }.sorted {
        value($0.stats) == value($1.stats) ? $0.id.title < $1.id.title : value($0.stats) > value($1.stats)
    }
}
