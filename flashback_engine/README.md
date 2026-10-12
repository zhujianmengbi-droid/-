# ZJ HUB 内置录制引擎

此目录包含 FlashbackRecorder 的 C# 录制/导出逻辑及其 Java 运行时资源。ZJ HUB 通过隐藏的 `--bridge-stdio` 进程调用引擎，所有操作界面由 ZJ HUB 的 PySide6 页面提供。用户只需一个 ZJ HUB EXE，无需桌面上的 FlashbackRecorder 软件或插件目录。

## 构建

在 Windows x64 安装 Python、项目 `requirements.txt` 依赖和 .NET 8 SDK，然后在项目根目录执行：

```powershell
python build_large.py
python package_source.py
```

构建脚本先将引擎发布到 `flashback_engine/bin/ZJHUB.Recorder.Engine.exe`，使用 `win-x64`、自包含 .NET 运行时和单文件发布，再由 PyInstaller 将这个 EXE 收进 ZJ HUB。不要只运行 PyInstaller 或手工复制另一版本引擎。引擎的 `bin/`、`obj/` 和本机设置不会进入源码包。

## 运行依赖与文件

- ZJ HUB 及引擎无需用户安装 Python 或 .NET。
- 录制和协议转换仍需要本机 **64 位 JDK 17 或 21**，包含 `jdk.attach` 模块；普通精简 JRE 不足以进行 Attach。优先使用 `JAVA_HOME`、常见 JDK 安装路径或 PATH 中的 `java.exe`。
- 游戏进程必须允许 Java Attach，并使用支持的 Minecraft 版本。软件不会代替 Minecraft 或 Flashback 播放器；播放、编辑导出的回放仍使用对应游戏中的 Flashback。
- 三个 JAR 均作为 C# 嵌入资源打包。资源名称和引擎读取名称一致，提取/校验由 `RuntimeAssets` 负责。
- `runtime-recorder-agent-v12.jar` 来源于原交付包中名为 `runtime-recorder-agent-v6.jar` 的资源，文件内容未变；这里按引擎实际要求的版本名命名。

## 源码包范围

`package_source.py` 使用白名单打包 Python/C# 源码、构建脚本、图标和三个必需 JAR。它不包含录制文件、聊天缓存、用户设置、构建缓存或外部软件路径。Java JAR 使用当前交付的预构建资源；此目录不声称包含这些 JAR 的全部 Java 构建源码。

## 大小

协议适配器 JAR 约 43.3 MiB，其余两个 JAR 约 218 KiB。原自包含引擎约 112 MiB；压缩单文件和外层 PyInstaller 会使最终大小变化，以 `dist/ZJ HUB.exe` 的实际大小为准。
