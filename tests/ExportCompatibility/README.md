# 导出与导入检查

默认检查使用合成数据验证 C# 导出契约，不启动或操作游戏：

```powershell
dotnet run --project tests/ExportCompatibility/ExportCompatibility.csproj --configuration Release
```

`--convert <capture.fbrl> <java.exe> <adapter.jar>` 对指定原始录制运行实际转换。它只导出回放，不向播放器安装录制皮肤。

`--import` 调用所给引擎 DLL 的实际 `RecorderForm.ImportWithSkins`，先复制回放，再安装原生 Flashback 文件皮肤覆盖。需要通过该引擎的 `runtimeconfig.json` 加载 Windows Desktop 框架。测试应使用独立临时目标目录；目标需要包含 `mods/` 下的 Flashback JAR。

```powershell
dotnet exec --runtimeconfig flashback_engine/bin/Release/net8.0-windows/ZJHUB.Recorder.Engine.runtimeconfig.json tests/ExportCompatibility/bin/Release/net8.0/ExportCompatibility.dll --import flashback_engine/bin/Release/net8.0-windows/ZJHUB.Recorder.Engine.dll <replay.zip> <temporary-game-directory> <temporary-skin-cache-directory>
```

导入会写入目标目录的 `flashback/replays`、`flashback/recorded_skins` 和对应回放 UUID 的 `flashback/editor_states/<uuid>.json`；不会启动播放器。对正在打开的回放不要外部安装，以免播放器内存中的编辑状态覆盖安装结果。
