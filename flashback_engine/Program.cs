using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace FlashbackRecorder;

internal sealed class GameProcess
{
    public int Pid { get; init; }
    public string Name { get; init; } = "java.exe";
    public string CommandLine { get; init; } = "";
    public string StartKey { get; init; } = "";
    public bool IsNetease { get; init; }
    public bool Supported { get; init; }
    public string Version { get; init; } = "";
    public string Identity => $"{Pid}:{StartKey}";
    public override string ToString() => string.IsNullOrWhiteSpace(CommandLine)
        ? $"{(IsNetease ? "[网易]" : "[Minecraft]")} PID {Pid}  {Name}"
        : $"{(IsNetease ? "[网易]" : "[Minecraft]")} PID {Pid}  {Name}  {CommandLine}";
}

internal static class RuntimeAssets
{
    public static string Root { get; } = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FlashbackRecorder", "runtime-recorder");
    public static string Cli => Path.Combine(Root, "runtime-recorder-cli.jar");
    // Versioned names avoid Windows keeping an older Agent JAR open inside a
    // JVM.  A running game can therefore keep its old session while the next
    // launch extracts the repaired Agent and adapter side by side.
    public const int RequiredAgentVersion = 12;
    public static string Agent => Path.Combine(Root, "runtime-recorder-agent-v12.jar");
    public static string ProtocolAdapter => Path.Combine(Root, "flashback-protocol-adapter-v5.jar");

    public static void Ensure()
    {
        Directory.CreateDirectory(Root);
        foreach (var file in new[] { ("runtime-recorder-cli.jar", Cli), ("runtime-recorder-agent-v12.jar", Agent), ("flashback-protocol-adapter-v5.jar", ProtocolAdapter) })
        {
            if (IsCurrent(file.Item2, file.Item1)) continue;
            var name = Assembly.GetExecutingAssembly().GetManifestResourceNames().FirstOrDefault(n => n.EndsWith(file.Item1, StringComparison.OrdinalIgnoreCase));
            if (name is null) throw new FileNotFoundException("EXE 内未找到运行时组件", file.Item1);
            using var input = Assembly.GetExecutingAssembly().GetManifestResourceStream(name)!;
            using (var output = File.Create(file.Item2 + ".tmp"))
            {
                input.CopyTo(output);
            }
            File.Move(file.Item2 + ".tmp", file.Item2, overwrite: true);
        }
    }

    /// <summary>True when the extracted jar matches the jar embedded in this EXE,
    /// so updated builds overwrite jars left behind by older builds.</summary>
    static bool IsCurrent(string path, string resourceSuffix)
    {
        try
        {
            if (!File.Exists(path) || new FileInfo(path).Length <= 100) return false;
            var name = Assembly.GetExecutingAssembly().GetManifestResourceNames()
                .FirstOrDefault(n => n.EndsWith(resourceSuffix, StringComparison.OrdinalIgnoreCase));
            if (name is null) return false;
            using var embedded = Assembly.GetExecutingAssembly().GetManifestResourceStream(name)!;
            var embeddedHash = System.Security.Cryptography.SHA256.HashData(embedded);
            var localHash = System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(path));
            return embeddedHash.AsSpan().SequenceEqual(localHash);
        }
        catch { return false; }
    }
}

internal sealed class RecorderForm : Form
{
    readonly ListBox processes = new() { Dock = DockStyle.Fill, IntegralHeight = false, BackColor = Color.FromArgb(25, 29, 35), ForeColor = Color.FromArgb(176, 184, 196), BorderStyle = BorderStyle.FixedSingle };
    readonly Label notice = new() { AutoSize = false, Dock = DockStyle.Top, Height = 54, Padding = new Padding(10), TextAlign = ContentAlignment.MiddleLeft };
    readonly Label state = new() { Text = "等待手动注入", AutoSize = false, Height = 32, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
    readonly Label pidLabel = new() { Text = "-", AutoSize = false, Height = 26, Dock = DockStyle.Fill };
    readonly Label samples = new() { Text = "0", AutoSize = false, Height = 26, Dock = DockStyle.Fill };
    readonly Label packets = new() { Text = "0", AutoSize = false, Height = 26, Dock = DockStyle.Fill };
    readonly Label replay = new() { Text = "录制尚未开始", AutoSize = false, Dock = DockStyle.Fill, AutoEllipsis = true };
    readonly TextBox output = new() { Dock = DockStyle.Fill };
    readonly TextBox flashbackDirectory = new() { Dock = DockStyle.Fill, PlaceholderText = "完整版 Flashback 所在的游戏目录" };
    readonly NumericUpDown readRate = new() { Minimum = 20, Maximum = 120, Value = 20, Increment = 10, Dock = DockStyle.Fill };
    readonly ToolTip hints = new();
    readonly TextBox marker = new() { Dock = DockStyle.Fill, PlaceholderText = "时间线标记，例如：进入矿洞" };
    readonly CheckBox autoZip = new() { Text = "停止后自动生成回放 ZIP", Checked = true, AutoSize = true };
    readonly RichTextBox log = new() { Dock = DockStyle.Fill, ReadOnly = true, BackColor = Color.FromArgb(18, 23, 32), ForeColor = Color.Gainsboro, BorderStyle = BorderStyle.FixedSingle };
    readonly RecorderButton inject = new() { Text = "注入选中游戏", Description = "手动加载录制组件", Dock = DockStyle.Fill };
    readonly RecorderButton start = new() { Text = "开始录制", Description = "从现在开始保存游戏", Dock = DockStyle.Fill, Enabled = false };
    readonly RecorderButton stop = new() { Text = "停止录制", Description = "结束本段并保存文件", Dock = DockStyle.Fill, Enabled = false };
    readonly RecorderButton mark = new() { Text = "添加时间标记", Description = "记录旁边填写的事件", Dock = DockStyle.Fill, Enabled = false };
    readonly RecorderButton refresh = new() { Text = "刷新进程", Description = "重新检测，不会注入", Dock = DockStyle.Fill };
    readonly RecorderButton browse = new() { Text = "浏览", Dock = DockStyle.Fill };
    readonly RecorderButton browseFlashback = new() { Text = "浏览", Dock = DockStyle.Fill };
    readonly RecorderButton importReplay = new() { Text = "安装回放到 Flashback", Description = "选择已有 ZIP，复制回放和皮肤以便播放", Dock = DockStyle.Fill };
    readonly RecorderButton open = new() { Text = "打开保存目录", Description = "查看已经保存的录制", Dock = DockStyle.Fill };
    readonly RecorderButton export = new() { Text = "导出 / 重试", Description = "重新转换最近一次录制", Dock = DockStyle.Fill, Enabled = false };
    readonly System.Windows.Forms.Timer processTimer = new() { Interval = 2500 };
    readonly System.Windows.Forms.Timer statusTimer = new() { Interval = 600 };
    readonly string settingsPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FlashbackRecorder", "settings.json");
    readonly string javaPath;
    readonly CancellationTokenSource lifetime = new();
    readonly HashSet<string> attemptedExports = new(StringComparer.OrdinalIgnoreCase);
    readonly bool diagnosticsOnly;
    readonly string uiTheme;
    bool scanning;
    bool polling;
    bool exporting;
    bool assetsReady;
    bool closing;
    readonly HashSet<string> reportedErrors = new(StringComparer.Ordinal);
    Dictionary<string, string> latestStatus = new(StringComparer.Ordinal);
    string? lastLogMessage;
    GameProcess? selected;
    string? stateDir;
    string? attachedProcessKey;
    string? replayPath;
    bool busy;
    bool warnedOldAgent;
    string? failedExport;
    readonly bool bridgeStdio;
    readonly object bridgeOutputLock = new();
    readonly List<(long Sequence, string Time, string Text)> bridgeLogs = new();
    long bridgeLogSequence;
    long bridgeLastLogSent;
    long bridgeErrorSequence;
    string bridgeLastError = "";
    bool bridgeExitRequested;
    bool bridgeShutdownPrepared;
    bool bridgeShuttingDown;

    public RecorderForm(bool diagnosticsOnly = false, string? settingsFile = null, string? theme = null, bool bridgeStdio = false)
    {
        this.diagnosticsOnly = diagnosticsOnly;
        this.bridgeStdio = bridgeStdio;
        uiTheme = NormalizeTheme(theme);
        if (bridgeStdio)
        {
            ShowInTaskbar = false;
            ShowIcon = false;
            Opacity = 0;
        }
        settingsPath = settingsFile ?? settingsPath;
        Text = "Flashback Recorder · 2026-10-12 手动注入版" + (diagnosticsOnly ? " · 诊断模式" : "");
        MinimumSize = new Size(1000, 720); Size = new Size(1280, 840);
        AutoScaleMode = AutoScaleMode.Dpi;
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScroll = true;
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(22, 25, 31); ForeColor = Color.FromArgb(177, 184, 194);
        Font = new Font("Microsoft YaHei UI", 9f);
        javaPath = FindJava();
        BuildUi(); LoadSettings(); ApplyControls(); ApplyThemePalette();
        processTimer.Tick += async (_, _) => await DetectProcessesAsync();
        statusTimer.Tick += async (_, _) => await ReadStatusAsync();
        Shown += async (_, _) =>
        {
            if (bridgeStdio)
            {
                // Keep the WinForms message pump alive for Attach and status
                // callbacks, but do not expose a second Flashback window.
                ShowInTaskbar = false;
                WindowState = FormWindowState.Minimized;
                Hide();
            }
            if (diagnosticsOnly)
            {
                Log("诊断模式：仅检测进程和检查界面，不附加游戏、不发送录制命令。");
                processTimer.Start();
                await DetectProcessesAsync();
                if (bridgeStdio) _ = Task.Run(BridgeStdioLoopAsync);
                return;
            }
            try { await Task.Run(RuntimeAssets.Ensure, lifetime.Token); assetsReady = true; }
            catch (Exception ex) { ReportError("运行时组件提取失败", ex); }
            if (closing) return;
            processTimer.Start(); statusTimer.Start();
            await DetectProcessesAsync();
            if (bridgeStdio) _ = Task.Run(BridgeStdioLoopAsync);
        };
        FormClosing += (_, e) =>
        {
            if (exporting || busy) { e.Cancel = true; Log("正在执行操作，请完成后再关闭。导出在后台进行，界面仍可操作。"); return; }
            closing = true; processTimer.Stop(); statusTimer.Stop(); lifetime.Cancel();
            Diagnostics.Write("正常关闭录制器窗口。");
        };
    }

    async Task BridgeStdioLoopAsync()
    {
        try
        {
            string? line;
            while (!bridgeExitRequested && (line = await Console.In.ReadLineAsync()) is not null)
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                object response;
                try
                {
                    using var document = JsonDocument.Parse(line);
                    response = await InvokeOnUiAsync(async () =>
                    {
                        var command = "";
                        try
                        {
                            command = BridgeCommand(document.RootElement);
                            if (document.RootElement.TryGetProperty("afterLog", out var afterLog) && afterLog.TryGetInt64(out var cursor))
                                bridgeLastLogSent = Math.Max(0, Math.Min(cursor, bridgeLogSequence));
                            return await HandleBridgeCommandAsync(document.RootElement);
                        }
                        catch (Exception error)
                        {
                            Log("ZJ HUB 请求失败：" + error.Message);
                            return BridgeSnapshot(command, error: error.Message);
                        }
                    });
                }
                catch (JsonException error) { response = BridgeError("请求不是有效 JSON：" + error.Message); }
                catch (Exception error) { response = BridgeError(error.Message); }
                WriteBridgeResponse(response);
            }
        }
        catch (Exception error) { WriteBridgeResponse(BridgeError("标准输入桥接失败：" + error.Message)); }
        finally
        {
            try
            {
                await InvokeOnUiAsync(async () =>
                {
                    try { await PrepareBridgeShutdownAsync(); }
                    catch (Exception error) { Diagnostics.Write("ZJ HUB 连接关闭时收尾失败", error); }
                    if (!IsDisposed) Close();
                    return true;
                });
            }
            catch { }
        }
    }

