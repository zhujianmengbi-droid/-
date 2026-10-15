# ZJ HUB

面向起床战争玩家的 Windows 工具中心，把战局数据、排行榜、大厅聊天、快捷工具和 Flashback 录制放在一个窗口中。

**OOPZ 交流频道：881695**

[下载 v1.6.26](https://github.com/zhujianmengbi-droid/ZJ-HUB/releases/tag/v1.6.26) · [查看最新版本](https://github.com/zhujianmengbi-droid/ZJ-HUB/releases/latest) · [反馈问题](https://github.com/zhujianmengbi-droid/ZJ-HUB/issues)

## 下载与开始使用

| 文件 | 用途 |
| --- | --- |
| [ZJ-HUB-v1.6.26.exe](https://github.com/zhujianmengbi-droid/ZJ-HUB/releases/download/v1.6.26/ZJ-HUB-v1.6.26.exe) | Windows x64 主程序，下载后直接运行 |
| [ZJ-HUB-modpack.zip](https://github.com/zhujianmengbi-droid/ZJ-HUB/releases/download/v1.6.26/ZJ-HUB-modpack.zip) | Flashback 配套播放器整合包；也可从软件内保存 |
| [ZJ-HUB-v1.6.26-source.zip](https://github.com/zhujianmengbi-droid/ZJ-HUB/releases/download/v1.6.26/ZJ-HUB-v1.6.26-source.zip) | 本版源码及必要构建资源 |
| [SHA256 校验值](https://github.com/zhujianmengbi-droid/ZJ-HUB/releases/download/v1.6.26/ZJ-HUB-v1.6.26-SHA256SUMS.txt) | 核对下载文件是否完整 |

1. 在 **Windows 10 / 11 x64** 上，关闭旧版 ZJ HUB 后运行新 EXE。成品已包含 Python、Qt 和 .NET 运行环境。
2. 首次启动可验证游戏 ID，并按提示配置个人 API Token；也可选择“稍后登录”，直接使用快捷工具和 Flashback。
3. 使用录制功能前，按下方说明准备 JDK 与播放器，在 Flashback 页面选择游戏进程和输出目录。

需要校验下载文件时，在 PowerShell 中运行 `Get-FileHash .\ZJ-HUB-v1.6.26.exe -Algorithm SHA256`，与发布页的校验文件对照。

## 可以做什么

- **战局数据与排行榜**：查询玩家数据，保留最近查询；可调整自动加载、记录数量和对局卡片间距。
- **大厅聊天**：公共频道消息与在线人数，游戏 ID 可在设置中重新验证或换绑。输入 `/player` 查看当前在线的 ZJ HUB 大厅玩家，输入 `/help` 查看指令说明。
- **快捷工具**：电脑清理、窗口与任务栏相关工具，集中管理常用操作。
- **Flashback**：内置录制引擎、录制状态与日志、最近回放、回放信息、导出和安装已有回放。
- **界面设置**：明亮、暗色、暖砂三套主题，统一滚动条与控件；支持顺滑滚动、背景视差及动画开关。

## v1.6.26 更新

本版删除液态玻璃主题，保留明亮、暗色和暖砂的既有样式、布局、图标与动画。暗色使用中性炭黑与灰阶，暖砂使用米色与铜棕；这两套主题不使用蓝紫主题色。

大厅聊天新增两条本地指令：`/player` 显示 ZJ HUB 软件大厅当前在线玩家，`/help` 显示可用指令及用法。指令和结果只在本机处理，不发送到公共聊天频道；在线列表来自软件大厅的在线状态，不是 Minecraft 服务器全部玩家。

优化集中在界面工作量与慢磁盘访问：聊天复用已有气泡，任务栏内容不变时保留表格；重复点击当前主题不再重设全局样式；设置只刷新相关控件，并合并音量拖动产生的配置写入。回放扫描、ZIP 信息读取和目录查找移至后台，过期结果不会覆盖新选择，关闭窗口也不等待慢磁盘查询。

顺滑滚动、背景视差和入场动画继续保留。“设置 → 性能 → 滚动视差”可单独关闭视差；关闭界面动画或 Windows 客户区动画后恢复原生滚动。

![ZJ HUB v1.6.26 主题设置](https://github.com/zhujianmengbi-droid/ZJ-HUB/releases/download/v1.6.26/ZJ-HUB-v1.6.26-themes.png)

完整内容见 [更新说明](https://github.com/zhujianmengbi-droid/ZJ-HUB/blob/main/release-notes/1.6.26.txt) 与 [流畅度审查记录](https://github.com/zhujianmengbi-droid/ZJ-HUB/releases/download/v1.6.26/ZJ-HUB-v1.6.26-performance-notes.txt)。

## Flashback 录制与播放

| 项目 | 当前适配范围与要求 |
| --- | --- |
| 录制客户端 | 网易 Minecraft 1.20.1，以及 Minecraft 1.21.10 / NeoForge 21.10.64 |
| 录制辅助运行时 | **x64 Java 17+ JDK，包含 `jdk.attach`**；精简 JRE 不足以附加游戏 |
| 现代游戏运行时 | 1.21.10 组件在目标游戏的 Java 21+ JVM 内运行 |
| 回放播放器 | **Minecraft 1.21.11 / Flashback 0.39.10** |
| 导出格式 | `.flashback.zip` |

ZJ HUB 会在后台寻找并验证 JDK。安装 JDK 后可在界面刷新重试；游戏进程需要允许 Java Attach。录制和导出都由 ZJ HUB 内置引擎完成，播放与编辑则在对应版本的 Minecraft / Flashback 中进行。

大厅或子服切换会记录新连接的配置与世界状态；导出时生成质量报告，对不完整片段及目标播放器的兼容限制提供诊断。停止录制后如导出失败，请保留输出目录下的 `.flashback-runtime` 原始录制，便于排查或重新导出。实际完整性还取决于录制时捕获到的数据，不能仅凭成功生成 ZIP 判断。

**皮肤安装**：使用 ZJ HUB 的“安装已有回放”或自动导入流程。软件临时皮肤缓存按本次运行隔离，正常关闭后清理；播放器使用的副本保存在所选版本目录的 `flashback/recorded_skins`，不会随软件缓存一起删除。重新导入可从回放 ZIP 补齐缺失文件，并保留已有手工皮肤设置。单独复制 ZIP 不会生成播放器的皮肤覆盖状态；旧录制没有采集到的真实皮肤也不能凭空恢复。

详细适配范围、组件机制与测试命令见 [内置录制引擎说明](flashback_engine/README.md)。

## 验证范围

发布检查覆盖 UI 回归，以及本机的新配置、中文只读安装目录和高 DPI 三种隔离启动场景，并核对打包模块、内置引擎和源码包；本次最终发布包的检查结果见 [流畅度审查记录](https://github.com/zhujianmengbi-droid/ZJ-HUB/releases/download/v1.6.26/ZJ-HUB-v1.6.26-performance-notes.txt)。

这些检查覆盖界面与打包行为，**不等同于全新 Windows 虚拟机验证，也不证明长时间录制、多次转服和每段回放内容都已完整验收**。本版流畅度优化没有改动录制协议或转换算法。性能记录是本机指定操作的对比，不代表所有电脑的整体帧率。

遇到问题时，请在反馈中提供 ZJ HUB 版本、Minecraft / Java 版本、触发步骤和相关日志；回放异常也请保留原始录制与质量报告。

## 从源码构建

在 Windows x64 安装 64 位 Python 和 **.NET 8 SDK**，在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
python build_large.py
python package_source.py
```

主程序输出为 `dist/ZJ HUB.exe`。完整构建会先发布自包含录制引擎，再用 `zj_hub.spec` 打包主程序；请保留这一流程，以免混入旧版引擎。Pillow 仍用于构建图标，已从成品运行时依赖中移除。可选 UPX 目录通过 `ZJ_HUB_UPX_DIR` 指定。

源码包使用白名单收集 Python / C# 源码、新增 Java 实现、测试及必要预构建 JAR，不包含用户设置、录制文件和构建缓存；原有 JAR 的全部第三方源码不在本包范围内。需要重新编译 Java 组件时，参阅 [引擎构建说明](flashback_engine/README.md#构建)。源码采用 UTF-8，PowerShell 读取中文文本时请指定 `-Encoding utf8`。

## 大厅服务与更新提示

客户端默认使用 Supabase Realtime 公共频道，聊天缓存随本次运行清理，不提供私聊。仓库也保留可自托管的 `lobby_server.py` 与 `render.yaml`：内存状态部署应保持单 Worker；信任反向代理地址前，须按实际部署配置 `LOBBY_TRUSTED_PROXY_CIDRS`，默认不信任请求中的 `X-Forwarded-For`。

程序启动后查询本仓库的最新 Release。发现新版本时显示“可更新”，点击会打开 GitHub 发布页，由用户下载并运行新版；**不会自动替换或安装程序**。
