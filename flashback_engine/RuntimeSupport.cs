using System.Diagnostics;
using System.Globalization;
using System.IO.Compression;
using System.Management;
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
        "net.fabricmc.loader.launch.knot.KnotClient", "net.neoforged.fml.startup.Client"
    };
    static readonly HashSet<string> PairedOptions = new(StringComparer.Ordinal)
    {
        "-cp", "-classpath", "--class-path", "-p", "--module-path", "--upgrade-module-path",
        "--add-opens", "--add-exports", "--add-modules", "--patch-module", "--add-reads",
        "--limit-modules", "--enable-native-access", "--source"
    };
    public static GameProcess? Classify(int pid, string name, string command, string startKey,
        string? executablePath = null, IEnumerable<string>? loadedNativeModules = null)
    {
        if (!IsJavaName(name)) return null;
        // JVM properties commonly quote only the value: -Dname="path with spaces".
        // Adjacent quoted/unquoted fragments belong to the same argument.
        var tokens = Regex.Matches(command, "(?:[^\\s\"]+|\"[^\"]*\")+")
            .Select(m => m.Value.Replace("\"", "")).ToArray();
        string? entry = null;
        var mainIndex = -1;
        // Some clients overwrite their command line with a display string.
        // An embedded Minecraft class name is not a Java launch command.
        for (var i = 1; tokens.Length > 0 && IsJavaName(Path.GetFileName(tokens[0])) && i < tokens.Length; i++)
        {
            var token = tokens[i];
            if (PairedOptions.Contains(token)) { i++; continue; }
            if (token is "-jar" or "-m" or "--module") break;
            if (token.StartsWith('-') || token.StartsWith('@')) continue;
            entry = token;
            mainIndex = i;
            break;
        }
        var recognizedEntry = entry is not null && EntryPoints.Contains(entry);
        string Flag(string key)
        {
            if (!recognizedEntry) return "";
            for (var i = mainIndex + 1; i + 1 < tokens.Length; i++)
                if (tokens[i].Equals(key, StringComparison.OrdinalIgnoreCase)) return tokens[i + 1];
            return "";
        }
        var version = Flag("--fml.mcVersion");
        if (version.Length == 0) version = Flag("--version");
        var native = ReadNativeEvidence(executablePath, loadedNativeModules);
        if (!recognizedEntry && native is null) return null;
        // A version folder, title, or a lone LWJGL DLL cannot establish a
        // Minecraft version. Native evidence also checks the adjacent game
        // JAR's version.json, including its protocol and data version.
        var conflict = native is { Ambiguous: true };
        if (native is { Ambiguous: false })
        {
            if (Regex.IsMatch(version, @"^\d+\.\d+(?:\.\d+)?$") && version != native.Version)
                conflict = true;
            version = native.Version;
        }
        var supported = !conflict && (version is "1.20.1" or "1.21.10");
        var netease = Regex.IsMatch((recognizedEntry ? command : "") + " " + executablePath + " " + native?.Instance,
            "MCLDownload|launcherGameId|pc\\.bjdmc\\.net|a50_sdk_cn|Netease|MCLauncher", RegexOptions.IgnoreCase);
        var description = recognizedEntry ? entry : "已加载游戏原生库 / version.json 已验证";
        if (conflict) description = "版本证据冲突，禁止注入";
        return new GameProcess { Pid = pid, Name = name, StartKey = startKey, IsNetease = netease,
            Version = version, Supported = supported, CommandLine = $"{version} · {description}" };
    }

    static bool IsJavaName(string name) => name.Equals("java.exe", StringComparison.OrdinalIgnoreCase)
        || name.Equals("javaw.exe", StringComparison.OrdinalIgnoreCase)
        || name.Equals("java", StringComparison.OrdinalIgnoreCase)
        || name.Equals("javaw", StringComparison.OrdinalIgnoreCase);

    sealed record NativeEvidence(string Version, string Instance, bool Ambiguous = false);

    static NativeEvidence? ReadNativeEvidence(string? executablePath, IEnumerable<string>? loadedModules)
    {
        if (string.IsNullOrWhiteSpace(executablePath) || !Path.IsPathFullyQualified(executablePath)
            || !IsJavaName(Path.GetFileName(executablePath)) || !File.Exists(executablePath)
            || loadedModules is null) return null;
        var roots = new Dictionary<string, HashSet<string>>(StringComparer.OrdinalIgnoreCase);
        foreach (var module in loadedModules)
        {
            if (string.IsNullOrWhiteSpace(module) || !Path.IsPathFullyQualified(module) || !File.Exists(module)) continue;
            var file = Path.GetFileName(module);
            if (!file.Equals("lwjgl.dll", StringComparison.OrdinalIgnoreCase)
                && !file.Equals("glfw.dll", StringComparison.OrdinalIgnoreCase)) continue;
            var root = Path.GetDirectoryName(Path.GetFullPath(module));
            if (root is null) continue;
            var nativesName = Path.GetFileName(root);
            if (!nativesName.Equals("natives", StringComparison.OrdinalIgnoreCase)
                && !nativesName.StartsWith("natives-", StringComparison.OrdinalIgnoreCase)) continue;
            if (!roots.TryGetValue(root, out var modules)) roots[root] = modules = new(StringComparer.OrdinalIgnoreCase);
            modules.Add(file);
        }
        var candidates = new List<NativeEvidence>();
        foreach (var (root, modules) in roots)
        {
            if (!modules.Contains("lwjgl.dll") || !modules.Contains("glfw.dll")) continue;
            var instance = Directory.GetParent(root);
            if (instance?.Parent is null || !instance.Parent.Name.Equals("versions", StringComparison.OrdinalIgnoreCase)) continue;
            var jar = Path.Combine(instance.FullName, instance.Name + ".jar");
            try
            {
                using var stream = new FileStream(jar, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                using var archive = new ZipArchive(stream, ZipArchiveMode.Read);
                var entry = archive.GetEntry("version.json");
                if (entry is null || entry.Length <= 0 || entry.Length > 65536) continue;
                using var versionStream = entry.Open();
                using var document = JsonDocument.Parse(versionStream);
                var metadata = document.RootElement;
                if (metadata.ValueKind != JsonValueKind.Object
                    || !metadata.TryGetProperty("id", out var id) || id.ValueKind != JsonValueKind.String
                    || !metadata.TryGetProperty("protocol_version", out var protocol) || protocol.ValueKind != JsonValueKind.Number || !protocol.TryGetInt32(out var protocolId)
                    || !metadata.TryGetProperty("world_version", out var world) || world.ValueKind != JsonValueKind.Number || !world.TryGetInt32(out var dataVersion)) continue;
                var version = id.GetString() ?? "";
                if (!Regex.IsMatch(version, @"^\d+\.\d+(?:\.\d+)?$") || protocolId <= 0 || dataVersion <= 0) continue;
                // Fail closed if a supported label was put on another game
                // build. Other actual versions remain visible but unsupported.
                var mismatch = version switch
                {
                    "1.20.1" => protocolId != 763 || dataVersion != 3465,
                    "1.21.10" => protocolId != 773 || dataVersion != 4556,
                    _ => false
                };
                candidates.Add(new(version, instance.FullName, mismatch));
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException or InvalidDataException or JsonException or NotSupportedException) { }
        }
        if (candidates.Count == 0) return null;
        if (candidates.Count != 1) return new("unknown", "", Ambiguous: true);
        return candidates[0];
    }

    public static Task<List<GameProcess>> FindAsync(CancellationToken cancellation) =>
        Task.Run(() => Find(cancellation), cancellation);

    static List<GameProcess> Find(CancellationToken cancellation)
    {
        var games = new List<GameProcess>();
        // Query only Java processes in-process; periodic detection must not cold-start PowerShell.
        using var searcher = new ManagementObjectSearcher(new ManagementScope("root\\cimv2"),
            new ObjectQuery("SELECT ProcessId, Name, CommandLine, ExecutablePath, CreationDate FROM Win32_Process WHERE Name='java.exe' OR Name='javaw.exe'"),
            new System.Management.EnumerationOptions { ReturnImmediately = true, Rewindable = false, Timeout = TimeSpan.FromSeconds(3) });
        using var results = searcher.Get();
        foreach (ManagementObject item in results)
        {
            using (item)
            {
            cancellation.ThrowIfCancellationRequested();
            var pid = checked((int)(uint)item["ProcessId"]);
            var name = item["Name"] as string ?? "java.exe";
            var creation = item["CreationDate"] as string;
            if (string.IsNullOrWhiteSpace(creation)) continue;
            var creationTicks = ManagementDateTimeConverter.ToDateTime(creation).ToUniversalTime().Ticks;
            var started = creationTicks.ToString(CultureInfo.InvariantCulture);
            var command = item["CommandLine"] as string ?? "";
            var executable = item["ExecutablePath"] as string;
            try
            {
                using var live = Process.GetProcessById(pid);
                // Do not combine a recycled PID's modules with an older CIM
                // command line or process identity.
                // WMI truncates timestamps to microseconds; Process exposes
                // the original 100 ns FILETIME value.
                if (live.StartTime.ToUniversalTime().Ticks / 10 != creationTicks / 10) continue;
            }
            catch (System.ComponentModel.Win32Exception) { /* WMI still supplies the creation identity. */ }
            catch (Exception error) when (error is InvalidOperationException or ArgumentException) { continue; }
            // Toolhelp includes both 64-bit and WOW64 modules. Access denial is
            // reported, never replaced by guesses based on installed folders or file locks.
            var modules = NativeProcessModules.Read(pid, out var moduleError);
            var process = Classify(pid, name, command, started, executable, modules);
            if (process is null && moduleError != 0)
                process = new GameProcess { Pid = pid, Name = name, StartKey = started,
                    Version = "未验证", Supported = false,
                    CommandLine = $"无法验证游戏版本（模块读取错误 {moduleError}）；请检查进程权限。" };
            if (process is not null) games.Add(process);
            }
        }
        return games.OrderByDescending(p => p.Supported && p.IsNetease).ThenByDescending(p => p.Supported).ThenBy(p => p.Pid).ToList();
    }
}