    Task<T> InvokeOnUiAsync<T>(Func<Task<T>> action)
    {
        var completion = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
        if (IsDisposed || Disposing)
        {
            completion.SetException(new ObjectDisposedException(nameof(RecorderForm)));
            return completion.Task;
        }
        try
        {
            BeginInvoke((System.Windows.Forms.MethodInvoker)(async () =>
            {
                try { completion.SetResult(await action()); }
                catch (Exception error) { completion.SetException(error); }
            }));
        }
        catch (Exception error) { completion.SetException(error); }
        return completion.Task;
    }

    static object BridgeError(string message) => new Dictionary<string, object?>
    {
        ["ok"] = false, ["error"] = message,
    };

    void WriteBridgeResponse(object response)
    {
        try
        {
            lock (bridgeOutputLock)
            {
                Console.Out.WriteLine(JsonSerializer.Serialize(response));
                Console.Out.Flush();
            }
        }
        catch { }
    }

    object BridgeSnapshot(string command, string? message = null, string? error = null)
    {
        ApplyControls();
        static object ProcessRow(GameProcess process) => new Dictionary<string, object?>
        {
            ["pid"] = process.Pid, ["name"] = process.Name, ["identity"] = process.Identity,
            ["commandLine"] = process.CommandLine, ["supported"] = process.Supported,
            ["netease"] = process.IsNetease, ["version"] = process.Version,
        };
        var rows = bridgeLogs.Where(item => item.Sequence > bridgeLastLogSent)
            .Select(item => new { seq = item.Sequence, time = item.Time, text = item.Text }).ToArray();
        bridgeLastLogSent = bridgeLogSequence;
        return new Dictionary<string, object?>
        {
            ["ok"] = error is null, ["error"] = error, ["command"] = command, ["message"] = message,
            ["stateText"] = state.Text, ["notice"] = notice.Text, ["javaPath"] = javaPath,
            ["recording"] = latestStatus.GetValueOrDefault("recording") == "true",
            ["status"] = new Dictionary<string, string>(latestStatus),
            ["agent"] = new { requiredVersion = RuntimeAssets.RequiredAgentVersion,
                version = latestStatus.GetValueOrDefault("agentVersion"), outdated = warnedOldAgent },
            ["settings"] = new { output = output.Text, flashbackDirectory = flashbackDirectory.Text,
                readRate = (int)readRate.Value, autoZip = autoZip.Checked },
            ["controls"] = new { inject = inject.Enabled, start = start.Enabled, stop = stop.Enabled,
                mark = mark.Enabled, refresh = refresh.Enabled, export = export.Enabled,
                importReplay = importReplay.Enabled, output = browse.Enabled && stateDir is null,
                target = browseFlashback.Enabled, rate = readRate.Enabled, autoZip = autoZip.Enabled },
            ["selected"] = selected is null ? null : ProcessRow(selected),
            ["processes"] = processes.Items.OfType<GameProcess>().Select(ProcessRow).ToArray(),
            ["sessionDir"] = stateDir, ["replay"] = replayPath,
            ["assetsReady"] = assetsReady, ["busy"] = busy || exporting || scanning || polling,
            ["logs"] = rows, ["logSequence"] = bridgeLogSequence,
            ["shutdown"] = bridgeShutdownPrepared,
        };
    }

