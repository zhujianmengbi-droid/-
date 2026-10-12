using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace FlashbackRecorder;

internal static class Diagnostics
{
    static readonly object Gate = new();
    public static string DirectoryPath => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FlashbackRecorder", "logs");
    public static string FilePath => Path.Combine(DirectoryPath, "recorder-" + DateTime.Now.ToString("yyyyMMdd") + ".log");
    public static void Write(string message, Exception? error = null)
    {
        try
        {
            lock (Gate)
            {
                Directory.CreateDirectory(DirectoryPath);
                var path = FilePath;
                if (File.Exists(path) && new FileInfo(path).Length > 4 * 1024 * 1024)
                    File.Move(path, path + ".previous", true);
                File.AppendAllText(path, $"[{DateTimeOffset.Now:O}] {message}{(error is null ? "" : Environment.NewLine + error)}{Environment.NewLine}", Encoding.UTF8);
            }
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}

internal static class ChildProcess
{
    public sealed record Result(int ExitCode, string Output);
    public static async Task<Result> RunAsync(ProcessStartInfo info, TimeSpan timeout, CancellationToken cancellation = default)
    {
        info.RedirectStandardOutput = info.RedirectStandardError = true;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        using var child = Process.Start(info) ?? throw new IOException("无法启动辅助进程。");
        var stdout = child.StandardOutput.ReadToEndAsync();
        var stderr = child.StandardError.ReadToEndAsync();
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        deadline.CancelAfter(timeout);
        try { await child.WaitForExitAsync(deadline.Token).ConfigureAwait(false); }
        catch (OperationCanceledException)
        {
            try { if (!child.HasExited) child.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
            // Only the helper created by this method is terminated. No game is stopped.
            if (cancellation.IsCancellationRequested) throw;
            throw new TimeoutException($"辅助进程超过 {timeout.TotalSeconds:0} 秒未完成。");
        }
        return new Result(child.ExitCode, ((await stdout.ConfigureAwait(false)) + "\n" + (await stderr.ConfigureAwait(false))).Trim());
    }
}

internal static class StatusFile
{
    // Atomic replace/write races on Windows are transient, not failed sessions.
    public static bool IsSharingViolation(IOException error) => (error.HResult & 0xffff) is 32 or 33;
    public static Dictionary<string, string> Read(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        using var reader = new StreamReader(stream);
        return Parse(reader.ReadToEnd());
    }
    public static Dictionary<string, string> Parse(string text)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var line in text.Split('\n'))
        {
            if (line.StartsWith('#') || line.StartsWith('!')) continue;
            var index = line.IndexOf('=');
            if (index > 0) result[line[..index].Trim()] = line[(index + 1)..].TrimEnd('\r');
        }
        return result;
    }
}

internal static class GameDiscovery
{
    static readonly HashSet<string> EntryPoints = new(StringComparer.Ordinal)
    {
        "cpw.mods.bootstraplauncher.BootstrapLauncher", "cpw.mods.modlauncher.Launcher",
        "net.minecraft.client.main.Main", "net.fabricmc.loader.impl.launch.knot.KnotClient",
        "net.fabricmc.loader.launch.knot.KnotClient"
    };
    static readonly HashSet<string> PairedOptions = new(StringComparer.Ordinal)
    {
        "-cp", "-classpath", "--class-path", "-p", "--module-path", "--upgrade-module-path",
        "--add-opens", "--add-exports", "--add-modules", "--patch-module", "--add-reads",
        "--limit-modules", "--enable-native-access", "--source"
    };
    public static GameProcess? Classify(int pid, string name, string command, string startKey)
    {
        // JVM properties commonly quote only the value: -Dname="path with spaces".
        // Adjacent quoted/unquoted fragments belong to the same argument.
        var tokens = Regex.Matches(command, "(?:[^\\s\"]+|\"[^\"]*\")+")
            .Select(m => m.Value.Replace("\"", "")).ToArray();
        string? entry = null;
        var mainIndex = -1;
        for (var i = 1; i < tokens.Length; i++)
        {
            var token = tokens[i];
            if (PairedOptions.Contains(token)) { i++; continue; }
            if (token is "-jar" or "-m" or "--module") return null;
            if (token.StartsWith('-') || token.StartsWith('@')) continue;
            entry = token;
            mainIndex = i;
            break;
        }
        if (entry is null || !EntryPoints.Contains(entry)) return null;
        string Flag(string key)
        {
            for (var i = mainIndex + 1; i + 1 < tokens.Length; i++)
                if (tokens[i].Equals(key, StringComparison.OrdinalIgnoreCase)) return tokens[i + 1];
            return "";
        }
        var version = Flag("--fml.mcVersion");
        if (version.Length == 0) version = Flag("--version");
        var supported = version == "1.20.1";
        var netease = Regex.IsMatch(command, "MCLDownload|launcherGameId|pc\\.bjdmc\\.net|a50_sdk_cn|Netease|MCLauncher", RegexOptions.IgnoreCase);
        return new GameProcess { Pid = pid, Name = name, StartKey = startKey, IsNetease = netease,
            Version = version, Supported = supported, CommandLine = $"{version} · {entry}" };
    }
    public static async Task<List<GameProcess>> FindAsync(CancellationToken cancellation)
    {
        const string script = "$x=Get-CimInstance Win32_Process | Where-Object {$_.Name -in @('java.exe','javaw.exe')} | Select-Object ProcessId,Name,CommandLine,@{Name='Started';Expression={$_.CreationDate.ToUniversalTime().Ticks.ToString()}}; ConvertTo-Json -Compress -InputObject @($x)";
        var info = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"));
        info.ArgumentList.Add("-NoProfile"); info.ArgumentList.Add("-NonInteractive");
        info.ArgumentList.Add("-Command"); info.ArgumentList.Add(script);
        var result = await ChildProcess.RunAsync(info, TimeSpan.FromSeconds(12), cancellation).ConfigureAwait(false);
        if (result.ExitCode != 0) throw new IOException("读取进程列表失败。");
        using var doc = JsonDocument.Parse(string.IsNullOrWhiteSpace(result.Output) ? "[]" : result.Output);
        var games = new List<GameProcess>();
        foreach (var item in doc.RootElement.EnumerateArray())
        {
            var command = item.GetProperty("CommandLine").GetString() ?? "";
            var process = Classify(item.GetProperty("ProcessId").GetInt32(), item.GetProperty("Name").GetString() ?? "java.exe", command, item.GetProperty("Started").GetString() ?? "");
            if (process is not null) games.Add(process);
        }
        return games.OrderByDescending(p => p.Supported && p.IsNetease).ThenByDescending(p => p.Supported).ThenBy(p => p.Pid).ToList();
    }
}
