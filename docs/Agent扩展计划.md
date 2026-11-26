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
- **TRAE**。覆盖 macOS IDE 的本地工作区和 CLI 2.0 的 JSONL 会话，后续核对压缩和数据库会话以及 TraeWork。

#### 第二批开发分支覆盖

| 家族 | 已接入的本地证据 | 仍需验证的范围 |
| --- | --- | --- |
| Claude Desktop/Cowork | 应用标识符、桌面数据、Cowork 虚拟机和本地会话分开盘点 | 项目路径格式和会话内产物归属 |
| GitHub Copilot | CLI 首条 v1 会话事件。VS Code／Insiders 用扩展会话证据关联本地单目录工程 | 多根、远程、共享 SQLite 索引与同步历史 |
| Windsurf / Devin Desktop | 稳定版应用标识符、Windsurf 与 Devin 的稳定版及 Next 单目录、已保存多根工作区和编辑器已落盘的未命名工作区，盘点 IDE 状态及 Cascade 本地存储 | Cascade 会话与具体工程的归属、桌面程序实测 |
| Roo Code | 默认 VS Code 扩展清单、3.53.0 独立任务工作区元数据 | 旧版索引、自定义及其他编辑器配置 |
| Kiro IDE / KAS 共享 | IDE 应用标识符、默认或已授权的 `KIRO_HOME` 中的本地 KAS 工程路径、旧版 IDE 会话和日志目录 | 旧版 IDE 工程归属、客户端标记及远程会话 |
| Kiro CLI | 旧版成对 JSON 和 JSONL 的本地工程目录，以及 macOS 已写入 SQLite 主库的工程键 | CLI 安装状态、未写入主库的会话、真实客户端与云端会话 |
| TRAE IDE | 已核对的 macOS 应用标识，`Trae` 和 `Trae CN` 资料目录。本地单目录、已保存多根及落盘的未保存工作区 | 实际客户端生成的工作区、TraeWork 和远程工程 |
| TRAE CLI 2.0 | 默认或已授权目录中的活动、归档 JSONL 首行，核对会话 ID 和本机工作目录 | 压缩和数据库专有会话、1.x CoCo 历史、CLI 实际运行 |
| OpenClaw | macOS 应用标识符、严格 JSON 配置的本地 Agent 工程，分别盘点各 Agent 会话和调度存储 | JSON5 专用语法、配置包含和单条调度任务 |
| Qwen Code | v0.28.4 本地聊天记录的工程路径和受支持运行目录配置 | 新版托管会话元数据 |
| goose | v1.60.2 已写入 SQLite 主库的本机工作目录 | 尚未写入主库的会话与容器工作目录 |