    static string BridgeCommand(JsonElement request)
    {
        if (request.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("请求必须是 JSON 对象。");
        if (request.TryGetProperty("command", out var command) && command.ValueKind == JsonValueKind.String)
            return command.GetString()?.Trim().ToLowerInvariant() ?? "";
        if (request.TryGetProperty("action", out var action) && action.ValueKind == JsonValueKind.String)
            return action.GetString()?.Trim().ToLowerInvariant() ?? "";
        return "";
    }

    static string? BridgeString(JsonElement request, string name) =>
        request.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()?.Trim() : null;

    static int? BridgeRate(JsonElement request)
    {
        if (!request.TryGetProperty("rate", out var value) && !request.TryGetProperty("readRate", out value))
            return null;
        if (!value.TryGetInt32(out var rate) || rate is < 20 or > 120)
            throw new InvalidDataException("读取频率须为 20 至 120 的整数。");
        return rate;
    }

    GameProcess? ResolveBridgeProcess(JsonElement request)
    {
        var identity = BridgeString(request, "identity");
        if (!string.IsNullOrWhiteSpace(identity))
            return processes.Items.OfType<GameProcess>().FirstOrDefault(item => item.Identity == identity);
        if (request.TryGetProperty("pid", out var pidValue))
        {
            if (!pidValue.TryGetInt32(out var pid)) throw new InvalidDataException("pid 必须为整数。");
            return processes.Items.OfType<GameProcess>().FirstOrDefault(item => item.Pid == pid);
        }
        return selected;
    }

    void RequireBridgeControl(bool enabled, string message)
    {
        if (closing || bridgeShuttingDown) throw new InvalidOperationException("录制服务正在关闭。");
        if (!enabled) throw new InvalidOperationException(message);
    }

    async Task BridgeOperationAsync(Func<Task> action)
    {
        var errorsBefore = bridgeErrorSequence;
        await action();
        if (bridgeErrorSequence != errorsBefore)
            throw new IOException(bridgeLastError);
    }

    async Task<object> HandleBridgeCommandAsync(JsonElement request)
    {
        var command = BridgeCommand(request);
        if (string.IsNullOrEmpty(command)) throw new InvalidDataException("缺少 command 字段。");
        ApplyControls();
        switch (command)
        {
            case "status":
                await ReadStatusAsync();
                return BridgeSnapshot(command);
            case "refresh":
                RequireBridgeControl(refresh.Enabled, "正在执行其他操作，请稍后刷新。");
                await BridgeOperationAsync(DetectProcessesAsync);
                return BridgeSnapshot(command);
            case "select":
            {
                RequireBridgeControl(!busy && !exporting && !scanning, "正在执行操作，暂时不能切换进程。");
                var process = ResolveBridgeProcess(request)
                    ?? throw new InvalidDataException("未找到指定的游戏进程，请刷新列表。");
                selected = process;
                processes.SelectedItem = process;
                ApplyControls();
                return BridgeSnapshot(command, $"已选择 PID {process.Pid}");
            }
            case "inject":
            {
                var process = ResolveBridgeProcess(request)
                    ?? throw new InvalidDataException("请先选择游戏进程。");
                if (request.TryGetProperty("pid", out _) || request.TryGetProperty("identity", out _))
                {
                    selected = process; processes.SelectedItem = process; ApplyControls();
                }
                RequireBridgeControl(inject.Enabled,
                    stateDir is not null ? "已有活动会话，请直接录制。" :
                    !assetsReady ? "录制组件未准备好。" : "当前进程不可注入，或正在执行其他操作。");
                if (javaPath.Length == 0) throw new InvalidOperationException("未找到 Java 17/21。");
                await BridgeOperationAsync(() => InstallAsync(process));
                if (stateDir is null) throw new IOException("注入未建立会话，请刷新游戏进程后重试。");
                return BridgeSnapshot(command, "Attach 请求已完成，正在等录制组件就绪。");
            }
            case "start":
            {
                RequireBridgeControl(start.Enabled, warnedOldAgent ? "游戏内为旧录制组件，请重启游戏后重新注入。" :
                    "当前未就绪或正在录制，不能开始。");
                var rate = BridgeRate(request);
                if (rate.HasValue) { readRate.Value = rate.Value; SaveSettings(); }
                await BridgeOperationAsync(() => CommandAsync("start"));
                return BridgeSnapshot(command, "开始命令已发送；以 recording 状态确认是否正在录制。");
            }
            case "stop":
                RequireBridgeControl(stop.Enabled, "当前没有正在进行的录制，或仍在执行其他操作。");
                await BridgeOperationAsync(() => CommandAsync("stop"));
                return BridgeSnapshot(command, "停止命令已发送，正在等录制文件收尾。");
            case "mark":
            {
                RequireBridgeControl(mark.Enabled, "仅录制中可以添加时间标记。");
                var text = BridgeString(request, "text") ?? BridgeString(request, "name");
                if (string.IsNullOrWhiteSpace(text)) throw new InvalidDataException("mark 命令缺少 text。");
                if (text.Length > 2048) throw new InvalidDataException("时间标记最多 2048 字。");
                await BridgeOperationAsync(() => CommandAsync("mark:" + text));
                return BridgeSnapshot(command, "时间线标记命令已发送。");
            }
            case "config":
                return ApplyBridgeConfig(request);
            case "export":
            {
                RequireBridgeControl(!diagnosticsOnly && assetsReady && !busy && !exporting
                    && latestStatus.GetValueOrDefault("recording") != "true"
                    && latestStatus.GetValueOrDefault("state") != "capturing-snapshot",
                    "录制或其他操作尚未完成，不能导出。");
                var source = BridgeString(request, "source") ?? replayPath;
                if (string.IsNullOrWhiteSpace(source)) throw new InvalidDataException("没有可导出的录制文件。");
                if (!File.Exists(source)) throw new FileNotFoundException("录制文件不存在。", source);
                if (!source.EndsWith(".fbrl", StringComparison.OrdinalIgnoreCase)
                    && !source.EndsWith(".zip", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("只支持 .fbrl 或 Flashback .zip 文件。");
                await BridgeOperationAsync(() => ExportAsync(source, false));
                if (failedExport == source) throw new IOException("导出失败，请查看日志。");
                return BridgeSnapshot(command, "导出完成。");
            }
            case "import":
            {
                RequireBridgeControl(importReplay.Enabled, "正在执行其他操作，暂时不能安装回放。");
                var path = BridgeString(request, "path");
                var target = BridgeString(request, "target") ?? flashbackDirectory.Text.Trim();
                if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
                    throw new InvalidDataException("请选择存在的回放 ZIP 文件。");
                if (string.IsNullOrWhiteSpace(target)) throw new InvalidDataException("请先选择回放游戏目录。");
                busy = true; ApplyControls();
                try
                {
                    var result = await Task.Run(() => ImportWithSkins(path, target), lifetime.Token);
                    Log($"已导入：{result.Path}；保存皮肤 {result.Skins.PlayerCount} 人，保留已有皮肤设置 {result.Skins.PreservedOverrides} 人。");
                    foreach (var warning in result.Skins.Warnings) Log("皮肤提示：" + warning);
                    flashbackDirectory.Text = target;
                    await BridgeOperationAsync(() => { SaveSettings(); return Task.CompletedTask; });
                }
                finally { busy = false; ApplyControls(); }
                return BridgeSnapshot(command, "回放与录制皮肤已安装。");
            }
            case "shutdown":
                await PrepareBridgeShutdownAsync();
                bridgeExitRequested = true;
                return BridgeSnapshot(command, "录制已收尾，后台服务即将关闭。");
            default:
                throw new InvalidDataException("不支持的 command：" + command);
        }
    }

    object ApplyBridgeConfig(JsonElement request)
    {
        RequireBridgeControl(!busy && !exporting && !scanning, "操作进行中，暂时不能修改设置。");
        var recording = latestStatus.GetValueOrDefault("recording") == "true"
            || latestStatus.GetValueOrDefault("state") == "capturing-snapshot";
        var newOutput = BridgeString(request, "output");
        var targetProvided = request.TryGetProperty("target", out _) || request.TryGetProperty("flashbackDirectory", out _);
        var newTarget = BridgeString(request, "target") ?? BridgeString(request, "flashbackDirectory");
        var rate = BridgeRate(request);
        bool? newAutoZip = null;
        if (request.TryGetProperty("autoZip", out var autoValue))
        {
            if (autoValue.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                throw new InvalidDataException("autoZip 必须为布尔值。");
            newAutoZip = autoValue.GetBoolean();
        }
        if (request.TryGetProperty("output", out _) && string.IsNullOrWhiteSpace(newOutput))
            throw new InvalidDataException("保存目录不能为空。");
        if (newOutput is not null)
        {
            newOutput = Path.GetFullPath(newOutput);
            if (stateDir is not null && !string.Equals(newOutput, Path.GetFullPath(output.Text), StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("活动会话期间不能修改保存目录；请先结束会话后重启录制服务。");
            if (!Directory.Exists(newOutput)) Directory.CreateDirectory(newOutput);
        }
        if (rate.HasValue && recording) throw new InvalidOperationException("本段录制的读取频率已经固定，停止后再修改。");
        if (targetProvided && string.IsNullOrWhiteSpace(newTarget))
            newTarget = "";
        else if (newTarget is not null && newTarget.Length > 0)
        {
            newTarget = Path.GetFullPath(newTarget);
            var mods = Path.Combine(newTarget, "mods");
            if (!Directory.Exists(mods) || !Directory.EnumerateFiles(mods, "*.jar")
                .Any(path => Path.GetFileName(path).Contains("flashback", StringComparison.OrdinalIgnoreCase)))
                throw new InvalidDataException("回放游戏目录须包含 Flashback 模组。");
        }
        var errorsBefore = bridgeErrorSequence;
        if (newOutput is not null) output.Text = newOutput;
        if (newTarget is not null) flashbackDirectory.Text = newTarget;
        if (rate.HasValue) readRate.Value = rate.Value;
        if (newAutoZip.HasValue) autoZip.Checked = newAutoZip.Value;
        SaveSettings();
        if (bridgeErrorSequence != errorsBefore) throw new IOException(bridgeLastError);
        return BridgeSnapshot("config", "录制设置已保存。");
    }

    async Task PrepareBridgeShutdownAsync()
    {
        if (bridgeShutdownPrepared) return;
        bridgeShuttingDown = true;
        processTimer.Stop(); statusTimer.Stop();
        try
        {
            // Any pending scan/status/export must settle before final commands.
            while (busy || exporting || scanning || polling) await Task.Delay(100);
            if (stateDir is not null)
            {
                await ReadStatusAsync();
                var deadline = DateTime.UtcNow.AddSeconds(75);
                while (latestStatus.GetValueOrDefault("state") == "capturing-snapshot")
                {
                    if (DateTime.UtcNow > deadline) throw new TimeoutException("等待初始世界快照收尾超时。");
                    await Task.Delay(200); await ReadStatusAsync();
                }
                if (latestStatus.GetValueOrDefault("recording") == "true")
                {
                    await CommandAsync("stop");
                    // CommandAsync reports Java CLI errors instead of throwing.
                    // A direct atomic stop is a local IPC fallback, never a game kill.
                    if (latestStatus.GetValueOrDefault("recording") == "true")
                    {
                        var command = Path.Combine(stateDir, "command.txt");
                        var temporary = command + "." + Guid.NewGuid().ToString("N") + ".tmp";
                        try { File.WriteAllText(temporary, "stop" + Environment.NewLine, new UTF8Encoding(false));
                            File.Move(temporary, command, true); }
                        finally { if (File.Exists(temporary)) File.Delete(temporary); }
                    }
                    deadline = DateTime.UtcNow.AddSeconds(75);
                    while (latestStatus.GetValueOrDefault("recording") == "true")
                    {
                        if (DateTime.UtcNow > deadline) throw new TimeoutException("已发送停止命令，但录制器未确认收尾。原始录制保留。");
                        await Task.Delay(200); await ReadStatusAsync();
                    }
                }
                await ReadStatusAsync(); // completes optional auto-export before exit
                while (busy || exporting || polling) await Task.Delay(100);
            }
            bridgeShutdownPrepared = true;
        }
        catch
        {
            bridgeShuttingDown = false;
            if (!closing) { processTimer.Start(); statusTimer.Start(); }
            throw;
        }
    }

    static string NormalizeTheme(string? theme) => theme?.Trim().ToLowerInvariant() switch
    {
        "light" or "glass" or "dark" or "blue" => theme.Trim().ToLowerInvariant(),
        _ => "dark",
    };

    IEnumerable<Control> Descendants(Control root)
    {
        foreach (Control child in root.Controls)
        {
            yield return child;
            foreach (var nested in Descendants(child)) yield return nested;
        }
    }

    void ApplyThemePalette()
    {
        var light = uiTheme == "light";
        var blue = uiTheme == "blue";
        var surface = light ? Color.FromArgb(240, 243, 247)
            : blue ? Color.FromArgb(8, 27, 58) : Color.FromArgb(22, 25, 31);
        var card = light ? Color.White
            : blue ? Color.FromArgb(12, 39, 78) : Color.FromArgb(31, 36, 44);
        var input = light ? Color.FromArgb(248, 250, 252)
            : blue ? Color.FromArgb(8, 31, 65) : Color.FromArgb(24, 29, 37);
        var text = light ? Color.FromArgb(28, 44, 61)
            : blue ? Color.FromArgb(230, 240, 255) : Color.FromArgb(220, 225, 233);
        var muted = light ? Color.FromArgb(91, 109, 128)
            : blue ? Color.FromArgb(170, 194, 224) : Color.FromArgb(163, 174, 190);
        var accentSurface = light ? Color.FromArgb(231, 240, 250)
            : blue ? Color.FromArgb(22, 64, 116) : Color.FromArgb(31, 38, 49);

        BackColor = surface;
        ForeColor = text;
        foreach (var control in Descendants(this))
        {
            control.ForeColor = text;
            control.BackColor = surface;
            if (control is GroupBox)
            {
                control.BackColor = card;
                control.ForeColor = muted;
            }
            else if (control is TextBoxBase textBox)
            {
                textBox.BackColor = input;
                textBox.ForeColor = text;
            }
            else if (control is ListBox list)
            {
                list.BackColor = input;
                list.ForeColor = text;
            }
            else if (control is Button button)
            {
                button.BackColor = card;
                button.ForeColor = text;
            }
        }
        log.BackColor = card;
        log.ForeColor = text;
        processes.BackColor = input;
        processes.ForeColor = text;
        notice.BackColor = accentSurface;
        notice.ForeColor = text;
    }

    void BuildUi()
    {
        var root = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, AutoScroll = true };
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 62));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 64));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var header = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, Padding = new Padding(20, 10, 20, 8) };
        header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 75)); header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25));
        var title = new Label { Text = "Flashback Recorder", Font = new Font("Segoe UI", 16, FontStyle.Regular), Dock = DockStyle.Fill, ForeColor = Color.FromArgb(184, 191, 202), TextAlign = ContentAlignment.MiddleLeft };
        var java = new Label { Text = javaPath.Length > 0 ? "手动注入 · Java 已就绪" : "未找到 Java 17+", TextAlign = ContentAlignment.MiddleRight, Dock = DockStyle.Fill, ForeColor = Color.FromArgb(150, 164, 183) };
        header.Controls.Add(title, 0, 0); header.Controls.Add(java, 1, 0);
        notice.Text = "正在检测游戏…"; notice.BackColor = Color.FromArgb(31, 38, 49); notice.ForeColor = Color.FromArgb(175, 188, 206); notice.Margin = new Padding(20, 4, 20, 10); notice.Dock = DockStyle.Fill;
        var split = new SplitContainer { Dock = DockStyle.Fill, Padding = new Padding(24, 0, 24, 18), BackColor = BackColor, FixedPanel = FixedPanel.None };
        split.Panel1.Padding = new Padding(0, 0, 8, 0); split.Panel2.Padding = new Padding(8, 0, 0, 0);
        split.Panel1.AutoScroll = true; split.Panel2.AutoScroll = true;
        split.SizeChanged += (_, _) =>
        {
            if (split.Width < 120) return;
            var minimum = Math.Min(420, Math.Max(240, (split.Width - split.SplitterWidth) / 2 - 20));
            if (split.Width < minimum * 2 + split.SplitterWidth)
            {
                split.Panel2MinSize = 0;
                split.Panel1MinSize = 0;
                return;
            }
            split.Panel2MinSize = 0;
            split.Panel1MinSize = 0;
            split.Panel1MinSize = minimum;
            split.Panel2MinSize = minimum;
            var maximum = split.Width - split.Panel2MinSize - split.SplitterWidth;
            split.SplitterDistance = Math.Clamp(split.Width / 2, split.Panel1MinSize, maximum);
        };
        split.Panel1.Controls.Add(BuildLeft()); split.Panel2.Controls.Add(BuildRight());
        root.Controls.Add(header, 0, 0); root.Controls.Add(notice, 0, 1); root.Controls.Add(split, 0, 2);
        Controls.Add(root);
        processes.SelectedIndexChanged += (_, _) => { selected = processes.SelectedItem as GameProcess; ApplyControls(); };
        refresh.Click += async (_, _) => await DetectProcessesAsync();
        inject.Click += async (_, _) => await InstallAsync();
        start.Click += async (_, _) => await CommandAsync("start");
        stop.Click += async (_, _) => await CommandAsync("stop");
        mark.Click += async (_, _) => { if (!string.IsNullOrWhiteSpace(marker.Text)) { await CommandAsync("mark:" + marker.Text.Trim()); marker.Clear(); } };
        export.Click += async (_, _) => { if (!string.IsNullOrEmpty(replayPath)) await ExportAsync(replayPath, false); };
        browse.Click += (_, _) => { try { using var dialog = new FolderBrowserDialog { SelectedPath = output.Text }; if (dialog.ShowDialog(this) == DialogResult.OK) { output.Text = dialog.SelectedPath; SaveSettings(); } } catch (Exception ex) { ReportError("保存目录设置失败", ex); } };
        browseFlashback.Click += (_, _) => { try { using var dialog = new FolderBrowserDialog { SelectedPath = flashbackDirectory.Text, Description = "选择装有完整版 Flashback 的游戏目录（内含 mods 文件夹）" }; if (dialog.ShowDialog(this) == DialogResult.OK) { flashbackDirectory.Text = dialog.SelectedPath; SaveSettings(); } } catch (Exception ex) { ReportError("回放目录设置失败", ex); } };
        flashbackDirectory.Validated += (_, _) => SaveSettings();
        readRate.ValueChanged += (_, _) => SaveSettings();
        hints.SetToolTip(flashbackDirectory, "填写后自动导入回放和皮肤；清空可关闭自动导入。换电脑后选择该电脑的游戏目录。");
        hints.SetToolTip(readRate, "软件每秒读取次数，20 tick = 50 ms，120 tick 约为 8.33 ms；不会改变游戏或回放的时间速度。录制开始后固定本段设置。");
        foreach (var button in new[] { inject, start, stop, mark, refresh, importReplay, open, export })
            hints.SetToolTip(button, button.Description);
        hints.SetToolTip(browse, "选择录制原始文件和导出 ZIP 的保存位置。");
        hints.SetToolTip(browseFlashback, "选择安装了完整版 Flashback 的游戏目录，目录内应有 mods 文件夹。");
        hints.SetToolTip(autoZip, "勾选后，停止录制会自动生成回放 ZIP；填写回放游戏目录后，还会自动安装回放和皮肤。");
        foreach (var field in new TextBox[] { output, flashbackDirectory, marker })
        { field.BackColor = Color.FromArgb(31, 36, 44); field.ForeColor = ForeColor; field.BorderStyle = BorderStyle.FixedSingle; }
        readRate.BackColor = Color.FromArgb(31, 36, 44); readRate.ForeColor = ForeColor;
        log.BackColor = Color.FromArgb(24, 28, 34); log.ForeColor = Color.FromArgb(165, 176, 192);
        importReplay.Click += async (_, _) => await ImportExistingReplayAsync();
        open.Click += (_, _) => { try { Directory.CreateDirectory(output.Text); var info = new ProcessStartInfo("explorer.exe") { UseShellExecute = true }; info.ArgumentList.Add(output.Text); Process.Start(info); } catch (Exception ex) { ReportError("打开目录失败", ex); } };
    }

    Control BuildLeft()
    {
        var box = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1, MinimumSize = new Size(0, 630), AutoScroll = true, Padding = new Padding(0, 0, 0, 2) }; box.RowStyles.Add(new RowStyle(SizeType.Absolute, 160)); box.RowStyles.Add(new RowStyle(SizeType.Absolute, 220)); box.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        box.Controls.Add(Group("游戏进程", processes, refresh, inject), 0, 0);
        box.Controls.Add(Group("录制控制", BuildActions(), null, null), 0, 1);
        var config = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 5, Padding = new Padding(10), AutoScroll = true }; config.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 92)); config.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); config.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 68));
        foreach (var height in new[] { 32, 32, 32, 28, 54 }) config.RowStyles.Add(new RowStyle(SizeType.Absolute, height));
        config.Controls.Add(new Label { Text = "保存目录", Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 0, 0); config.Controls.Add(output, 1, 0); config.Controls.Add(browse, 2, 0);
        config.Controls.Add(new Label { Text = "回放游戏目录", Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 0, 1); config.Controls.Add(flashbackDirectory, 1, 1); config.Controls.Add(browseFlashback, 2, 1);
        config.Controls.Add(new Label { Text = "读取频率", Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 0, 2); config.Controls.Add(readRate, 1, 2);
        config.Controls.Add(new Label { Text = "tick / 秒", Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 2, 2);
        config.Controls.Add(autoZip, 0, 3); config.SetColumnSpan(autoZip, 3);
        config.Controls.Add(importReplay, 0, 4); config.SetColumnSpan(importReplay, 3);
        box.Controls.Add(Group("录制配置", config, null, null), 0, 2); return box;
    }
    Control BuildActions()
    {
        var p = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 3, Padding = new Padding(10), AutoScroll = true }; p.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50)); p.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50)); p.RowStyles.Add(new RowStyle(SizeType.Percent, 34)); p.RowStyles.Add(new RowStyle(SizeType.Percent, 33)); p.RowStyles.Add(new RowStyle(SizeType.Percent, 33)); p.Controls.Add(start, 0, 0); p.Controls.Add(stop, 1, 0); p.Controls.Add(marker, 0, 1); p.Controls.Add(mark, 1, 1); p.Controls.Add(open, 0, 2); p.Controls.Add(export, 1, 2); return p;
    }
    GroupBox Group(string title, Control content, Control? first, Control? second)
    {
        var g = new GroupBox { Text = title, Dock = DockStyle.Fill, ForeColor = Color.FromArgb(163, 174, 190), Padding = new Padding(8) };
        var p = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = first is null ? 1 : 2, ColumnCount = 2 };
        p.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50)); p.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        p.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        if (first is not null) p.RowStyles.Add(new RowStyle(SizeType.Absolute, 56));
        p.Controls.Add(content, 0, 0); p.SetColumnSpan(content, 2);
        if (first is not null) { p.Controls.Add(first, 0, 1); p.Controls.Add(second!, 1, 1); }
        g.Controls.Add(p); return g;
    }
    Control BuildRight()
    {
        var panel = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1, AutoScroll = true }; panel.RowStyles.Add(new RowStyle(SizeType.Absolute, 204)); panel.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var status = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 5, Padding = new Padding(10) }; status.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 72)); status.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); status.Controls.Add(new Label { Text = "状态", Dock = DockStyle.Fill }, 0, 0); status.Controls.Add(state, 1, 0); status.Controls.Add(new Label { Text = "PID", Dock = DockStyle.Fill }, 0, 1); status.Controls.Add(pidLabel, 1, 1); status.Controls.Add(new Label { Text = "样本", Dock = DockStyle.Fill }, 0, 2); status.Controls.Add(samples, 1, 2); status.Controls.Add(new Label { Text = "数据包", Dock = DockStyle.Fill }, 0, 3); status.Controls.Add(packets, 1, 3); status.Controls.Add(new Label { Text = "录制文件", Dock = DockStyle.Fill }, 0, 4); status.Controls.Add(replay, 1, 4); panel.Controls.Add(Group("当前会话", status, null, null), 0, 0); panel.Controls.Add(Group("运行日志", log, null, null), 0, 1); return panel;
    }

    static string FindJava()
    {
        var candidates = new List<string>(); var javaHome = Environment.GetEnvironmentVariable("JAVA_HOME"); if (!string.IsNullOrWhiteSpace(javaHome)) candidates.Add(Path.Combine(javaHome, "bin", "java.exe")); candidates.AddRange(new[] { @"C:\Program Files\Java\jdk-21.0.10\bin\java.exe", @"C:\Program Files\Java\jdk-21\bin\java.exe", @"C:\Program Files\Java\jdk-17\bin\java.exe" });
        foreach (var c in candidates) if (File.Exists(c)) return c;
        foreach (var directory in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
        {
            try { var candidate = Path.Combine(directory.Trim('"'), "java.exe"); if (File.Exists(candidate)) return candidate; }
            catch (ArgumentException) { }
        }
        return "";
    }
    async Task DetectProcessesAsync()
    {
        if (closing || scanning) return;
        scanning = true;
        ApplyControls();
        try
        {
            var found = await GameDiscovery.FindAsync(lifetime.Token);
            if (closing) return;
            var currentIdentity = selected?.Identity;
            processes.BeginUpdate();
            try
            {
                processes.Items.Clear();
                foreach (var item in found) processes.Items.Add(item);
                for (var i = 0; i < processes.Items.Count; i++)
                    if ((processes.Items[i] as GameProcess)?.Identity == currentIdentity)
                    { processes.SelectedIndex = i; break; }
                if (processes.SelectedIndex < 0 && processes.Items.Count > 0)
                    processes.SelectedIndex = 0;
            }
            finally { processes.EndUpdate(); }
            selected = processes.SelectedItem as GameProcess;
            var supportedNetease = found.Where(p => p.Supported && p.IsNetease).ToList();
            notice.Text = diagnosticsOnly
                ? $"诊断模式 · 检测到 {found.Count} 个游戏进程；不会注入或录制。"
                : found.Count == 0 ? "未检测到游戏进程。启动网易 1.20.1 后会重新检测。"
                : supportedNetease.Count == 1 ? $"检测到网易 1.20.1 PID {supportedNetease[0].Pid}；回放客户端 1.21.11 不会被注入。"
                : supportedNetease.Count > 1 ? "检测到多个网易 1.20.1 进程，请选定目标后点击注入。"
                : "未检测到受支持的网易 1.20.1 进程。其他版本仅显示，不会自动注入。";
            // Selection changes do not invalidate the attached JVM identity.
            if (!busy && !exporting && attachedProcessKey is not null &&
                !found.Any(p => p.Identity == attachedProcessKey) &&
                !AttachedProcessStillExists(attachedProcessKey)) ResetSessionForProcess();
            if (diagnosticsOnly || busy || exporting || stateDir is not null || !assetsReady) return;
            if (supportedNetease.Count != 1) return;
            var preferred = supportedNetease[0];
            // Reading an existing identity-matched session does not inject anything.
            // Loading an agent is reserved for the explicit Inject button.
            await TryAdoptRecentSessionAsync(preferred);
        }
        catch (OperationCanceledException) when (closing) { }
        catch (Exception ex) { ReportError("检测游戏进程失败", ex); }
        finally { scanning = false; ApplyControls(); }
    }

    async Task<bool> TryAdoptRecentSessionAsync(GameProcess process)
    {
        var baseDir = Path.Combine(output.Text.Trim(), ".flashback-runtime");
        try
        {
            var matching = await Task.Run(() =>
            {
                if (!Directory.Exists(baseDir)) return new List<string>();
                var matches = new List<string>();
                foreach (var directory in Directory.EnumerateDirectories(baseDir, "session-*"))
                {
                    var identityFile = Path.Combine(directory, "process.identity");
                    var statusFile = Path.Combine(directory, "status.properties");
                    try
                    {
                        if (!File.Exists(identityFile) || !File.Exists(statusFile) ||
                            File.ReadAllText(identityFile) != process.Identity) continue;
                        var props = StatusFile.Read(statusFile);
                        if (props.GetValueOrDefault("attached") == "true" &&
                            props.GetValueOrDefault("pid") == process.Pid.ToString() &&
                            IsReusableSessionState(props.GetValueOrDefault("state")))
                            matches.Add(directory);
                    }
                    catch (IOException) { }
                    catch (UnauthorizedAccessException) { }
                }
                return matches;
            }, lifetime.Token);
            if (closing || busy || exporting || matching.Count != 1 || stateDir is not null) return false;
            stateDir = matching[0];
            attachedProcessKey = process.Identity;
            warnedOldAgent = false;
            pidLabel.Text = process.Pid.ToString();
            Log("已复用身份匹配的活动录制会话：" + stateDir);
            await ReadStatusAsync();
            return true;
        }
        catch (OperationCanceledException) when (closing) { return false; }
        catch (Exception ex) { ReportError("读取已有会话失败", ex); return false; }
    }

    async Task InstallAsync(GameProcess? targetOverride = null)
    {
        var target = targetOverride ?? selected;
        if (diagnosticsOnly || closing || busy || exporting || scanning || target is null) return;
        if (!target.Supported) { Log("只支持附加 Minecraft 1.20.1，选中的版本不能注入。"); return; }
        if (stateDir is not null) { Log("当前已有活动会话，请直接开始或停止录制。"); return; }
        if (!assetsReady || javaPath.Length == 0) { Log("运行时组件或 Java 尚未准备好，暂时不能注入。"); return; }
        busy = true;
        ApplyControls();
        try
        {
            var live = await GameDiscovery.FindAsync(lifetime.Token);
            if (closing || stateDir is not null) return;
            if (!live.Any(p => p.Identity == target.Identity && p.Supported))
                throw new InvalidOperationException("目标游戏已退出或重启，请重新选择进程。");
            var root = output.Text.Trim();
            if (root.Length == 0) throw new IOException("请选择录制保存目录。");
            var directory = Path.Combine(Path.GetFullPath(root), ".flashback-runtime", "session-" + DateTime.Now.ToString("yyyyMMdd-HHmmss-fff"));
            await Task.Run(() =>
            {
                Directory.CreateDirectory(directory);
                File.WriteAllText(Path.Combine(directory, "process.identity"), target.Identity);
            }, lifetime.Token);
            Log($"正在附加到 Minecraft 1.20.1 PID {target.Pid}…");
            var result = await RunJavaAsync("install", target.Pid.ToString(), RuntimeAssets.Agent, directory);
            if (!result.Ok) throw new IOException(result.Text);
            stateDir = directory;
            attachedProcessKey = target.Identity;
            replayPath = null;
            latestStatus.Clear();
            warnedOldAgent = false;
            failedExport = null;
            pidLabel.Text = target.Pid.ToString();
            state.Text = "等待录制器状态";
            Log("Attach 请求已完成，正在等待录制器状态。");
            SaveSettings();
        }
        catch (OperationCanceledException) when (closing) { }
        catch (Exception ex) { ReportError("注入失败", ex); }
        finally { busy = false; ApplyControls(); }
        await ReadStatusAsync();
    }

    void ResetSessionForProcess()
    {
        Log("原会话的游戏进程已退出；新进程将重新检测，已有录制文件保留。");
        stateDir = null;
        attachedProcessKey = null;
        replayPath = null;
        latestStatus.Clear();
        warnedOldAgent = false;
        failedExport = null;
        pidLabel.Text = "-";
        samples.Text = packets.Text = "0";
        replay.Text = "录制尚未开始";
        state.Text = "等待附加";
        ApplyControls();
    }

    async Task CommandAsync(string command)
    {
        if (diagnosticsOnly || closing || busy || exporting || stateDir is null) return;
        if (command == "start" && (warnedOldAgent || latestStatus.GetValueOrDefault("recording") == "true" ||
            !IsStartReadyState(latestStatus.GetValueOrDefault("state")))) return;
        var directory = stateDir;
        busy = true;
        ApplyControls();
        try
        {
            var wireCommand = command == "start" ? "start:" + ((int)readRate.Value).ToString(System.Globalization.CultureInfo.InvariantCulture) : command;
            var result = await RunJavaAsync("command", directory, wireCommand);
            if (!result.Ok) throw new IOException(result.Text);
            Log(command == "start" ? $"已发送开始录制命令，读取频率 {readRate.Value} tick / 秒。" : command == "stop" ? "已发送停止录制命令，等待文件写入完成。" : "已写入时间线标记。");
        }
        catch (OperationCanceledException) when (closing) { }
        catch (Exception ex) { ReportError("录制命令失败", ex); }
        finally { busy = false; ApplyControls(); }
        await ReadStatusAsync();
    }

    async Task<(bool Ok, string Text)> RunJavaAsync(params string[] args)
    {
        var info = new ProcessStartInfo(javaPath);
        info.ArgumentList.Add("--add-modules");
        info.ArgumentList.Add("jdk.attach");
        info.ArgumentList.Add("-jar");
        info.ArgumentList.Add(RuntimeAssets.Cli);
        foreach (var argument in args) info.ArgumentList.Add(argument);
        var result = await ChildProcess.RunAsync(info, TimeSpan.FromSeconds(25), lifetime.Token);
        return (result.ExitCode == 0, result.Output);
    }

    async Task ReadStatusAsync()
    {
        if (diagnosticsOnly || closing || polling || stateDir is null) return;
        polling = true;
        var directory = stateDir;
        try
        {
            var file = Path.Combine(directory, "status.properties");
            if (!File.Exists(file)) return;
            var props = await Task.Run(() => StatusFile.Read(file), lifetime.Token);
            if (closing || stateDir != directory) return;
            var expectedPid = attachedProcessKey?.Split(':', 2)[0];
            if (props.TryGetValue("pid", out var actualPid) && actualPid != expectedPid)
                throw new InvalidDataException("状态文件的进程身份不匹配，已暂停读取。");
            latestStatus = props;
            if (!props.ContainsKey("state") && props.GetValueOrDefault("attached") == "true")
            {
                state.Text = "当前 JVM 已加载其他录制器实例";
                if (!warnedOldAgent)
                {
                    Log("当前游戏已有旧录制器实例；无法替换正在加载的组件，需要下次重启游戏后使用新版。");
                    warnedOldAgent = true;
                }
                ApplyControls();
                return;
            }
            var recording = props.GetValueOrDefault("recording") == "true";
            var oldAgent = !int.TryParse(props.GetValueOrDefault("agentVersion"), out var agentVersion) || agentVersion < RuntimeAssets.RequiredAgentVersion;
            if (oldAgent && !warnedOldAgent)
                Log("游戏中仍是旧录制组件。已保存的回放可继续导出；请重启网易游戏一次，再手动注入新版以使用修复后的交互声音与裂纹采集。");
            warnedOldAgent = oldAgent;
            var currentReplay = props.GetValueOrDefault("replay", "");
            if (!string.Equals(currentReplay, replayPath, StringComparison.OrdinalIgnoreCase))
            {
                replayPath = currentReplay; failedExport = null;
                if (!string.IsNullOrEmpty(currentReplay))
                {
                    reportedErrors.Clear();
                    lastLogMessage = null;
                }
            }
            if (!exporting)
                state.Text = recording ? $"● 录制中 · 读取 {props.GetValueOrDefault("samplingRate", "20")} tick / 秒" : failedExport == replayPath
                    ? "导出未完成；可点击“导出 / 重试”" : oldAgent ? "请重启游戏一次，以启用新版功能" : props.GetValueOrDefault("state") switch
                    {
                        "ready" or "attached" => "已注入，等待录制",
                        "record-error" when props.GetValueOrDefault("error", "").Contains("Required world snapshot failed", StringComparison.Ordinal)
                            && Regex.IsMatch(props.GetValueOrDefault("error", ""), @"\bchunks=captured:0(?:\D|$)")
                            => "地图区块尚未就绪，请进入地图后重试",
                        "record-error" or "error" => "录制异常，请查看日志后重试",
                        "native-finish-error" => "原生回放保存异常，请查看下方日志",
                        "capturing-snapshot" => "正在保存初始世界，请稍候…",
                        "waiting-for-minecraft" => "已注入，等待 Minecraft 客户端就绪",
                        "stopped" => "录制组件已停止",
                        _ => "已连接，正在读取状态"
                    };
            samples.Text = props.GetValueOrDefault("samples", "0") + (props.ContainsKey("recordedSkins")
                ? $" · 皮肤 {props["recordedSkins"]} 人 · 皮肤异常 {props.GetValueOrDefault("skinCaptureErrors", "0")}" : "");
            packets.Text = $"{props.GetValueOrDefault("packets", "0")} · 丢失 {props.GetValueOrDefault("packetDrops", "0")} · 异常 {props.GetValueOrDefault("packetErrors", "0")} · 跳过 {props.GetValueOrDefault("packetSkippedCustomPayload", "0")}";
            replay.Text = string.IsNullOrEmpty(replayPath) ? "录制尚未开始" : replayPath;
            foreach (var key in new[] { "packetError", "error", "captureError", "chunkSnapshotError", "entitySnapshotError", "entityPacketError", "localStateError", "sampleError", "commandError", "worldSnapshotError", "stopWarning", "skinError" })
                if (props.TryGetValue(key, out var problem) && !string.IsNullOrWhiteSpace(problem))
                    ReportError("录制器报告 " + key, new IOException(problem));
            if (props.TryGetValue("localEffectsHook", out var effectsHook) && effectsHook.StartsWith("error:", StringComparison.Ordinal))
                ReportError("本地交互声音采集未就绪；服务器数据包仍保留", new IOException(effectsHook));
            if (int.TryParse(props.GetValueOrDefault("entitySnapshotErrors"), out var entitySnapshotErrors) && entitySnapshotErrors > 0)
                ReportError("初始实体采集不完整", new IOException("部分实体状态未能保存，请查看本段录制目录中的 status.properties。"));
            ApplyControls();
            if (!oldAgent && !recording && !busy && !exporting && autoZip.Checked &&
                props.GetValueOrDefault("state") == "ready" &&
                !string.IsNullOrEmpty(replayPath) && File.Exists(replayPath))
                await ExportAsync(replayPath, true);
        }
        catch (OperationCanceledException) when (closing) { }
        catch (IOException ex) when (StatusFile.IsSharingViolation(ex))
        {
            // The Java agent is replacing its status file. Preserve the last
            // known state and read the next timer tick; never open a JIT dialog.
        }
        catch (Exception ex) { ReportError("读取录制状态失败", ex); }
        finally { polling = false; ApplyControls(); }
    }

    async Task ExportAsync(string source, bool automatic)
    {
        if (diagnosticsOnly || closing || busy || exporting || latestStatus.GetValueOrDefault("recording") == "true") return;
        if (automatic && !attemptedExports.Add(source)) return;
        attemptedExports.Add(source);
        if (!File.Exists(source)) { ReportError("导出失败", new FileNotFoundException("录制文件不存在。", source)); return; }
        if (source.EndsWith(".zip", StringComparison.OrdinalIgnoreCase))
        {
            failedExport = null;
            Log("原生 Flashback 已生成 ZIP：" + source);
            return;
        }
        if (!source.EndsWith(".fbrl", StringComparison.OrdinalIgnoreCase))
        { Log("此录制文件不支持导出，请查看文件格式：" + source); return; }
        exporting = true;
        state.Text = "正在后台转换并导出…";
        ApplyControls();
        Log(automatic ? "录制文件已关闭，正在后台导出。" : "开始手动导出 / 重试。");
        try
        {
            var zip = await Task.Run(() =>
            {
                var adapted = FlashbackZip.RunProtocolAdapter(source, javaPath, RuntimeAssets.ProtocolAdapter);
                if (!adapted.Ok) throw new InvalidDataException("1.20.1 → 1.21.11 协议转换失败：" + adapted.Text);
                return FlashbackZip.Convert(source);
            }, lifetime.Token);
            if (closing) return;
            failedExport = null;
            state.Text = "导出完成";
            Log("已导出 Flashback 1.21.11 文件：" + zip);
            var targetDirectory = flashbackDirectory.Text.Trim();
            if (targetDirectory.Length > 0)
            {
                try
                {
                    var imported = await Task.Run(() => ImportWithSkins(zip, targetDirectory), lifetime.Token);
                    Log($"已导入回放：{imported.Path}；皮肤 {imported.Skins.PlayerCount} 人，保留已有皮肤设置 {imported.Skins.PreservedOverrides} 人。");
                    foreach (var warning in imported.Skins.Warnings) Log("皮肤提示：" + warning);
                }
                catch (Exception ex) { ReportError("回放已导出，但自动安装未完成；可使用“安装回放到 Flashback”重试", ex); }
            }
        }
        catch (OperationCanceledException) when (closing) { }
        catch (Exception ex)
        {
            failedExport = source;
            ReportError("导出失败", ex);
            state.Text = "导出未完成；可点击“导出 / 重试”";
        }
        finally { exporting = false; ApplyControls(); }
    }

    static bool IsStartReadyState(string? status) => status is
        "ready" or "attached" or "record-error" or "error" or "native-finish-error";

    static bool IsReusableSessionState(string? status) => IsStartReadyState(status) || status is
        "recording" or "capturing-snapshot" or "waiting-for-minecraft";

    void ApplyControls()
    {
        if (closing || IsDisposed) return;
        var operation = busy || exporting;
        var recording = latestStatus.GetValueOrDefault("recording") == "true";
        var sessionState = latestStatus.GetValueOrDefault("state");
        var sessionReady = stateDir is not null && IsStartReadyState(sessionState);
        var capturingSnapshot = stateDir is not null && sessionState == "capturing-snapshot";
        var waitingForMinecraft = stateDir is not null && sessionState == "waiting-for-minecraft";
        inject.Enabled = !diagnosticsOnly && assetsReady && !operation && !scanning && stateDir is null && selected?.Supported == true;
        start.Enabled = !diagnosticsOnly && !operation && !recording && sessionReady && !warnedOldAgent;
        stop.Enabled = mark.Enabled = !diagnosticsOnly && !operation && recording;
        export.Enabled = !diagnosticsOnly && assetsReady && !operation && !recording && !string.IsNullOrEmpty(replayPath) && File.Exists(replayPath);
        refresh.Enabled = !scanning && !operation;
        processes.Enabled = !operation;
        browse.Enabled = output.Enabled = !operation && stateDir is null;
        browseFlashback.Enabled = flashbackDirectory.Enabled = !operation;
        importReplay.Enabled = !diagnosticsOnly && !operation;
        marker.Enabled = !operation && recording;
        autoZip.Enabled = !operation;
        readRate.Enabled = !operation && !recording && !capturingSnapshot;
        open.Enabled = Directory.Exists(output.Text);
        inject.Text = stateDir is null ? "注入选中游戏" : "已注入当前游戏";
        inject.Description = stateDir is null ? "手动加载录制组件" : recording ? "已连接，正在录制" : "无需重复注入";
        start.Description = recording ? "本段录制正在进行" : capturingSnapshot ? "正在保存初始世界，请稍候" : waitingForMinecraft ? "等待客户端就绪后可用" : stateDir is null ? "请先注入选中游戏" : "从现在开始保存游戏";
        stop.Description = recording ? "结束本段并保存文件" : "录制开始后可用";
        export.Description = recording ? "停止录制后可用" : "重新转换最近一次录制";
        foreach (var button in new[] { inject, start, stop, export }) hints.SetToolTip(button, button.Description);
        notice.Text = diagnosticsOnly ? $"诊断模式 · 检测到 {processes.Items.Count} 个游戏进程；不会注入或录制。"
            : exporting ? "正在生成回放文件，请等待导出完成。"
            : busy ? "正在执行操作，请稍候…"
            : recording ? $"已注入 PID {pidLabel.Text} · 正在录制，读取 {latestStatus.GetValueOrDefault("samplingRate", "20")} tick / 秒。点击“停止录制”保存本段。"
            : stateDir is not null && warnedOldAgent ? "当前游戏仍加载旧组件；请重启游戏，再手动点击“注入选中游戏”。"
            : capturingSnapshot ? "正在保存初始世界快照，请等待完成；本段读取频率已固定，无需重复开始或注入。"
            : waitingForMinecraft ? "已注入，正在等待 Minecraft 客户端初始化；就绪后可开始录制，无需重复注入。"
            : sessionReady ? $"已注入 PID {pidLabel.Text} · 点击“开始录制”即可录制，无需重复注入。"
            : stateDir is not null ? "注入请求已发送，正在等待录制组件就绪。"
            : selected?.Supported == true ? $"手动注入模式 · 已选中 PID {selected.Pid}。点击“注入选中游戏”后才能录制。"
            : processes.Items.Count > 0 ? "手动注入模式 · 请选择 Minecraft 1.20.1；1.21.11 客户端用于播放回放。"
            : "手动注入模式 · 未检测到游戏。启动 Minecraft 1.20.1 后会自动检测，注入需要手动点击。";
    }

    async Task ImportExistingReplayAsync()
    {
        if (diagnosticsOnly || closing || busy || exporting) return;
        using var dialog = new OpenFileDialog { Filter = "Flashback 回放 (*.zip)|*.zip", Title = "选择要安装的回放 ZIP（请先在游戏中退出该回放）" };
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        var target = flashbackDirectory.Text.Trim();
        if (target.Length == 0) { Log("请先选择装有完整版 Flashback 的回放游戏目录。"); return; }
        busy = true; ApplyControls();
        try
        {
            var result = await Task.Run(() => ImportWithSkins(dialog.FileName, target), lifetime.Token);
            Log($"已导入：{result.Path}；保存皮肤 {result.Skins.PlayerCount} 人，保留已有皮肤设置 {result.Skins.PreservedOverrides} 人。");
            foreach (var warning in result.Skins.Warnings) Log("皮肤提示：" + warning);
            SaveSettings();
        }
        catch (OperationCanceledException) when (closing) { }
        catch (Exception ex) { ReportError("导入回放与皮肤失败", ex); }
        finally { busy = false; ApplyControls(); }
    }

    internal static (string Path, RecordedSkins.InstallResult Skins) ImportWithSkins(string zip, string gameDir)
    {
        gameDir = Path.GetFullPath(gameDir);
        var mods = Path.Combine(gameDir, "mods");
        if (!Directory.Exists(mods) || !Directory.EnumerateFiles(mods, "*.jar")
            .Any(path => Path.GetFileName(path).Contains("flashback", StringComparison.OrdinalIgnoreCase)))
            throw new IOException("所选目录没有找到 Flashback 模组。请选择包含 mods 文件夹的游戏目录。");
        using var archive = ZipFile.OpenRead(zip);
        using var input = (archive.GetEntry("metadata.json") ?? throw new InvalidDataException("回放缺少 metadata.json。")).Open();
        using var metadata = JsonDocument.Parse(input);
        if (!Guid.TryParse(metadata.RootElement.GetProperty("uuid").GetString(), out var replayId))
            throw new InvalidDataException("回放 UUID 无效。");
        _ = RecordedSkins.CountInReplay(zip); // Validate the bundle before copying or changing editor state.
        var destinationDir = Path.Combine(gameDir, "flashback", "replays");
        Directory.CreateDirectory(destinationDir);
        var destination = Path.Combine(destinationDir, Path.GetFileName(zip));
        if (!string.Equals(Path.GetFullPath(zip), destination, StringComparison.OrdinalIgnoreCase))
        {
            if (File.Exists(destination))
            {
                using var original = File.OpenRead(zip);
                using var existing = File.OpenRead(destination);
                if (System.Security.Cryptography.SHA256.HashData(original).AsSpan().SequenceEqual(System.Security.Cryptography.SHA256.HashData(existing)))
                    return (destination, RecordedSkins.InstallFromReplay(zip, gameDir));
                destination = Path.Combine(destinationDir, Path.GetFileNameWithoutExtension(zip) + "-" + replayId.ToString("N") + ".zip");
            }
            // Never replace a different replay that the editor may have open.
            if (File.Exists(destination))
            {
                using var original = File.OpenRead(zip);
                using var existing = File.OpenRead(destination);
                if (!System.Security.Cryptography.SHA256.HashData(original).AsSpan().SequenceEqual(System.Security.Cryptography.SHA256.HashData(existing)))
                    throw new IOException("目标目录已有同一回放 ID 的不同文件，未覆盖。请保留原文件并更换导入目录。");
            }
            else
            {
                var temporary = destination + "." + Guid.NewGuid().ToString("N") + ".tmp";
                try { File.Copy(zip, temporary, overwrite: false); File.Move(temporary, destination, overwrite: false); }
                finally { if (File.Exists(temporary)) File.Delete(temporary); }
            }
        }
        try { return (destination, RecordedSkins.InstallFromReplay(zip, gameDir)); }
        catch (Exception ex) { throw new IOException("回放文件已保留在目标目录，但皮肤安装未完成：" + destination + "。" + ex.Message, ex); }
    }

    static bool AttachedProcessStillExists(string identity)
    {
        var parts = identity.Split(':', 2);
        if (parts.Length != 2 || !int.TryParse(parts[0], out var pid) || !long.TryParse(parts[1], out var started)) return true;
        try
        {
            using var process = Process.GetProcessById(pid);
            if (process.HasExited) return false;
            // CIM serializes creation time at a lower precision than FILETIME.
            return Math.Abs(process.StartTime.ToUniversalTime().Ticks - started) < TimeSpan.TicksPerMillisecond;
        }
        catch (ArgumentException) { return false; }
        catch (InvalidOperationException) { return false; }
        catch (System.ComponentModel.Win32Exception) { return true; }
    }

    internal void ReportError(string context, Exception error)
    {
        if (closing || IsDisposed) { Diagnostics.Write(context, error); return; }
        var key = context + ":" + error.GetType().Name + ":" + error.Message;
        bridgeErrorSequence++;
        bridgeLastError = context + "：" + error.Message;
        if (reportedErrors.Count > 512) reportedErrors.Clear();
        if (!reportedErrors.Add(key)) return;
        Diagnostics.Write(context, error);
        if (context.StartsWith("界面", StringComparison.Ordinal)) state.Text = "界面操作失败；详细信息已写入日志";
        Log(context + "：" + error.Message + "（详细日志已保存）", false);
    }

    void LoadSettings()
    {
        output.Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyVideos), "FlashbackRecorder");
        var knownTarget = @"D:\PCL\.minecraft\versions\ZJ HUB";
        if (Directory.Exists(Path.Combine(knownTarget, "mods"))) flashbackDirectory.Text = knownTarget;
        try
        {
            if (!File.Exists(settingsPath)) return;
            using var json = JsonDocument.Parse(File.ReadAllText(settingsPath));
            if (json.RootElement.TryGetProperty("output", out var value) && value.ValueKind == JsonValueKind.String)
                output.Text = value.GetString() ?? output.Text;
            if (json.RootElement.TryGetProperty("flashbackDirectory", out var target) && target.ValueKind == JsonValueKind.String)
                flashbackDirectory.Text = target.GetString() ?? "";
            if (json.RootElement.TryGetProperty("readRate", out var rate) && rate.ValueKind == JsonValueKind.Number && rate.TryGetInt32(out var valueRate))
                readRate.Value = Math.Clamp(valueRate, 20, 120);
            if (json.RootElement.TryGetProperty("autoZip", out var zip) &&
                (zip.ValueKind is JsonValueKind.True or JsonValueKind.False))
                autoZip.Checked = zip.GetBoolean();
        }
        catch (Exception ex) { ReportError("读取设置失败，已使用默认目录", ex); }
    }
    void SaveSettings()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(settingsPath)!);
            var temporary = settingsPath + "." + Environment.ProcessId + ".tmp";
            File.WriteAllText(temporary, JsonSerializer.Serialize(new { output = output.Text,
                flashbackDirectory = flashbackDirectory.Text, readRate = (int)readRate.Value,
                autoZip = autoZip.Checked }));
            File.Move(temporary, settingsPath, overwrite: true);
        }
        catch (Exception ex) { ReportError("保存设置失败", ex); }
    }
    void Log(string text, bool persist = true)
    {
        if (IsDisposed || closing || lastLogMessage == text) return;
        lastLogMessage = text;
        if (persist) Diagnostics.Write(text);
        const int maximumLength = 64000;
        if (text.Length > 8000) text = text[..8000] + "…（完整内容见诊断日志）";
        if (log.TextLength + text.Length + 20 > maximumLength)
        {
            var remove = Math.Min(log.TextLength, Math.Max(16000, log.TextLength + text.Length + 20 - maximumLength));
            log.Text = log.Text[remove..];
        }
        var now = DateTime.Now;
        log.AppendText($"[{now:HH:mm:ss}] {text}\r\n");
        bridgeLogSequence++;
        bridgeLogs.Add((bridgeLogSequence, now.ToString("HH:mm:ss"), text));
        if (bridgeLogs.Count > 512) bridgeLogs.RemoveRange(0, bridgeLogs.Count - 512);
        log.SelectionStart = log.TextLength;
        log.ScrollToCaret();
    }
}

