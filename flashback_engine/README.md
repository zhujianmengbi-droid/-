# ZJ HUB 内置录制引擎（1.20.1 / 1.21.10）

此目录包含 FlashbackRecorder 的 C# 录制/导出逻辑及其 Java 运行时资源。ZJ HUB 通过隐藏的 `--bridge-stdio` 进程调用引擎，所有操作界面由 ZJ HUB 的 PySide6 页面提供。用户只需一个 ZJ HUB EXE，无需桌面上的 FlashbackRecorder 软件或插件目录。

## 2026-10-16 适配

新版布吉岛客户端使用 Minecraft 1.21.10、NeoForge 21.10.64、协议 773、数据版本 4556。引擎保留原 1.20.1 的 v13 组件，并根据验证后的游戏版本选择独立的现代组件。被启动器改写的命令行通过正在加载的 LWJGL/GLFW 模块及对应游戏 JAR 的 `version.json` 识别；不会仅凭窗口标题或文件夹名选择组件。

新增组件 `runtime-recorder-agent-1.21.10-v2.jar` 使用 `AgentV2` 入口，报告组件版本 17，使用 Java 21 字节码，在目标游戏的 Java 21+ JVM 内运行。辅助 Attach/导出进程需要可用的 Java 17+ JDK 和 `jdk.attach` 模块。新版 CLI 根据精确的 Agent 入口名称为现代组件传入实际实现 JAR 路径，使同一个游戏 JVM 可以重新加载更新后的实现。组件 17 同时替换录制回调，将实际处理包的新连接对象交给采集器，避免转服首包错误地读取旧玩家连接。`flashback-protocol-adapter-v6.jar` 保留原 v5 的所有类，新增 773 → 774 转换入口；导出仍交给配套 Minecraft 1.21.11 / Flashback 0.39.10 播放。

现代录制会保存配置注册表快照。缺少注册表、源版本与协议冲突、数据帧截断或动态转换失败时，导出会报错并保留原始录制。不要手动删除 `.flashback-runtime` 中的原始文件来处理导出错误。

组件 17 使用 `captureFormat=5`。大厅/子服切换时继续录制，暂停等待新世界的坐标采样；新 LOGIN 前保存完整 registry/features/tags 快照到 `runtime-configurations.bin`，按成功写入的源数据包序号与 tick 绑定，不能仅按时间猜测。转换器为新连接重建协议转换状态并更新源实体 ID，同时维持固定的回放本地玩家 ID。录制关闭、缓冲刷新及属性写入完成后，才发布 `captureClosed=true`。

用户在转服完成前停止时，未完成的尾段会明确记为不完整；没有目标世界位置样本的瞬时转服会整段省略，保留后续拥有完整配置和位置样本的世界，不伪造位置。异常关闭的录制也会尝试自动导出有效内容，质量报告保留原始错误。配置帧截断、索引错配或其他不可安全转换的问题仍会拒绝导出。

ZJ HUB 1.6.19 按数据包顺序与采样证据识别现代格式 5 的连续转场边界：当 LOGIN 后直接发生 Respawn（或连续 Respawn），中间没有其它游戏数据包，也没有已采样的中间世界帧时，仅合并冗余的本地玩家重建标记。包可以位于不同 tick，所有真实配置、登录、重生、后续数据和时间间隔均保留；不会依靠固定时间窗口猜测转场。最终世界的真实位置样本用于重建玩家。有世界数据包或中间样本时不进行这种合并。质量报告中的 `coalescedTransitionMarkers` 记录合并数，不计为数据丢失。此修复不更换组件 17，已有完整原始数据可直接重新导出；旧格式 3/4 的严格检查保持不变。

已经附加旧现代组件时，应先停止录制，再在 ZJ HUB 点击“更新录制组件”。程序只在实际读到组件 17 的状态后允许开始新版录制。录制中不会替换组件。

现代转换会过滤目标客户端不支持的自定义注册表标签组，同时省略没有任何标签的注册表组，与原版网络序列化一致。Minecraft 注册表中的模组标签仍保留。自定义注册表过滤数量写入 `converted.properties` 和回放中的 `capture-diagnostics.json`，录制质量显示为兼容限制，不计为意外丢包或自定义数据包丢弃。

NPC 的玩家资料从实际世界实体补齐，不再仅依赖 Tab 列表，并保留原始皮肤属性和签名。旧现代录制可按录制皮肤清单内的真实 UUID 与名字恢复缺失资料；缺少真实身份时记录诊断而不猜测。旧录制未采集的真实皮肤不能凭空恢复，应使用新版重新录制。

皮肤需要通过 ZJ HUB 的“安装已有回放”或自动导入流程安装，单独复制 ZIP 不会生成 Flashback 的皮肤覆盖状态。安装时应先关闭正在打开的同一回放，再重新打开；ZJ HUB 会保留已有手工皮肤设置。