Copilot CLI 与 VS Code 分行展示，工具总数按家族合并。以上是基于上游元数据和合成测试确认的格式。安装、工程关联和清理能力仍分别核验。[Copilot CLI 存储](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference) · [Windsurf 本地数据](https://docs.devin.ai/desktop/troubleshooting/windsurf-common-issues) · [Kiro 会话](https://kiro.dev/docs/ide/whats-new-v1/#sessions) · [OpenClaw Agent 配置](https://docs.openclaw.ai/concepts/multi-agent/index.html) · [Qwen Code 聊天存储](https://github.com/QwenLM/qwen-code/blob/v0.28.4/packages/core/src/services/chatRecordingService.ts) · [goose 会话结构](https://github.com/aaif-goose/goose/blob/v1.60.2/crates/goose/src/session/session_manager.rs)

从签名的 Devin Desktop 3.14.30833 稳定版安装镜像核对了沿用的 `com.exafunction.windsurf` 标识、迁移前后的数据目录名和 `workspace.json` 写入结构。既白从已记录且可读取的 `.code-workspace` 解析本地相对路径、绝对路径和文件 URI。工作区配置、全局状态和 Cascade 数据按保留盘点。 [官方更名与迁移记录](https://docs.devin.ai/desktop/changelog) · [多根工作区格式](https://code.visualstudio.com/docs/editing/workspaces/multi-root-workspaces#_workspace-file-schema) · [工单 #29](https://github.com/Moosphan/lumen/issues/29)

未命名工作区按桌面编辑器默认资料目录中的 `Workspaces/<时间戳>/workspace.json` 识别。在工作区索引建立前，也可读取已落盘的本地目录清单。工作区配置即使在手动扫描中也按保留处理。 [上游桌面写入逻辑](https://github.com/microsoft/vscode/blob/main/src/vs/platform/workspaces/electron-main/workspacesManagementMainService.ts)

Continue 通过 VS Code 扩展清单和 `sessions.json` 关联本地工作目录，验证对应会话文件存在。默认 `.continue` 下的配置、索引和会话列为保留；自定义数据位置沿用授权范围。冲突、失效和远程会话不建立本机项目归属。[Continue 会话格式](https://github.com/continuedev/continue/blob/5522c6f44ca0ac3528b37244818fbfa39b5af470/core/util/history.ts) · [工单 #23](https://github.com/Moosphan/lumen/issues/23)

Aider 接入后累计覆盖 16 个工具家族。默认在个人目录及已授权目录的有限层级查找历史文件开头的启动记录，使用目录位置关联工程。会话历史、配置和 RepoMap 缓存计入占用并保留。文件正文和提示词不进入索引，安装状态与自定义历史文件名仍待核验。[Aider 历史文件参数](https://github.com/Aider-AI/aider/blob/v0.86.2/aider/args.py) · [启动记录格式](https://github.com/Aider-AI/aider/blob/v0.86.2/aider/io.py) · [工单 #24](https://github.com/Moosphan/lumen/issues/24)

DeepSeek Harness 和 Pi 接入后累计覆盖 18 个工具家族。

| Agent | 已接入的本地证据 | 待验证范围 |
| --- | --- | --- |
| DeepSeek Harness | 官方签名桌面包 `0.2.2-rc.5` 的应用标识符、默认 `~/.dsh` 或授权的 `DSH_HOME`、工作区登记 v2、桌面应用数据与缓存分区、独立 CLI 会话 v0–v4 | 旧版工作区登记格式、自定义插件存储、完整桌面和 CLI 运行 |
| Pi | 默认和已授权的自定义配置及会话目录，JSONL v1–v3 首条会话记录的 `cwd`，正式 CLI `1.6.1` 生成的 v3 记录已验证 | 安装版本自动检测、临时 CLI 参数 |

DeepSeek Harness 的会话、配置、插件与凭证按保留盘点。Pi 只解析会话首条记录，历史和配置按保留盘点。两者沿用工程共享占用及清理审阅流程。[Harness 工作区格式](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/workspace/workspace/src/spec.ts) · [Harness 桌面数据路径](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/docs/user/guide/desktop-uninstall-cleanup.md) · [Pi 会话格式](https://github.com/earendil-works/pi/blob/bfafd8ed5168bca1f1ca88ef0529cfd326664d16/packages/coding-agent/docs/session-format.md)

隔离验证使用 DeepSeek JSON 存储组件 `0.0.1-rc.1` 和 Pi `1.6.1` 的会话写入组件创建真实格式的样本，确认同一工作目录可归属两者，索引和会话文件均保留。

独立 DeepSeek CLI 会话按最高物理世代核验 JSONL 首行或 Zstandard 首帧。校验会话标识和目录布局后使用会话头中的 `cwd`。未知版本、混合编码、重复会话标识只保留盘点。macOS 运行包自带首帧解码库。[会话存储格式](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/session/session-persistence-jsonl/README.md) · [工单 #28](https://github.com/Moosphan/lumen/issues/28)

接入 Conductor 后累计覆盖 19 个工具家族。默认目录中的工作树通过本地 `.git` 文件格式识别，可与 Codex 等共同归属同一工程。识别不跟随 Git 指针访问外部索引。应用会话数据和工作树的 `.context` 附件按保留盘点，Git 索引未核对的工作树文件留待核对。真实客户端、旧版嵌套工作区、自定义目录和云端工作区仍待验证。 [官方工作区布局](https://www.conductor.build/docs/concepts/git-worktrees) · [本地会话路径](https://www.conductor.build/docs/reference/privacy) · [工单 #30](https://github.com/Moosphan/lumen/issues/30)

接入 Amp 后累计覆盖 20 个工具家族。默认盘点和已授权目录识别 `.amp/settings.json` 或 `settings.jsonc` 中的 Amp 工程配置。旧版 CLI JSON 通过文件名对应的会话 ID 与 `env.initial.trees` 解析本地 URI。用户配置、凭证、旧会话、文件变动备份和识别到的 Amp 专用缓存按保留盘点。云端线程、新版线程执行位置和真实客户端运行仍待验证。 [官方配置](https://ampcode.com/docs/cli/settings) · [旧版 CLI 归档元数据](https://registry.npmjs.org/@sourcegraph%2famp/0.0.1768176116-ged0d88) · [工单 #31](https://github.com/Moosphan/lumen/issues/31)

Kiro CLI 与 IDE 合计一个工具家族，仍覆盖 20 个家族。新版共享 KAS 会话的项目归到 Kiro，旧版 CLI 的本地工程单列为 Kiro CLI。旧版 SQLite 仅查询受支持会话表的工程目录键。会话、配置和凭证按保留盘点，外部 `KIRO_HOME` 仍需目录授权。 [CLI 会话管理](https://kiro.dev/docs/cli/chat/session-management/) · [AWS 会话存储示例](https://github.com/aws-samples/sample-kiro-cli-multiagent-development/blob/main/docs/observability.md) · [工单 #32](https://github.com/Moosphan/lumen/issues/32)

接入 TRAE 后累计覆盖 21 个工具家族。macOS 的 `Trae` 与 `Trae CN` 默认资料目录归到同一个 TRAE 家族。项目关联来自编辑器工作区元数据，外部工作区清单需要目录授权。会话、原始快照、扩展和用户配置按保留盘点。 [TRAE 数据位置](https://docs.trae.cn/ide_troubleshoot-general-issues) · [工作区格式](https://code.visualstudio.com/docs/editing/workspaces/multi-root-workspaces#_workspace-file-schema) · [工单 #33](https://github.com/Moosphan/lumen/issues/33)

TRAE CLI 2.0 在工作区单列，工具家族与 IDE 合计。扫描默认 `~/.trae/cli` 以及已授权的当前自定义位置，从活动和归档 JSONL 的首行核验会话标识和工作目录。原始会话、提示词历史和已知认证及状态文件保留。压缩会话计入占用，工程归属待核验。 [官方 CLI 会话命令](https://docs.trae.cn/cli_command-line-parameters) · [官方发行脚本](https://trae.cn/trae-cli/install_v2.sh) · [工单 #34](https://github.com/Moosphan/lumen/issues/34)

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

本分支的模型页使用已盘点的本地文件。Ollama 显示清单和 tag，Hugging Face 显示修订及共享权重，LM Studio 区分已知量化或分片，ComfyUI 识别内置加载节点的静态引用。支持已授权的自定义目录，并可手动核验 Ollama 或 LM Studio 本机服务的加载状态。结果缺乏证据时显示未知，模型全部保留。任务收尾单显示生命周期和批次，OpenTimelineIO 的本地源素材与代理引用也参与保护。增量扫描复用未变化的 Agent 归属摘要，缓存仅持久化证据字段、工程路径和文件状态戳。

#### 第三批开发分支覆盖

| 工单 | 当前覆盖 | 后续验收 |
| --- | --- | --- |
| [#19 模型重新获取](https://github.com/Moosphan/lumen/issues/19) | 保存公开 Hugging Face 模型的文件级恢复凭据；固定修订、文件大小和 SHA-256 复核；支持下载中断与同名冲突 | 在实际工具中核验重新导入后的可见性；扩展 Ollama、LM Studio 和 ComfyUI 的可重建来源与整组权重验证 |
| [#20 本地 MCP](https://github.com/Moosphan/lumen/issues/20) | stdio 脱敏汇总、限时草案和状态查询；原生窗口展示实际路径并批准，复核通过后可恢复隔离 | 与实际 Agent 客户端做配置和协议联调，核对加载、刷新、过期及批次查询 |
| [#21 通用清理](https://github.com/Moosphan/lumen/issues/21) | 七类开发缓存盘点；按规则审阅 npm、旧版 pip、Homebrew 与 Xcode 的部分文件；按已索引的媒体类型、已知跨工程引用和内容重复项审阅；读取 Docker 内置构建缓存独立用量 | 验证 Docker Desktop 的实际版本组合、原生工具清理与恢复，扩充应用低影响规则和残留归属 |

pnpm 内容存储、pip `http-v2`、uv 缓存与 Docker 虚拟磁盘只做盘点。Docker 行显示整个本地虚拟磁盘的已分配空间，手动查询可查看内置构建器报告的缓存用量。媒体内容重复依据深度扫描的哈希结果，跨工程引用依赖已识别的格式。[pip 缓存布局](https://pip.pypa.io/en/stable/topics/caching/) · [uv 缓存链接模式](https://docs.astral.sh/uv/reference/settings/#link-mode) · [Docker Desktop 数据与恢复](https://docs.docker.com/desktop/settings-and-maintenance/backup-and-restore/)

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