internal static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        var diagnosticsOnly = args.Contains("--diagnostics-only", StringComparer.OrdinalIgnoreCase);
        var bridgeStdio = args.Contains("--bridge-stdio", StringComparer.OrdinalIgnoreCase);
        var theme = "dark";
        for (var index = 0; index < args.Length; index++)
        {
            if (args[index].StartsWith("--theme=", StringComparison.OrdinalIgnoreCase))
            {
                theme = args[index][8..];
                break;
            }
            if (string.Equals(args[index], "--theme", StringComparison.OrdinalIgnoreCase) && index + 1 < args.Length)
            {
                theme = args[index + 1];
                break;
            }
        }
        RecorderForm? form = null;
        Mutex? instanceMutex = null;
        var ownsMutex = false;
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
        Application.ThreadException += (_, e) =>
        {
            if (form is { IsDisposed: false }) form.ReportError("界面操作出现错误，请查看日志后重试", e.Exception);
            else Diagnostics.Write("界面线程异常", e.Exception);
        };
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            Diagnostics.Write("未处理的后台异常", e.ExceptionObject as Exception);
        TaskScheduler.UnobservedTaskException += (_, e) =>
        {
            Diagnostics.Write("未观察的后台任务异常", e.Exception);
            e.SetObserved();
        };
        try
        {
            if (!diagnosticsOnly)
            {
                instanceMutex = new Mutex(false, @"Local\FlashbackRecorder");
                try { ownsMutex = instanceMutex.WaitOne(0); }
                catch (AbandonedMutexException) { ownsMutex = true; }
                if (!ownsMutex)
                {
                    if (bridgeStdio)
                    {
                        Console.WriteLine(JsonSerializer.Serialize(new Dictionary<string, object?>
                        {
                            ["ok"] = false,
                            ["error"] = "录制器已经运行，请先关闭现有窗口。",
                        }));
                        Console.Out.Flush();
                    }
                    else
                        MessageBox.Show("录制器已经运行，请使用现有窗口。", "Flashback Recorder", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return;
                }
            }
            ApplicationConfiguration.Initialize();
            Diagnostics.Write(diagnosticsOnly ? "启动诊断模式。" : "启动录制器。");
            form = new RecorderForm(diagnosticsOnly, theme: theme, bridgeStdio: bridgeStdio);
            Application.Run(form);
        }
        catch (Exception ex)
        {
            Diagnostics.Write("录制器启动失败", ex);
            if (bridgeStdio)
            {
                Console.WriteLine(JsonSerializer.Serialize(new Dictionary<string, object?>
                {
                    ["ok"] = false,
                    ["error"] = "录制器启动失败：" + ex.Message,
                }));
                Console.Out.Flush();
            }
            else
                MessageBox.Show("录制器启动失败：" + ex.Message + "\n\n诊断日志：" + Diagnostics.FilePath,
                    "Flashback Recorder 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            if (ownsMutex) instanceMutex?.ReleaseMutex();
            instanceMutex?.Dispose();
        }
    }
}
