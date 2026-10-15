using System.IO.Compression;
using System.Text.Json;
using System.Diagnostics;
using FlashbackRecorder;

var fixtureBase = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "fixtures"));
var fixtureRoot = Path.Combine(fixtureBase, Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(fixtureRoot);
var java = Path.Combine(fixtureRoot, "MCLDownload", "jdk22", "bin", "javaw.exe");
Directory.CreateDirectory(Path.GetDirectoryName(java)!);
File.WriteAllText(java, "test fixture; never executed");
var count = 0;
void Check(bool condition, string name)
{
    if (!condition) throw new Exception("FAIL: " + name);
    Console.WriteLine("PASS " + name);
    count++;
}
GameProcess? Classify(string command, string? exe = null, IEnumerable<string>? modules = null, string name = "javaw.exe")
    => GameDiscovery.Classify(1234, name, command, "123456", exe, modules);
string[] Fixture(string folder, string version, int protocol, int dataVersion, string? json = null)
{
    var instance = Path.Combine(fixtureRoot, "netease_minecraft_neoforge", "versions", folder);
    var natives = Path.Combine(instance, "natives-windows-x86_64");
    Directory.CreateDirectory(natives);
    var paths = new[] { Path.Combine(natives, "lwjgl.dll"), Path.Combine(natives, "glfw.dll") };
    foreach (var path in paths) File.WriteAllText(path, "fixture; never loaded");
    using var zip = ZipFile.Open(Path.Combine(instance, folder + ".jar"), ZipArchiveMode.Create);
    using var writer = new StreamWriter(zip.CreateEntry("version.json").Open());
    writer.Write(json ?? JsonSerializer.Serialize(new { id = version, protocol_version = protocol, world_version = dataVersion }));
    return paths;
}

const string masked = "net.caffeinemc.sodium / net.minecraft.client.main.Main /";
var old = Classify("javaw.exe -Xmx2G cpw.mods.bootstraplauncher.BootstrapLauncher --version custom --fml.mcVersion 1.20.1");
Check(old is { Supported: true, Version: "1.20.1", Identity: "1234:123456" }, "legacy Forge launch and identity");
Check(Classify("\"C:\\Program Files\\Java\\bin\\javaw.exe\" -Dfoo=\"path with spaces\" -cp \"a b.jar\" net.minecraft.client.main.Main --version 1.20.1") is { Supported: true }, "quoted JVM arguments");
Check(Classify("java.exe net.neoforged.fml.startup.Client --version 1.21.10") is { Supported: true, Version: "1.21.10" }, "NeoForge launch entry");
Check(Classify("java.exe net.minecraft.client.main.Main --version 1.21.11") is { Supported: false }, "playback version is not a capture target");
Check(Classify("java.exe net.minecraft.client.main.Main --version 1.21.1") is { Supported: false }, "no partial version matching");
Check(Classify("java.exe net.minecraft.client.main.Main --version 1.21.10.0") is { Supported: false }, "no version-prefix matching");
Check(Classify(masked) is null, "display class names alone rejected");
Check(Classify("布吉岛 Minecraft 1.21.10") is null, "display title alone rejected");
Check(Classify("java.exe -jar helper.jar net.minecraft.client.main.Main --version 1.21.10") is null, "helper jar arguments cannot impersonate main class");
Check(Classify("java.exe -cp net.minecraft.client.main.Main Helper --version 1.21.10") is null, "classpath cannot impersonate main class");
Check(Classify("java.exe -Dversion=1.21.10 Helper") is null, "unrelated Java process rejected");

var currentModules = Fixture("1.21.10", "1.21.10", 773, 4556);
var detected = Classify(masked, java, currentModules);
Check(detected is { Supported: true, Version: "1.21.10", IsNetease: true }, "masked client verified from executable and loaded native paths");
Check(Classify("", java, currentModules) is { Supported: true }, "empty command line with verified native evidence");
Check(Classify(masked, null, currentModules) is null, "missing executable evidence rejected");
Check(Classify(masked, Path.Combine(fixtureRoot, "missing", "javaw.exe"), currentModules) is null, "nonexistent Java executable rejected");
Check(Classify(masked, java, currentModules, "helper.exe") is null, "non-Java process rejected");
Check(Classify(masked, java, Array.Empty<string>()) is null, "isolated installed version directory is not evidence");
Check(Classify(masked, java, currentModules.Take(1)) is null, "lone native DLL rejected");
Check(Classify("java.exe net.minecraft.client.main.Main --version 1.20.1", java, currentModules) is { Supported: false }, "conflicting command and JAR versions block injection");
Check(Classify("java.exe net.minecraft.client.main.Main --version custom-profile", java, currentModules) is { Supported: true, Version: "1.21.10" }, "JAR version resolves custom launch profile");

var oldModules = Fixture("old-client", "1.20.1", 763, 3465);
Check(Classify(masked, java, oldModules) is { Supported: true, Version: "1.20.1" }, "legacy native evidence accepted");
var otherModules = Fixture("misleading-1.21.10", "1.21.11", 774, 4671);
Check(Classify(masked, java, otherModules) is { Supported: false, Version: "1.21.11" }, "actual JAR version overrides directory label");
Check(Classify(masked, java, new[] { currentModules[0], oldModules[1] }) is null, "native libraries from different instances cannot combine");
Check(Classify(masked, java, currentModules.Concat(oldModules)) is { Supported: false }, "multiple complete native roots block injection");
var wrongProtocol = Fixture("wrong-protocol", "1.21.10", 774, 4556);
Check(Classify(masked, java, wrongProtocol) is not { Supported: true }, "supported label with wrong protocol rejected");
var wrongData = Fixture("wrong-data", "1.21.10", 773, 4671);
Check(Classify(masked, java, wrongData) is not { Supported: true }, "supported label with wrong data version rejected");
var invalidJson = Fixture("bad-json", "", 0, 0, "{invalid");
Check(Classify(masked, java, invalidJson) is null, "malformed version JSON rejected");
var badTypes = Fixture("bad-types", "", 0, 0, "{\"id\":\"1.21.10\",\"protocol_version\":\"773\",\"world_version\":4556}");
Check(Classify(masked, java, badTypes) is null, "invalid metadata types rejected");
var missingVersion = Fixture("missing-version", "", 0, 0, "{}");
Check(Classify(masked, java, missingVersion) is null, "absent version metadata rejected");
var nonObject = Fixture("non-object", "", 0, 0, "[]");
Check(Classify(masked, java, nonObject) is null, "non-object metadata rejected");

if (args.Length == 2 && args[0] == "--live-pid")
{
    var pid = int.Parse(args[1]);
    var processes = await GameDiscovery.FindAsync(CancellationToken.None);
    var live = processes.SingleOrDefault(process => process.Pid == pid);
    Check(live is { Supported: true, Version: "1.21.10", IsNetease: true }, "live read-only discovery of PID " + pid);
    Console.WriteLine(JsonSerializer.Serialize(new { live!.Pid, live.Version, live.Supported, live.IsNetease, live.Identity, live.CommandLine }));
}
var ownModules = NativeProcessModules.Read(Environment.ProcessId, out var ownError);
Check(JavaRuntimeProbe.IsX64Executable(Environment.ProcessPath!), "current test host PE is x64");
var truncatedPe = Path.Combine(fixtureRoot, "truncated-java.exe"); File.WriteAllBytes(truncatedPe, new byte[] { 0x4d, 0x5a });
Check(!JavaRuntimeProbe.IsX64Executable(truncatedPe), "truncated Java executable is rejected");
var pe = new byte[128]; pe[0] = 0x4d; pe[1] = 0x5a; pe[0x3c] = 64;
pe[64] = 0x50; pe[65] = 0x45; pe[68] = 0x4c; pe[69] = 0x01;
var x86Pe = Path.Combine(fixtureRoot, "x86-java.exe"); File.WriteAllBytes(x86Pe, pe);
Check(!JavaRuntimeProbe.IsX64Executable(x86Pe) && !await JavaRuntimeProbe.IsUsableAsync(x86Pe), "32-bit Java is rejected before execution");
pe[68] = 0x64; pe[69] = 0xaa;
var armPe = Path.Combine(fixtureRoot, "arm-java.exe"); File.WriteAllBytes(armPe, pe);
Check(!JavaRuntimeProbe.IsX64Executable(armPe), "ARM Java is not mistaken for x64 Attach runtime");
if (args.FirstOrDefault() == "--jdk")
    foreach (var path in args.Skip(1))
        Check(await JavaRuntimeProbe.IsUsableAsync(path), "actual Java 17+ x64 Attach runtime verified: " + Path.GetFileName(Path.GetDirectoryName(Path.GetDirectoryName(path))));
Check(ownError == 0 && ownModules.Any(path => Path.GetFileName(path).Equals("coreclr.dll", StringComparison.OrdinalIgnoreCase)),
    "native 64-bit module snapshot succeeds");
_ = NativeProcessModules.Read(int.MaxValue, out var invalidError);
Check(invalidError != 0, "unavailable process reports a module error without crashing");
var scriptHost = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "SysWOW64", "cscript.exe");
if (Environment.Is64BitProcess && File.Exists(scriptHost))
{
    var script = Path.Combine(fixtureRoot, "sleep.vbs"); File.WriteAllText(script, "WScript.Sleep 10000");
    var info = new ProcessStartInfo(scriptHost) { UseShellExecute = false, CreateNoWindow = true };
    info.ArgumentList.Add("//nologo"); info.ArgumentList.Add(script);
    using var helper = Process.Start(info)!;
    try
    {
        var modules = Array.Empty<string>(); var error = 0;
        for (var attempt = 0; attempt < 10; attempt++)
        {
            modules = NativeProcessModules.Read(helper.Id, out error);
            if (modules.Any(path => path.Contains("SysWOW64", StringComparison.OrdinalIgnoreCase))) break;
            await Task.Delay(50);
        }
        Check(error == 0 && modules.Any(path => path.Contains("SysWOW64", StringComparison.OrdinalIgnoreCase)),
            "64-bit detector reads actual 32-bit helper modules");
    }
    finally { if (!helper.HasExited) helper.Kill(true); helper.WaitForExit(2500); }
}
Console.WriteLine($"{count} checks passed. No attach or recording commands were used.");

namespace FlashbackRecorder
{
    // Keep discovery tests independent from WinForms and the embedded runtime.
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
    }
}
