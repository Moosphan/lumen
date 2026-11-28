# 既白 Lumen

**Agent 完工，既白收尾。**

*Agents build. Lumen tidies up.*

面向 macOS 的本地磁盘清理器与 Agent 工作区管理工具。既白把应用占用、项目归属、共享素材和清理依据放在一起，帮你判断清掉一个文件会影响什么。[下载 Apple Silicon 开发预览版](https://github.com/Moosphan/lumen/releases/tag/v0.1.0-dev.6)

![既白原生 Mac 应用的空间总览，展示隔离演练的真实扫描结果](设计稿/Lumen-overview.png)

[应用清理详情实机截图](设计稿/Lumen-app-cleanup.png) · [大文件清理实机截图](设计稿/Lumen-large-files.png)

## 安全体验

需要 macOS 13+、Swift 5.10+ 的 Xcode 工具链和可用的 `/usr/bin/python3`（3.9+）。引擎使用标准库，无需 pip、API 密钥或账号。

```bash
git clone https://github.com/Moosphan/lumen.git
cd lumen
/usr/bin/python3 scripts/package.py
open dist/Lumen.app
```

每轮本地开发包可以保存在 `dist/<提交>/`，交付同一构建的 `Lumen.app` 和 `Lumen.zip`。

```bash
lumen_build_id=$(git rev-parse --short HEAD)
/usr/bin/python3 -B scripts/package.py --output "dist/$lumen_build_id/Lumen.app"
/usr/bin/codesign --verify --strict "dist/$lumen_build_id/Lumen.app"
/usr/bin/ditto -c -k --keepParent "dist/$lumen_build_id/Lumen.app" "dist/$lumen_build_id/Lumen.zip"
```

既白在启动盘点前核对扫描进程能否读取受保护的位置。确认权限后，自动盘点应用、下载、缓存、日志和本地 Agent 工程。权限暂无法确认时仍可查看应用本体占用。下载解压后，用 Finder 把 `Lumen.app` 移到固定位置，例如「应用程序」。打开后在「隐私与安全性 → 完全磁盘访问权限」为该位置的既白开启权限，再按系统提示重新打开。权限面板显示当前运行包的位置。升级时先退出旧版，再在原位置替换应用。[Apple 的隐私与安全性设置说明](https://support.apple.com/guide/mac-help/change-privacy-security-settings-on-mac-mchl211c911f/mac)

GitHub 上现有的旧开发预览包采用临时签名，更换这些包可能需要重新授权。采用同一开发团队的 Developer ID 签名发布后，macOS 可用稳定的代码身份识别后续版本。首次切换到该签名版本时，需要为它授权一次。[Apple 的代码签名说明](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements)

首页圆环显示启动盘已用空间、各类文件的占用和未归类差额。点击「未归类占用」可按目录或类别查看补查出的文件分配空间，并在 Finder 中打开目录。快照、其他 APFS 卷和无法读取的位置可能形成剩余差额。应用排行支持在 Finder 中显示应用及打开所在目录。可在「扫描范围」排除自动发现的位置，或添加额外目录。清理需要审阅和确认，自动收尾可单独启用。

也可以点击侧栏底部的 **创建演练样本**，体验两个 Agent 共用一个视频工程、被两个工程引用的配乐及可处理的日志。演练使用独立的设置、隔离区和废纸篓。

## 可以做什么

| 入口 | 功能 |
| --- | --- |
| 空间总览与应用排行 | 快速/深度盘点、按应用独占量或可低影响清理量排序、核对可处理项 |
| 应用清理详情 | 按应用查看低影响、待确认和保留项目，分析沙盒缓存、临时文件与 Chrome / Chromium 的生成代码缓存 |
| Agent 工作区 | 28 个工具家族的本地工程或数据盘点，包括 OpenHands Agent Canvas 与 CLI、Oh My Pi、Mistral Vibe、Hermes Agent、Zed、Crush、Kilo Code、TRAE IDE、CLI 与 TraeWork、Kiro IDE 与 CLI、Amp、Conductor、DeepSeek Harness、Pi、Aider 和 Continue。按 Agent 或工程查看共享占用、清理建议、任务收尾单和会话观察 |
| 模型存储 | Ollama、Hugging Face、LM Studio、ComfyUI 的模型、版本、共享权重和已知工作流引用；支持按固定修订重新获取经验证的 Hugging Face 单文件 |
| 大文件清理 | 多列卡片展示当前层占用，目录下钻复用快照；按视频、图片、音频审阅已索引的素材、引用和重复副本 |
| 清理建议 | 筛选大文件、旧下载、重复件、卸载残留；查看 Xcode、npm、pnpm、pip、uv、Homebrew 和 Docker 的开发缓存占用与处置依据 |
| 本地 MCP | Agent 可读取脱敏摘要、提交限时草案和查询状态。用户在「扫描范围」审阅实际文件并批准隔离 |
| 文件与应用处置 | 低影响直接清理、可恢复隔离、同卷废纸篓、受支持应用的卸载；逐项预演和执行前复核 |
| 清理记录 | 查看成功与跳过的路径、恢复批次、同名旁置恢复、清空及核对空间变化 |

**让 Agent 完工后自己收尾。** 在「Agent 工作区」登记进行中的任务，将原始素材填入「输入」、交付件填入「正式输出」、可舍弃的中间文件填入「临时产物」。在「扫描范围」启用「结束任务时隔离已登记的临时产物」。手动结束任务，或给 Claude Code 任务填写会话 ID 并接入 SessionEnd 后，既白会复核文件并隔离仍符合条件的临时产物。原项目需要某份文件时，可以从清理记录恢复。隔离占用在确认清空后才释放。[Claude 接入与收尾时序](docs/技术方案.md#6-可选的-agent-事件接入)

结束任务后可打开收尾单，查看素材引用、文件保留依据及关联的隔离或恢复批次。Codex 和 Gemini CLI 可自选接入本地 Hook，将回复结束、会话结束或受支持的中断信号记为会话观察，由用户审阅或登记任务。[会话观察接入](docs/技术方案.md#7-codex-与-gemini-cli-会话观察)

「模型存储」支持手动添加已授权目录。点击「核验运行状态」可查询已运行的 Ollama 或 LM Studio 默认本机服务。模型文件受保留保护。

可在符合条件的 Hugging Face 模型上保存恢复凭据。凭据记录公共仓库、固定修订、文件名、SHA-256 和大小。重新获取时由用户选择下载目录或已授权的普通文件夹；同名文件保留，校验成功后才发布下载结果。恢复后的模型可由用户导入对应工具。[模型恢复流程](docs/技术方案.md#9-固定修订的模型重新获取)

「扫描范围」中的本地 MCP 默认关闭。启用后可复制 Codex 连接命令或通用 MCP JSON。Agent 用本次扫描的别名拟定草案，用户在既白窗口逐项核对并批准，随后可在清理记录恢复。[MCP 配置与审批流程](docs/技术方案.md#8-本地-mcp-草案与原生审批)

**规则覆盖。** 低影响操作依赖具体应用版本及文件用途。当前包含 VS Code 1.95.3 的过期会话日志规则；样本应用规则限于既白创建的演练目录。模型按版本和共享 blob 盘点保护，重建成本未知的项目资产进入人工审阅。第三方应用包的可恢复卸载限明确适配且通过身份与占用检查的版本。

在「清理建议 → 开发工具缓存」看到 Docker 虚拟磁盘时，可点「读取构建缓存」，查看本机 Docker Desktop 内置构建器报告的总量、共享量及未使用且未共享的量。虚拟磁盘的已分配空间与构建器报告分别显示。[Docker 盘点口径](docs/技术方案.md#10-docker-构建缓存盘点)

应用分析把按 bundle ID 归属的用户缓存、沙盒缓存、临时文件，以及已知的 VS Code 运行缓存和 Xcode 中间产物列入待确认范围。隔离可以恢复，执行时会复核应用和文件占用。大文件页按磁盘已分配块估算大小，跳过符号链接目标、其他挂载卷及排除位置。选中的外部目录需显式纳入清理审阅。每层重新盘点可能改变共享硬链接的归属。

## 测试

```bash
./scripts/test-isolated.sh
./tests/native/smoke.sh
```

第一条命令在私有临时目录生成文件、应用包、工程和多份副本，使用 macOS 沙箱禁止测试进程联网及向测试目录外写入。第二条运行 Swift 客户端到真实 Python 引擎的隔离冒烟测试。两个脚本均自行回收临时夹具。[验证范围与结果](docs/验证报告.md)

## 从 GitHub 分发

发行者在签名 Mac 的钥匙串准备 Apple 团队的 **Developer ID Application** 证书及私钥，并通过 `xcrun notarytool store-credentials` 保存公证配置。始终使用同一 Apple 团队与既白的 bundle ID `org.lumen.cleaner`。

```bash
/usr/bin/python3 -B scripts/package.py \
  --output dist/release/Lumen.app \
  --sign-identity 'Developer ID Application: Example Name (TEAMID)' \
  --notary-profile lumen-notary \
  --release-zip dist/release/Lumen-macOS-arm64.zip
```

在 Apple Silicon Mac 构建得到 ARM64 包。脚本启用 Hardened Runtime，先签内部动态库，再签外层应用。Apple 接受公证并且应用内的票据通过验证后，脚本才生成可上传到 GitHub Releases 的 ZIP。签名私钥和公证配置保留在钥匙串。需要在签名 Mac 上验证两个发布包可互相满足对方的 designated requirement，并通过同一位置首次授权、退出重开、重启及覆盖升级的验收。[Apple 直接分发指南](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution) · [分发任务 #10](https://github.com/Moosphan/lumen/issues/10)

本机开发测试可单独传入 `--sign-identity` 使用钥匙串中的 Apple Development 身份。GitHub 公共分发使用 Developer ID 和公证。

## 技术与任务

- [技术分层、扫描与处置时序、状态机、协议和发布验收](docs/技术方案.md)
- [GitHub 任务单、依赖和验收条件](docs/任务索引.md) · [Agent 扩展计划与覆盖矩阵](docs/Agent扩展计划.md)
- [产品需求](产品需求文档.md) · [完整功能分层图](功能清单与分层功能图.md) · [竞品调研](竞品调研.md)
- [交互设计原型](设计稿/index.html) 使用演示数据；原生程序位于 `Sources/Lumen`，真实文件引擎位于 `engine/lumen`。