1.6.21 的软件皮肤缓存采用每个进程独立的临时目录，正常退出后删除。缓存路径不写入持久设置，不会在换电脑后恢复过时路径。播放器副本位于所选版本目录的 `flashback/recorded_skins`，编辑状态只引用该副本，软件缓存删除不会影响已安装回放。重复导入会从 ZIP 重建缓存并保留已有编辑状态。Java 发现移到后台，并在执行探测前检查 x64 PE 机器类型；刷新按钮可重试新安装的 JDK。

## 构建

在 Windows x64 安装 Python、项目 `requirements.txt` 依赖和 .NET 8 SDK，然后在项目根目录执行（源码包已含需要的预构建 JAR）：

```powershell
python build_large.py
python package_source.py
```

构建脚本先将引擎发布到 `flashback_engine/bin/ZJHUB.Recorder.Engine.exe`，使用 `win-x64`、自包含 .NET 运行时和单文件发布，再由 PyInstaller 将这个 EXE 收进 ZJ HUB。不要只运行 PyInstaller 或手工复制另一版本引擎。引擎的 `bin/`、`obj/` 和本机设置不会进入源码包。

## 运行依赖与文件

- ZJ HUB 及引擎无需用户安装 Python 或 .NET。
- C# 的 `System.Management` 8.0 随自包含引擎发布，进程检测在后台线程内直接查询 WMI，不启动 PowerShell。模块快照使用 Windows Toolhelp 的 64 位及 WOW64 标志；无法读取权限受限进程时不猜测版本、不启用注入。
- 录制和协议转换仍需要本机 **64 位 Java 17+ JDK**，包含 `jdk.attach` 模块；普通精简 JRE 不足以进行 Attach。软件会依次检查 `JAVA_HOME`/`JDK_HOME`、Windows 注册表、带版本号的常见安装目录、Minecraft 启动器运行时、正在运行的 Java 进程和 PATH，并执行 `java --add-modules jdk.attach -version` 验证。
- 游戏进程必须允许 Java Attach，并使用支持的 Minecraft 版本。软件不会代替 Minecraft 或 Flashback 播放器；播放、编辑导出的回放仍使用对应游戏中的 Flashback。
- 四个 JAR 均作为 C# 嵌入资源打包。资源名称和引擎读取名称一致，提取/校验由 `RuntimeAssets` 负责。
- `runtime-recorder-agent-v13.jar` 是按 Minecraft 1.20.1 SRG 映射修复过的运行时组件：属性数据包会通过属性注册表实际查找资源键，无法识别的条目会被安全跳过，并以快照副本写回，避免把空资源键传给客户端。`flashback-protocol-adapter-v6.jar` 中保留的旧版入口会先按 tick 稳定排序运行时数据，再执行 1.20.1 → 1.21.11 转换，避免并发写入导致时间线回退。

## 源码包范围

`package_source.py` 使用白名单打包 Python/C# 源码、构建脚本、图标、四个必需 JAR、新增 Java 实现及定向回归测试。它不包含录制文件、聊天缓存、用户设置或构建缓存。原 v13 与原协议库仍使用已有预构建资源；本包不声称包含原 JAR 所有第三方代码的构建源码。

需要重建新增 Java 代码时，在此引擎目录分别运行 `java-cli/build.ps1 -JavaHome <JDK17或更新目录>`、`java-modern-agent/build.ps1 -InstanceRoot <包含 libraries 和 versions 的客户端根目录> -JavaHome <JDK21目录>` 和 `java-modern-adapter/build.ps1 -Jdk <JDK17或更新目录>`，再从项目根目录完整运行 `python build_large.py`。现代 Agent 构建使用该客户端已经安装的 1.21.10 / NeoForge 21.10.64 编译依赖，不把游戏文件装入源码包。

从项目根目录执行定向检查：`dotnet run --project tests/GameDiscovery/GameDiscovery.Tests.csproj --configuration Release`、`dotnet run --project tests/ExportCompatibility/ExportCompatibility.csproj --configuration Release`。CLI 离线检查为 `flashback_engine/java-cli/test.ps1 -JavaHome <JDK17或更新目录>`；Java 转换器协议检查见 `java-modern-adapter/ModernAdapterSelfTest.java`。这些检查不替代实际游戏录制及播放器验证。

关闭桥接回归：先运行上述 ExportCompatibility 生成测试数据，再运行 `dotnet build tests/BridgeLifecycle/BridgeLifecycle.csproj -c Release` 和 `python tests/BridgeLifecycle/test_bridge_close.py`。它在独立目录使用隐藏的真实桥接和测试适配器，检查导出中关闭、取消、子进程退出及原始录制保留，不会附加游戏。大厅限流检查为 `python -m unittest discover -s tests/LobbySecurity -v`。

## 大小

协议适配器 JAR 约 43.4 MiB，其余三个 JAR 合计约 406 KiB。压缩单文件和外层 PyInstaller 会使最终大小变化，以 `dist/ZJ HUB.exe` 的实际大小为准。
