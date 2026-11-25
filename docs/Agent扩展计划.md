# 既白 Lumen · Agent 识别与清理扩展计划

## 竞品对标

截至 2026-11-25，Latent 官网宣称覆盖 120 多种开发与 AI 工具。工具页和更新页通过 JavaScript 加载数据。本次读取官方目录，数得 174 个条目，含 Claude Code、Codex、Cursor、Gemini CLI 和 Ollama；这一数字包含开发工具、应用及 CLI 等不同实体。1.0.38 更新记录新增 Amp、Kiro CLI，并称使用 Git 状态评估 Conductor 工作区。[官网](https://latentmac.com/) · [工具目录数据](https://latentmac.com/tools.json) · [版本记录](https://latentmac.com/changelog.json)

据帮助文档，Latent 能区分同一工具下的模型、缓存和用户作品；对部分模型会在删除前核对注册表摘要、修订或文件名。文件通常经废纸篓和清单原位恢复；Docker 镜像及模拟器运行时由原工具移除，记录重新获取命令。MCP 可读取扫描摘要、拟定限时清单，并在用户确认后执行获准项目。[帮助文档](https://latentmac.com/help)

本次核验限于官方公开资料，尚未安装实测。既白将用同一批工程对比目录发现、项目引用、风险判断和恢复后复跑。目录覆盖与清理准确率分别衡量。

## 产品目标与现状

让用户在清理前看见文件来自哪次任务、仍被哪些工程引用、实际可释放多少；在清理后能从记录里恢复文件，并核对原工程是否还能打开或构建。工作区按 Agent 和工程两种视角浏览，工程视角汇总同一项目里的多个 Agent，Agent 视角保留各自的会话和数据归属。

当前 `v0.1.0-dev.6` 已从 Codex 和 Claude Code 会话识别工程，保护 Git 跟踪文件、已知跨工程素材引用和任务输入输出。用户已能手动收尾，或选用 Claude Code SessionEnd 隔离已登记的临时产物。现有大文件浏览和应用清理详情继续作为通用入口。[当前功能](../README.md) · [技术方案](技术方案.md) · [原生与性能验证](验证报告.md)

## Agent 适配顺序

### P0 · 6 种 Agent，深化 2 种、新增 4 种

| Agent | 发现与关联 | 首版盘点 |
| --- | --- | --- |
| Codex | 分别验证 CLI 和桌面版。从本机会话工作目录和已支持的任务元数据关联工程 | 工程、会话与工具数据、已登记的临时产物 |
| Claude Code | 识别项目会话、受支持的工作树和自定义数据目录 | 工程、会话数据、工作树；沿用已登记产物的可选收尾 |
| Cursor | 从安装信息、受支持版本的工作区元数据关联本地路径 | 工作区、本地存储、可归属的日志和缓存 |
| Gemini CLI | 从本地配置和受支持版本的项目、会话元数据发现实际工程 | 会话存储、工具缓存、工程产物 |
| OpenCode | 读取受支持版本的项目／会话元数据，区分主工程与工作树 | 会话存储、日志、工程产物 |
| Cline | 首先验证 VS Code 扩展的数据和工作区关系，记录每次匹配的依据 | 扩展存储、任务历史、可归属的缓存 |

以上是拟交付范围。适配器按已验证版本声明能力，界面分别标明「已安装」「已关联工程」「已识别数据」「支持任务事件」「有已验证清理规则」。安装目录、会话目录和工程目录分别显示，存在多个候选或权限限制时提供原因与 Finder 定位。默认扫描读取必要的本地元数据，自定义目录可单独授权。项目归属不明时保留「待关联」状态。[Codex App Server](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md) · [Claude Code Hooks](https://code.claude.com/docs/en/hooks#sessionend) · [Cursor Hooks](https://cursor.com/docs/hooks) · [Gemini CLI Hooks](https://geminicli.com/docs/hooks/reference/) · [OpenCode 插件](https://opencode.ai/docs/plugins/) · [Cline Hooks](https://docs.cline.bot/customization/hooks)

#### 首批开发分支覆盖

| Agent | 支持的本地证据 | 待验证范围 |
| --- | --- | --- |
| Codex | 会话的工作目录、桌面默认工作区布局、应用标识符、进程环境指明的 `CODEX_HOME` | CLI 安装核验，更多原生工作树布局 |
| Claude Code | 会话的工作目录、默认位置和 `CLAUDE_CONFIG_DIR`，已登记任务的可选 SessionEnd | 独立安装版本核验，扩展原生工作树布局 |
| Cursor | 官方稳定版与 Nightly 应用标识符、本地单目录工作区 URI、已知 IDE 数据与日志目录 | 多根工作区、远程工程 |
| Gemini CLI | 项目注册表、项目根目录标记、`GEMINI_CLI_HOME` | CLI 安装版本，任务结束事件 |
| OpenCode | 旧版项目 JSON、已写入 SQLite 主库的项目与工作树，`XDG_DATA_HOME` 和 `XDG_CACHE_HOME` | 尚在 WAL 中的会话、CLI 安装版本 |
| Cline | VS Code 和 Insiders 扩展安装清单、历史任务的初始工作目录，已知共享任务目录 | 缺少工作目录字段的历史，以及其他编辑器变体 |

官方元数据结构由合成夹具覆盖。安装、关联和清理能力分别显示。首批新发现的工具存储按保留处理，工程内容沿用原有引用和清理复核规则。Codex、Claude Code 会话路径缓存按解析器、文件身份和读取范围分开，目录树查询复用已有快照。[Cursor 安装标识](https://prod.cursor.com/docs/enterprise/deployment-patterns) · [Gemini CLI 项目注册表](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/config/projectRegistry.ts) · [OpenCode 项目结构](https://github.com/anomalyco/opencode/blob/v1.2.15/packages/opencode/src/project/project.sql.ts) · [Cline 任务结构](https://github.com/cline/cline/blob/v3.45.1/src/shared/HistoryItem.ts)

### P1 · 再增 8 种，累计目标 14 种

- **Claude Desktop/Cowork、GitHub Copilot CLI/VS Code、Windsurf、Roo Code**。按产品和版本核对配置、历史、缓存与实际工作区，区分本地运行、远程工作区和同步数据。Claude Desktop/Cowork 单列运行环境和会话数据，Copilot 分测 CLI 与编辑器集成。[GitHub Copilot Hooks](https://docs.github.com/en/copilot/concepts/customizations/hooks)
- **OpenClaw、Kiro、Qwen Code、goose**。优先识别项目、会话存储和工具数据。OpenClaw 按 Agent 和会话隔离归属，用户调度的后台任务与本次任务单独标记。[OpenClaw 会话文档](https://docs.openclaw.ai/concepts/session)
- **候补队列**。Trae、Amp、Kiro CLI、Conductor、Aider，按用户需求和元数据可靠性排序。Kiro、Kiro CLI 如共享产品家族，在覆盖统计中合并，在版本矩阵中单列。

## 功能和验收清单

| 编号 | 交付 | 依赖 | 验收 |
| --- | --- | --- | --- |
| P0-1 | 建立统一 Agent 适配接口和版本矩阵 | 现有扫描引擎 | 识别安装、工程、存储位置与证据。测试默认位置、自定义位置、多版本、损坏数据和权限缺口。 |
| P0-2 | 交付首批 6 种适配 | P0-1 | 每种在隔离样本中识别已知工程、数据目录和占用。CLI、桌面版或扩展分别记录实际支持范围，未知版本可降级盘点。 |
| P0-3 | 升级工作区和清理详情 | P0-2 | 从「Agent → 工程 → 数据类别」和「工程 → Agent」均可进入详情。展示总占用、独占、共享、候选数量及大小，支持 Finder 定位、快速预览和扫把图标入口。 |
| P0-4 | 增量识别和清理证据 | P0-1 | 只重读变动的元数据，缓存工作区和大小聚合。同一文件身份去重，APFS 克隆等空间估算限制可见；受保护或无法核验的资源无法进入低影响批次。 |
| P1-1 | 支持第二批 8 种适配 | P0-2 | 每种提交版本、路径来源、权限缺口和功能能力。Agent、IDE 扩展与工作区管理器分别统计。 |
| P1-2 | 任务收尾单与共享素材影响分析 | P0-3、P0-4 | 任务完成后生成清单，区分原料、交付物、临时渲染和缓存。复用 Git、HTML、Vite、FCPXML 引用保护，扩展到可解析的媒体工程与模型工作流。多工程共享配乐、代理素材和模型时，展示全体已知引用。 |
| P1-3 | 扩展任务事件和恢复核对 | P1-2 | 优先验证 Codex、Claude Code 和 Gemini CLI。会话结束、模型回复结束和用户确认交付分别处理；弱信号只生成草案。自选规则只隔离已确认范围内的临时产物。重复或乱序事件、后台仍写文件和任务中断均有回归样本。 |
| P1-4 | 模型级盘点 | P0-4 | 覆盖 Ollama、Hugging Face、LM Studio、ComfyUI。按模型、精确版本和量化文件展示独占空间、共享依赖、已知工作流引用、加载状态及状态来源。支持本地配置的自定义模型目录。 |
| P2-1 | 可验证的清理与重新获取 | P1-3、P1-4 | 清理前核对原始来源、版本或摘要并提示重建成本；恢复后核对文件身份与工具可见性。离线、来源下架、私有权重、自训练模型、未识别引用及下载中断均有用例。 |
| P2-2 | 本地 MCP | P0-4、P2-1 | Agent 能查询汇总、生成清理草案、取回已批准批次状态。要达到执行能力对标，须验证原生界面批准、绑定到期的逐项清单、越权和重复执行防护。默认摘要使用别名或相对路径，文件名和路径可按授权范围披露。 |
| P2-3 | 补齐通用清理 | P0-4、P2-1 | 开发缓存优先覆盖 Xcode、npm/pnpm、pip/uv、Homebrew 和 Docker 构建缓存。支持受验证的原生清理与恢复方式。大文件增加媒体组及工程引用提示；应用详情继续扩展低影响规则和卸载残留归属。 |

模型盘点优先使用本地元数据和已经运行的工具提供的查询接口。Hugging Face 以共享 blob 的最终引用确定可释放量，Ollama 和 LM Studio 将工具返回的逻辑模型与磁盘文件对应，ComfyUI 按内置加载节点和已知路径配置建立引用。未知节点和路径解析缺口在详情中可见。隔离空间与实际释放空间分开显示。[Ollama 模型列表](https://docs.ollama.com/api/tags) · [Hugging Face 缓存](https://huggingface.co/docs/huggingface_hub/guides/manage-cache) · [LM Studio 模型列表](https://lmstudio.ai/docs/developer/rest/list) · [ComfyUI 模型路径](https://docs.comfy.org/basic-concepts/models)

通用功能对照表持续追踪模拟器运行时、相似照片视频、厂商原生包管理、可恢复卸载和内存压力／交换空间展示。先在磁盘清理范围内逐项验收，内存指标作为后续独立功能评估。[Latent 功能概览](https://latentmac.com/) · [Latent 帮助](https://latentmac.com/help)

## 怎样证明做到更好

在同一台 Apple Silicon Mac、隔离用户或一次性运行环境中，用同一批工具版本和数据比较既白、Latent 与既白 `v0.1.0-dev.6`。每个场景都记录权限、覆盖缺口、耗时、回收字节及恢复后的结果。以下数字为验收目标。

| 场景 | 通过条件 |
| --- | --- |
| 工具与目录发现 | 版本矩阵内的默认、自定义和隐藏目录按预期识别。分别记录已装、已关联和已验证清理的工具数；比较双方在相同数据上的命中与误报。 |
| 清理安全与恢复 | 夹具覆盖源文件、共享模型、用户交付件、打开的文件及权限边界。受保护项进入执行批次的数量为 0。已声明可恢复的样本恢复后校验一致；选定样本成功重新打开或构建。 |
| 清理成本和空间 | 对同一批文件逐项核对共享空间、实际恢复方式、是否依赖联网、是否需重新下载。向用户明确显示批准前的预计空间及清理后的实际变化。 |
| 响应与扫描 | 在至少 10 万文件的夹具中，重复测量缓存目录切换、冷启动和深度盘点。缓存目录切换目标 P95 ≤ 200 ms；在同等扫描范围下，首次快速盘点中位数比现版慢不超过 10%。首次发现和深度扫描分开计时。 |
| 既白差异化 | 用「两个 Agent 共用工程和素材」及「视频工作流完成后收尾」两个场景比较。既白要给出可核验的引用链、明确的保留理由与可复验的恢复结果，再比较用户完成同一操作所需步骤。 |

对标通过的门槛是首批共同覆盖场景的发现准确性、保护与恢复结果达到 Latent 实测水平，共享引用或任务收尾至少两项指标有可复测的改善，例如识别出更多真实引用、保留更多有用素材或减少完成同一操作的步骤。工具分布、模型、MCP、通用清理分别记录已达到、部分达到和待补齐的范围。

首批建议按 **6 种 Agent、工程自动归属、空间去重和可审阅清理**验收。第二批再增加适配和任务收尾，模型恢复与 MCP 执行能力在各自的证据和恢复用例通过后验收。
