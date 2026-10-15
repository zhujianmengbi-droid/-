using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using FlashbackRecorder;

if (args.FirstOrDefault() == "--child-helper")
{
    var mode = args[1];
    if (mode == "output")
    {
        Console.WriteLine(new string('x', 200000));
        Console.Error.WriteLine(new string('y', 200000));
        Environment.ExitCode = 7;
    }
    else if (mode == "inherit")
    {
        var info = new ProcessStartInfo(Path.Combine(AppContext.BaseDirectory, "ExportCompatibility.exe")) { UseShellExecute = false, CreateNoWindow = true };
        foreach (var value in new[] { "--child-helper", "sleep", args[2] }) info.ArgumentList.Add(value);
        using var holder = Process.Start(info)!;
        File.WriteAllText(args[2], holder.Id.ToString());
        Console.WriteLine("parent exited");
    }
    else
    {
        File.WriteAllText(args[2], Environment.ProcessId.ToString());
        Console.WriteLine("ready");
        await Task.Delay(30000);
    }
    return;
}

if (args.FirstOrDefault() == "--import")
{
    if (args.Length != 5)
        throw new ArgumentException("Usage: --import <engine.dll> <replay.zip> <game-directory> <skin-cache-directory>; run with the engine runtimeconfig.json to load Windows Forms.");
    // Exercise the exact ZJ HUB import path without constructing a Form,
    // opening a replay, starting Minecraft, or attaching to any process.
    var engine = Assembly.LoadFrom(Path.GetFullPath(args[1]));
    var form = engine.GetType("FlashbackRecorder.RecorderForm", throwOnError: true)!;
    var import = form.GetMethod("ImportWithSkins", BindingFlags.Static | BindingFlags.NonPublic)!;
    object result;
    try { result = import.Invoke(null, new object[] { args[2], args[3], args[4] })!; }
    catch (TargetInvocationException error) when (error.InnerException is not null)
    { System.Runtime.ExceptionServices.ExceptionDispatchInfo.Capture(error.InnerException).Throw(); throw; }
    var output = (string)result.GetType().GetField("Item1")!.GetValue(result)!;
    var skins = result.GetType().GetField("Item2")!.GetValue(result)!;
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        ok = true, output,
        sourceReplaySha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(args[2]))),
        importedReplaySha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(output))),
        skinInstall = skins
    }, new JsonSerializerOptions { WriteIndented = true }));
    return;
}

if (args.FirstOrDefault() == "--audit-engine")
{
    if (args.Length != 3) throw new ArgumentException("Usage: --audit-engine <engine.dll> <runtime-directory>");
    var engine = Assembly.LoadFrom(Path.GetFullPath(args[1]));
    var assets = engine.GetType("FlashbackRecorder.RuntimeAssets", throwOnError: true)!;
    var required = assets.GetMethod("RequiredAgentVersionFor", BindingFlags.Static | BindingFlags.Public)!;
    var agent = assets.GetMethod("AgentForVersion", BindingFlags.Static | BindingFlags.Public)!;
    foreach (var (version, minimum, filename) in new[] {
        ("1.20.1", 13, "runtime-recorder-agent-v13.jar"),
        ("1.21.10", 17, "runtime-recorder-agent-1.21.10-v2.jar") })
    {
        if ((int)required.Invoke(null, new object[] { version })! != minimum
            || Path.GetFileName((string)agent.Invoke(null, new object[] { version })!) != filename)
            throw new InvalidDataException("Engine agent routing mismatch for " + version);
        Console.WriteLine($"PASS engine route {version}, minimum {minimum}, {filename}");
    }
    var resourceNames = engine.GetManifestResourceNames();
    foreach (var filename in new[] { "runtime-recorder-cli.jar", "runtime-recorder-agent-v13.jar",
        "runtime-recorder-agent-1.21.10-v2.jar", "flashback-protocol-adapter-v6.jar" })
    {
        var name = resourceNames.Single(resource => resource == "RuntimeAssets/" + filename);
        using var embedded = engine.GetManifestResourceStream(name)!;
        using var disk = File.OpenRead(Path.Combine(args[2], filename));
        if (!SHA256.HashData(embedded).SequenceEqual(SHA256.HashData(disk)))
            throw new InvalidDataException("Embedded JAR differs from runtime input: " + filename);
        Console.WriteLine("PASS embedded JAR matches source asset: " + filename);
    }
    Console.WriteLine("Engine audit passed. No recorder was started.");
    return;
}

if (args.FirstOrDefault() == "--convert")
{
    if (args.Length != 4)
    {
        Console.Error.WriteLine("Usage: ExportCompatibility --convert <capture.fbrl> <java.exe> <adapter.jar>");
        Environment.ExitCode = 2;
        return;
    }
    var watch = Stopwatch.StartNew();
    var result = FlashbackZip.RunProtocolAdapter(args[1], args[2], args[3]);
    Console.WriteLine(result.Text);
    if (!result.Ok) { Environment.ExitCode = 1; return; }
    try
    {
        var output = FlashbackZip.Convert(args[1], null, out var quality);
        using var archive = ZipFile.OpenRead(output);
        using var metadata = JsonDocument.Parse(archive.GetEntry("metadata.json")!.Open());
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            ok = true, output, bytes = new FileInfo(output).Length,
            sha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(output))),
            elapsedSeconds = watch.Elapsed.TotalSeconds,
            version = metadata.RootElement.GetProperty("version_string").GetString(),
            protocol = metadata.RootElement.GetProperty("protocol_version").GetInt32(),
            totalTicks = metadata.RootElement.GetProperty("total_ticks").GetInt32(),
            quality = new { quality.Status, quality.HasDataLoss, quality.HasCompatibilityLimitations, quality.Summary }
        }));
    }
    catch (Exception error)
    {
        Console.Error.WriteLine(error.ToString());
        Environment.ExitCode = 1;
    }
    return;
}

// A process fixture exercises the real C# launch, staging and publication
// path. These bytes are intentionally not a playable Minecraft recording.
if (args.FirstOrDefault() == "-cp")
{
    var dir = args[^1];
    var properties = ReadProperties(Path.Combine(dir, "capture.properties"));
    if (properties.TryGetValue("fixtureAdapterDelayMs", out var adapterDelay))
    {
        File.WriteAllText(Path.Combine(dir, "helper.pid"), Environment.ProcessId.ToString());
        await Task.Delay(int.Parse(adapterDelay));
    }
    var identity = ReadProperties(Path.Combine(dir, "replay-player.properties"));
    var isModernRoute = args[2] == "ModernFlashbackProtocolAdapter";
    var route = isModernRoute ? "modern" : args[2] == "FlashbackProtocolAdapter" ? "legacy" : "unknown";
    var sourceProtocol = isModernRoute ? "773" : "763";
    if (properties.GetValueOrDefault("fixtureExpectedRoute") != route
        || identity.GetValueOrDefault("sourceProtocol") != sourceProtocol)
    {
        Console.Error.WriteLine("Wrong adapter route or replay identity protocol: " + route);
        Environment.ExitCode = 3;
        return;
    }
    foreach (var name in new[] { "converted-config.bin", "converted-game-snapshot.bin", "converted-chunks.bin" })
        File.WriteAllBytes(Path.Combine(dir, name), Frame(8, 0, new byte[] { 7 }));
    File.WriteAllBytes(Path.Combine(dir, "converted-entities.bin"), Array.Empty<byte>());
    File.WriteAllBytes(Path.Combine(dir, "converted-packets.bin"), Frame(12, 101, new byte[] { 7 }));
    File.WriteAllText(Path.Combine(dir, "converted.properties"),
        $"sourceProtocol={sourceProtocol}\ntargetProtocol=774\ndynamicPacketErrors={properties.GetValueOrDefault("fixturePacketErrors", "0")}\nfixtureRoute={route}\n"
        + "customRegistryTagGroupsDropped=" + properties.GetValueOrDefault("fixtureRegistryTagGroupsDropped", "0") + "\n"
        + "playerProfilesMissing=" + properties.GetValueOrDefault("fixtureMissingProfiles", "0") + "\n"
        + "recordedProfileManifestInvalid=" + properties.GetValueOrDefault("fixtureInvalidSkinManifest", "0") + "\n"
        + "playerProfilesRecoveredFromManifest=" + properties.GetValueOrDefault("fixtureRecoveredProfiles", "0") + "\n"
        + "incompleteWorldTransitions=" + properties.GetValueOrDefault("fixtureIncompleteTransitions", "0") + "\n"
        + "fixtureSkinSha256=" + (File.Exists(Path.Combine(dir, "recorded-skins.json"))
            ? Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(Path.Combine(dir, "recorded-skins.json")))) : "missing") + "\n");
    if (properties.TryGetValue("fixtureMutateRawSkin", out var rawSkin))
        File.AppendAllText(rawSkin, "\n");
    if (properties.GetValueOrDefault("fixtureDamage") == "chunk")
        File.WriteAllBytes(Path.Combine(dir, "converted-chunks.bin"), new byte[] { 0, 1 });
    if (properties.GetValueOrDefault("fixtureDamage") == "dynamic")
        File.WriteAllBytes(Path.Combine(dir, "converted-packets.bin"), new byte[] { 0, 1 });
    if (properties.GetValueOrDefault("captureFormat") == "5")
    {
        var local = Enumerable.Range(0, 9).Select(slot => new byte[] { 20, 0, 0, 0, (byte)slot })
            .Concat(new[] { new byte[] { 103, 0 }, new byte[] { 101 }, new byte[] { 102 } }).ToArray();
        var dynamic = local.SelectMany(packet => Frame(12, 100, packet)).ToList();
        var timeline = local.SelectMany(packet => TimelineFrame(100, 0, packet)).ToList();
        var transitions = properties.GetValueOrDefault("fixtureTransitions", "").Split(';', StringSplitOptions.RemoveEmptyEntries);
        foreach (var spec in transitions)
        {
            var parts = spec.Split(',', 3); var tick = long.Parse(parts[0]);
            var respawn = parts[1].StartsWith('R'); var unbound = parts[1].StartsWith('X');
            var combined = parts[1].StartsWith('C');
            var tag = byte.Parse(respawn || unbound || combined ? parts[1][1..] : parts[1]);
            var dimension = Encoding.UTF8.GetBytes(parts[2]);
            if (!respawn && !unbound) timeline.AddRange(TimelineFrame(tick, 1, new byte[] { 12, tag }));
            var boundary = respawn ? new byte[] { 80, 0, (byte)dimension.Length }.Concat(dimension).ToArray()
                : unbound || properties.GetValueOrDefault("fixtureFakeBoundary") == "true" ? new byte[] { 24, 0 } : new byte[] { 48 };
            timeline.AddRange(TimelineFrame(tick, 0, boundary));
            timeline.AddRange(TimelineFrame(tick, 2, new byte[] { 0, (byte)dimension.Length }.Concat(dimension).ToArray()));
            dynamic.AddRange(Frame(12, tick, boundary));
            var finalBoundaryTick = tick;
            if (combined)
            {
                var chain = int.Parse(properties.GetValueOrDefault("fixtureRespawnChain", "1"));
                var delay = int.Parse(properties.GetValueOrDefault("fixtureRespawnDelay", "0"));
                for (var n = 0; n < chain; n++)
                {
                    finalBoundaryTick = tick + (n + 1) * delay;
                    if (properties.GetValueOrDefault("fixtureInterveningPacket") == "true")
                        timeline.AddRange(TimelineFrame(finalBoundaryTick, 0, new byte[] { 24, 99 }));
                    var nextDimension = n + 1 == chain ? dimension : Encoding.UTF8.GetBytes("minecraft:the_nether");
                    var packet = new byte[] { 80, 0, (byte)nextDimension.Length }.Concat(nextDimension).ToArray();
                    timeline.AddRange(TimelineFrame(finalBoundaryTick, 0, packet));
                    timeline.AddRange(TimelineFrame(finalBoundaryTick, 2, new byte[] { 0, (byte)nextDimension.Length }.Concat(nextDimension).ToArray()));
                    dynamic.AddRange(Frame(12, finalBoundaryTick, packet));
                }
            }
            timeline.AddRange(TimelineFrame(finalBoundaryTick + 1, 0, new byte[] { 24, tag }));
            dynamic.AddRange(Frame(12, finalBoundaryTick + 1, new byte[] { 24, tag }));
        }
        File.WriteAllBytes(Path.Combine(dir, "converted-packets.bin"), dynamic.ToArray());
        File.WriteAllBytes(Path.Combine(dir, "converted-timeline.bin"), timeline.ToArray());
        File.AppendAllText(Path.Combine(dir, "converted.properties"), "timelineFormat=1\n");
    }
    Console.WriteLine("fixtureRoute=" + route);
    return;
}

var root = Path.Combine(AppContext.BaseDirectory, "test-results-" + DateTime.Now.ToString("yyyyMMdd-HHmmss-fff"));
Directory.CreateDirectory(root);
var fixtureJar = Path.Combine(root, "fixture-adapter.jar");
File.WriteAllText(fixtureJar, "test child marker, never loaded by Java");
var executable = Path.Combine(AppContext.BaseDirectory, "ExportCompatibility.exe");
var passed = 0;
var failed = new List<string>();
void Check(bool condition, string name)
{
    if (condition) { Console.WriteLine("PASS " + name); passed++; }
    else { Console.WriteLine("FAIL " + name); failed.Add(name); }
}
void Reject(Action action, string name)
{
    try { action(); Check(false, name); }
    catch (InvalidDataException) { Check(true, name); }
}
string Capture(string name, string version, int? protocol, bool configuration = false, string extraProperties = "")
{
    var dir = Path.Combine(root, name);
    Directory.CreateDirectory(dir);
    var path = Path.Combine(dir, "recording.fbrl");
    var metadata = new JsonObject
    {
        ["type"] = "meta", ["format"] = 5, ["version"] = version,
        ["createdUtc"] = DateTimeOffset.UtcNow.ToString("O"),
        ["dataVersion"] = version == "1.21.10" ? 4556 : 3465,
        ["playerUuid"] = "12345678-1234-1234-1234-123456789abc",
        ["playerName"] = "CompatibilityFixture", ["playerEntityId"] = 3,
        ["worldName"] = "Fixture world", ["gametype"] = 0
    };
    if (protocol.HasValue) metadata["protocolVersion"] = protocol;
    File.WriteAllBytes(Path.Combine(dir, "snapshot-packets.bin"), new byte[] { 1 });
    File.WriteAllBytes(Path.Combine(dir, "snapshot-chunks.bin"), Frame(4, 0, new byte[] { 1 }));
    if (configuration) File.WriteAllBytes(Path.Combine(dir, "snapshot-config.bin"), Frame(4, 0, new byte[] { 7 }));
    File.WriteAllText(Path.Combine(dir, "capture.properties"),
        "packetErrors=0\npacketDrops=0\nentitySnapshotErrors=0\nskinCaptureErrors=0\n" +
        "fixtureExpectedRoute=" + (version == "1.21.10" ? "modern" : "legacy") + "\n" + extraProperties);
    File.WriteAllText(path, metadata.ToJsonString() + "\n" + """
{"type":"sample","t":0,"tick":100,"x":1,"y":64,"z":3,"dimension":"minecraft:overworld"}
{"type":"sample","t":50,"tick":101,"x":2,"y":64,"z":3,"dimension":"minecraft:overworld"}
{"type":"mark","t":50,"name":"Compatibility marker"}
""" + "\n");
    return path;
}
string Adapted(string source) => Path.Combine(Path.GetDirectoryName(source)!, ".flashback-adapted-" + Path.GetFileNameWithoutExtension(source));
(bool Ok, string Text) Adapt(string source) => FlashbackZip.RunProtocolAdapter(source, executable, fixtureJar);
void FailedAdaptation(string source, string expectedText, string name)
{
    var original = SHA256.HashData(File.ReadAllBytes(source));
    var result = Adapt(source);
    Check(!result.Ok && result.Text.Contains(expectedText, StringComparison.Ordinal), name + " rejected");
    Check(!Directory.Exists(Adapted(source)) && !Directory.EnumerateDirectories(Path.GetDirectoryName(source)!, ".flashback-adapted-*.tmp-*").Any()
        && SHA256.HashData(File.ReadAllBytes(source)).SequenceEqual(original), name + " preserves capture and leaves no partial conversion");
}
void RewriteCachedProperty(string source, string from, string to)
{
    var dir = Adapted(source);
    var file = Path.Combine(dir, "converted.properties");
    File.WriteAllText(file, File.ReadAllText(file).Replace(from, to, StringComparison.Ordinal));
    var manifest = JsonNode.Parse(File.ReadAllText(Path.Combine(dir, "manifest.json")))!.AsObject();
    manifest["Files"]!["converted.properties"] = JsonSerializer.SerializeToNode(new
    {
        Length = new FileInfo(file).Length, LastWriteUtcTicks = File.GetLastWriteTimeUtc(file).Ticks,
        Sha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(file)))
    });
    File.WriteAllText(Path.Combine(dir, "manifest.json"), manifest.ToJsonString());
}

var modern = Capture("modern", "1.21.10", 773, true);
var modernResult = Adapt(modern);
Check(modernResult.Ok && modernResult.Text.Contains("fixtureRoute=modern"), "773 uses modern adapter entry and identity");
Check(FlashbackZip.HasProtocolAdaptation(modern, fixtureJar), "modern conversion publishes valid per-recording cache");
Check(File.ReadAllBytes(Path.Combine(Adapted(modern), "snapshot-config.bin")).SequenceEqual(File.ReadAllBytes(Path.Combine(Path.GetDirectoryName(modern)!, "snapshot-config.bin"))), "modern registry configuration copied into immutable cache");
var modernArchive = FlashbackZip.Convert(modern, Path.Combine(root, "skins"));
using (var zip = ZipFile.OpenRead(modernArchive))
{
    using var document = JsonDocument.Parse(zip.GetEntry("metadata.json")!.Open());
    var metadata = document.RootElement;
    Check(metadata.GetProperty("version_string").GetString() == "1.21.11"
        && metadata.GetProperty("protocol_version").GetInt32() == 774
        && metadata.GetProperty("data_version").GetInt32() == 4671, "modern conversion ZIP advertises target 1.21.11 protocol 774");
    Check(zip.GetEntry("capture-diagnostics.json") != null && zip.GetEntry("c0.flashback")?.Length > 0
        && zip.GetEntry("level_chunk_caches/0")?.Length > 0, "modern ZIP contains timeline, cache and capture quality");
    Check(metadata.GetProperty("flashback_recorder").GetProperty("quality").GetProperty("complete").GetBoolean(), "capture quality is retained in replay metadata");
}
Check(Adapt(modern).Ok, "unchanged modern conversion cache can be reused");

string SkinManifest(string source) => Path.Combine(Path.GetDirectoryName(source)!, "recorded-skins.json");
string SkinHash(string path) => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path)));
void WriteSkinManifest(string source, string note = "a") =>
    File.WriteAllText(SkinManifest(source), "{\"version\":1,\"players\":[],\"note\":\"" + note + "\"}");
bool AdapterSawCurrentSkin(string source) => ReadProperties(Path.Combine(Adapted(source), "converted.properties"))
    .GetValueOrDefault("fixtureSkinSha256") == SkinHash(SkinManifest(source));

var modernSkins = Capture("modern-skin-manifest", "1.21.10", 773, true);
WriteSkinManifest(modernSkins);
File.SetLastWriteTimeUtc(SkinManifest(modernSkins), File.GetLastWriteTimeUtc(modernSkins).AddSeconds(30));
Check(Adapt(modernSkins).Ok && AdapterSawCurrentSkin(modernSkins), "post-stop skin flush is staged and visible to modern adapter");
Check(JsonNode.Parse(File.ReadAllText(Path.Combine(Adapted(modernSkins), "manifest.json")))!["Files"]!["recorded-skins.json"] is not null
    && FlashbackZip.HasProtocolAdaptation(modernSkins), "skin manifest fingerprint belongs to immutable conversion cache");
var cachedSkinPath = Path.Combine(Adapted(modernSkins), "recorded-skins.json");
var expectedSkin = File.ReadAllBytes(cachedSkinPath);
File.Delete(SkinManifest(modernSkins));
Check(FlashbackZip.HasProtocolAdaptation(modernSkins) && Adapt(modernSkins).Ok,
    "removed raw skin manifest does not discard immutable cache");
var replacementAdapter = Path.Combine(root, "replacement-adapter.jar");
File.WriteAllText(replacementAdapter, "updated fixture adapter");
Check(FlashbackZip.RunProtocolAdapter(modernSkins, executable, replacementAdapter).Ok
    && File.ReadAllBytes(cachedSkinPath).SequenceEqual(expectedSkin), "adapter upgrade retains immutable skin manifest when raw is absent");

var lateManifest = Capture("skin-added-after-cache", "1.21.10", 773, true);
Check(Adapt(lateManifest).Ok, "old cache without skin manifest fixture setup");
WriteSkinManifest(lateManifest);
Check(!FlashbackZip.HasProtocolAdaptation(lateManifest), "new raw skin manifest invalidates old cache lacking it");
Check(Adapt(lateManifest).Ok && AdapterSawCurrentSkin(lateManifest), "old cache is rebuilt from raw including new skin manifest");

var omittedManifest = Path.Combine(Adapted(lateManifest), "manifest.json");
var omitted = JsonNode.Parse(File.ReadAllText(omittedManifest))!;
omitted["Files"]!.AsObject().Remove("recorded-skins.json");
File.WriteAllText(omittedManifest, omitted.ToJsonString());
Check(!FlashbackZip.HasProtocolAdaptation(lateManifest), "cache requires skin manifest fingerprint even if staged file remains");
Check(Adapt(lateManifest).Ok && AdapterSawCurrentSkin(lateManifest), "cache manifest omission is repaired by fresh adaptation");

WriteSkinManifest(lateManifest, "longer");
Check(!FlashbackZip.HasProtocolAdaptation(lateManifest), "changed raw skin manifest size invalidates cached NPC profiles");
Check(Adapt(lateManifest).Ok && AdapterSawCurrentSkin(lateManifest), "changed skin manifest is copied from raw instead of old cache");
File.SetLastWriteTimeUtc(SkinManifest(lateManifest), File.GetLastWriteTimeUtc(SkinManifest(lateManifest)).AddSeconds(4));
Check(!FlashbackZip.HasProtocolAdaptation(lateManifest), "raw skin manifest timestamp is checked without hashing");
Check(Adapt(lateManifest).Ok, "timestamp-only skin manifest changes are readapted");
var unchangedTime = File.GetLastWriteTimeUtc(SkinManifest(lateManifest));
WriteSkinManifest(lateManifest, "change");
File.SetLastWriteTimeUtc(SkinManifest(lateManifest), unchangedTime);
Check(FlashbackZip.HasProtocolAdaptation(lateManifest), "lightweight cache check deliberately compares only skin size and timestamp");
Reject(() => FlashbackZip.Convert(lateManifest), "full export verifies current raw skin manifest hash");
Check(Adapt(lateManifest).Ok && AdapterSawCurrentSkin(lateManifest), "same-size same-timestamp skin content changes force readaptation");

var oldSkins = Capture("stale-skin-manifest", "1.21.10", 773, true);
WriteSkinManifest(oldSkins);
File.SetLastWriteTimeUtc(SkinManifest(oldSkins), File.GetLastWriteTimeUtc(oldSkins).AddMinutes(-1));
FailedAdaptation(oldSkins, "早于这次录制", "stale skin manifest from previous recording");
var sharedSkins = Capture("shared-skin-manifest", "1.21.10", 773, true);
WriteSkinManifest(sharedSkins);
var otherCapture = Path.Combine(Path.GetDirectoryName(sharedSkins)!, "older.fbrl");
File.Copy(sharedSkins, otherCapture);
File.SetLastWriteTimeUtc(otherCapture, File.GetLastWriteTimeUtc(sharedSkins).AddMinutes(-1));
FailedAdaptation(sharedSkins, "独占的目录", "skin manifest in shared recording directory");
var undatedSkins = Capture("undated-skin-manifest", "1.21.10", 773, true);
WriteSkinManifest(undatedSkins);
var undatedLines = File.ReadAllLines(undatedSkins);
var undatedMeta = JsonNode.Parse(undatedLines[0])!.AsObject();
undatedMeta.Remove("createdUtc"); undatedLines[0] = undatedMeta.ToJsonString();
File.WriteAllLines(undatedSkins, undatedLines);
FailedAdaptation(undatedSkins, "缺少开始时间", "skin ownership without recording start time");
var changingSkins = Capture("skin-mutates-during-adaptation", "1.21.10", 773, true);
WriteSkinManifest(changingSkins);
File.AppendAllText(Path.Combine(Path.GetDirectoryName(changingSkins)!, "capture.properties"), "fixtureMutateRawSkin=" + SkinManifest(changingSkins) + "\n");
FailedAdaptation(changingSkins, "recorded-skins.json", "skin flush during conversion");

var inferredModern = Capture("modern-inferred", "1.21.10", null, true);
Check(Adapt(inferredModern).Ok, "known modern version supplies omitted protocol");
var legacy = Capture("legacy", "1.20.1", 763);
var legacyResult = Adapt(legacy);
Check(legacyResult.Ok && legacyResult.Text.Contains("fixtureRoute=legacy"), "763 keeps legacy adapter entry without modern config");
Check(File.Exists(FlashbackZip.Convert(legacy, Path.Combine(root, "skins"))), "legacy recording still exports through adapted ZIP path");
Check(Adapt(Capture("legacy-inferred", "1.20.1", null)).Ok, "legacy version supplies omitted protocol");

var tagGroups = Capture("custom-registry-tags", "1.21.10", 773, true, "fixtureRegistryTagGroupsDropped=11\n");
Check(Adapt(tagGroups).Ok, "custom registry tag filtering conversion succeeds");
var tagQuality = FlashbackZip.ReadCaptureQuality(tagGroups);
Check(tagQuality.CustomRegistryTagGroupsDropped == 11 && tagQuality.HasCompatibilityLimitations
    && tagQuality.Status == "complete_with_compatibility_limits" && tagQuality.IsComplete,
    "filtered custom tag groups are a complete capture with compatibility limitations");
Check(!tagQuality.HasDataLoss && tagQuality.PacketErrors == 0 && tagQuality.PacketDrops == 0
    && tagQuality.PacketSkippedCustomPayload == 0, "tag filtering is separate from packet errors, drops and custom payloads");
Check(tagQuality.Summary.Contains("跳过 11 个自定义注册表标签组", StringComparison.Ordinal), "quality summary names the filtered registry group count");
using (var diagnostics = JsonDocument.Parse(tagQuality.ToJson()))
{
    var quality = diagnostics.RootElement;
    Check(quality.GetProperty("customRegistryTagGroupsDropped").GetInt32() == 11
        && quality.GetProperty("status").GetString() == "complete_with_compatibility_limits"
        && quality.GetProperty("hasCompatibilityLimitations").GetBoolean()
        && !quality.GetProperty("hasDataLoss").GetBoolean()
        && quality.GetProperty("warnings").EnumerateArray().Any(warning => warning.GetString()!.Contains("11 个自定义注册表标签组")),
        "diagnostics JSON exposes registry count and compatibility warning without data loss");
}
using (var zip = ZipFile.OpenRead(FlashbackZip.Convert(tagGroups, Path.Combine(root, "skins"))))
{
    using var metadata = JsonDocument.Parse(zip.GetEntry("metadata.json")!.Open());
    using var diagnostics = JsonDocument.Parse(zip.GetEntry("capture-diagnostics.json")!.Open());
    var quality = metadata.RootElement.GetProperty("flashback_recorder").GetProperty("quality");
    Check(quality.GetProperty("status").GetString() == "complete_with_compatibility_limits"
        && quality.GetProperty("hasCompatibilityLimitations").GetBoolean()
        && !quality.GetProperty("hasDataLoss").GetBoolean()
        && diagnostics.RootElement.GetProperty("customRegistryTagGroupsDropped").GetInt32() == 11,
        "exported metadata and diagnostics use the merged conversion quality");
}
var bothLimitations = CaptureQualityReport.FromCaptureProperties(Encoding.UTF8.GetBytes("packetSkippedCustomPayload=3\n"))
    .WithConversionProperties(Encoding.UTF8.GetBytes("customRegistryTagGroupsDropped=11\n"));
Check(bothLimitations.PacketSkippedCustomPayload == 3 && bothLimitations.CustomRegistryTagGroupsDropped == 11
    && !bothLimitations.HasDataLoss, "custom payload and registry tag counts remain independent");
var actualLoss = CaptureQualityReport.FromCaptureProperties(Encoding.UTF8.GetBytes("packetErrors=1\n"))
    .WithConversionProperties(Encoding.UTF8.GetBytes("customRegistryTagGroupsDropped=11\n"));
Check(actualLoss.HasDataLoss && actualLoss.HasCompatibilityLimitations && actualLoss.Status == "incomplete",
    "compatibility tag filtering never hides real capture errors");
var unknownCapture = CaptureQualityReport.FromCaptureProperties(null)
    .WithConversionProperties(Encoding.UTF8.GetBytes("customRegistryTagGroupsDropped=11\n"));
Check(!unknownCapture.Available && unknownCapture.Status == "unknown" && !unknownCapture.IsComplete
    && unknownCapture.Summary.Contains("11 个自定义注册表标签组"), "conversion counters do not invent capture completeness");
foreach (var invalidCount in new[] { "-1", "bad", "999999999999999" })
{
    var invalid = CaptureQualityReport.FromCaptureProperties(Encoding.UTF8.GetBytes("packetErrors=0\n"))
        .WithConversionProperties(Encoding.UTF8.GetBytes("customRegistryTagGroupsDropped=" + invalidCount + "\n"));
    Check(invalid.CustomRegistryTagGroupsDropped == 0 && !invalid.HasCompatibilityLimitations,
        "invalid registry filtering count is ignored: " + invalidCount);
}
var unboundQuality = Capture("unbound-conversion-quality", "1.21.10", 773, true);
File.WriteAllText(Path.Combine(Path.GetDirectoryName(unboundQuality)!, "converted.properties"), "customRegistryTagGroupsDropped=999\n");
Check(FlashbackZip.ReadCaptureQuality(unboundQuality).CustomRegistryTagGroupsDropped == 0,
    "unbound shared conversion properties cannot contaminate another recording's quality");

var missingProfiles = Capture("missing-player-profiles", "1.21.10", 773, true, "fixtureMissingProfiles=4\nfixtureRecoveredProfiles=6\n");
Check(Adapt(missingProfiles).Ok, "unresolved player identity diagnostics do not prevent conversion");
var missingQuality = FlashbackZip.ReadCaptureQuality(missingProfiles);
Check(missingQuality.MissingPlayerProfiles == 4 && missingQuality.Status == "incomplete" && missingQuality.HasDataLoss
    && !missingQuality.IsComplete && missingQuality.Summary.Contains("4 个玩家/NPC 缺少资料，可能不显示"),
    "unresolved player or NPC profiles are visibly incomplete");
Check(missingQuality.PacketErrors == 0 && missingQuality.PacketDrops == 0 && missingQuality.PacketSkippedCustomPayload == 0,
    "missing player identities have their own count without inflating packet counters");
using (var zip = ZipFile.OpenRead(FlashbackZip.Convert(missingProfiles, Path.Combine(root, "skins"))))
{
    using var diagnostics = JsonDocument.Parse(zip.GetEntry("capture-diagnostics.json")!.Open());
    using var metadata = JsonDocument.Parse(zip.GetEntry("metadata.json")!.Open());
    Check(diagnostics.RootElement.GetProperty("missingPlayerProfiles").GetInt32() == 4
        && diagnostics.RootElement.GetProperty("recordedProfileManifestInvalid").GetInt32() == 0
        && metadata.RootElement.GetProperty("flashback_recorder").GetProperty("quality").GetProperty("status").GetString() == "incomplete",
        "exported diagnostics and replay metadata retain missing identity quality");
}
var recoveredQuality = CaptureQualityReport.FromCaptureProperties(Encoding.UTF8.GetBytes("packetErrors=0\n"))
    .WithConversionProperties(Encoding.UTF8.GetBytes("playerProfilesRecoveredFromManifest=8\nplayerProfilesRestored=3\nplayerProfilesMissing=0\n"));
Check(recoveredQuality.Status == "complete" && !recoveredQuality.HasDataLoss && recoveredQuality.MissingPlayerProfiles == 0,
    "successfully recovered or restored profiles are not counted as data loss");
var invalidIdentity = Capture("invalid-profile-manifest", "1.21.10", 773, true, "fixtureInvalidSkinManifest=1\n");
Check(Adapt(invalidIdentity).Ok && File.Exists(FlashbackZip.Convert(invalidIdentity, Path.Combine(root, "skins"))),
    "invalid optional skin identity manifest still permits replay export");
var invalidIdentityQuality = FlashbackZip.ReadCaptureQuality(invalidIdentity);
using (var diagnostics = JsonDocument.Parse(invalidIdentityQuality.ToJson()))
{
    Check(invalidIdentityQuality.Status == "incomplete" && invalidIdentityQuality.RecordedProfileManifestInvalid == 1
        && invalidIdentityQuality.Summary.Contains("皮肤身份清单无效")
        && diagnostics.RootElement.GetProperty("recordedProfileManifestInvalid").GetInt32() == 1
        && diagnostics.RootElement.GetProperty("warnings").EnumerateArray().Any(item => item.GetString()!.Contains("皮肤身份清单无效")),
        "invalid optional identity manifest is exposed as an independent warning and count");
}
var missingWithoutCapture = CaptureQualityReport.FromCaptureProperties(null)
    .WithConversionProperties(Encoding.UTF8.GetBytes("playerProfilesMissing=2\n"));
Check(!missingWithoutCapture.Available && missingWithoutCapture.Status == "incomplete" && missingWithoutCapture.HasDataLoss,
    "known unresolved identities remain incomplete even without capture diagnostics");

FailedAdaptation(Capture("missing-config", "1.21.10", 773), "配置注册表快照", "missing modern configuration");
var emptyConfig = Capture("empty-config", "1.21.10", 773, true);
File.WriteAllBytes(Path.Combine(Path.GetDirectoryName(emptyConfig)!, "snapshot-config.bin"), Array.Empty<byte>());
FailedAdaptation(emptyConfig, "配置注册表快照", "empty modern configuration");
FailedAdaptation(Capture("unknown-protocol", "1.21.10", 999, true), "尚不支持源协议", "unknown protocol");
FailedAdaptation(Capture("unknown-version", "1.21.11", null, true), "尚不支持录制版本", "unknown version without protocol");
FailedAdaptation(Capture("conflict", "1.21.10", 763, true), "不一致", "version and protocol conflict");
FailedAdaptation(Capture("packet-errors", "1.21.10", 773, true, "fixturePacketErrors=2\n"), "动态数据包失败", "converter packet failure");
FailedAdaptation(Capture("broken-chunks", "1.21.10", 773, true, "fixtureDamage=chunk\n"), "帧头不完整", "truncated converted chunks");
FailedAdaptation(Capture("broken-dynamic", "1.21.10", 773, true, "fixtureDamage=dynamic\n"), "帧头不完整", "truncated converted dynamic packets");

var missingCachedConfig = Capture("missing-cached-config", "1.21.10", 773, true);
Check(Adapt(missingCachedConfig).Ok, "cache fixture setup");
var missingManifestPath = Path.Combine(Adapted(missingCachedConfig), "manifest.json");
var missingManifest = JsonNode.Parse(File.ReadAllText(missingManifestPath))!.AsObject();
missingManifest["Files"]!.AsObject().Remove("snapshot-config.bin");
File.WriteAllText(missingManifestPath, missingManifest.ToJsonString());
Check(!FlashbackZip.HasProtocolAdaptation(missingCachedConfig), "modern cache lacking configuration is rejected");
Reject(() => FlashbackZip.Convert(missingCachedConfig), "export cannot bypass missing cached configuration");

var wrongSource = Capture("wrong-cached-source", "1.21.10", 773, true);
Check(Adapt(wrongSource).Ok, "source protocol cache fixture setup");
RewriteCachedProperty(wrongSource, "sourceProtocol=773", "sourceProtocol=763");
Check(!FlashbackZip.HasProtocolAdaptation(wrongSource), "modern cache with wrong source protocol is rejected despite matching fingerprints");
var wrongTarget = Capture("wrong-cached-target", "1.21.10", 773, true);
Check(Adapt(wrongTarget).Ok, "target protocol cache fixture setup");
RewriteCachedProperty(wrongTarget, "targetProtocol=774", "targetProtocol=773");
Check(!FlashbackZip.HasProtocolAdaptation(wrongTarget), "modern cache with wrong target protocol is rejected despite matching fingerprints");

var nullConfig = Capture("null-cached-config", "1.21.10", 773, true);
Check(Adapt(nullConfig).Ok, "null configuration manifest fixture setup");
var nullManifestPath = Path.Combine(Adapted(nullConfig), "manifest.json");
var nullManifest = JsonNode.Parse(File.ReadAllText(nullManifestPath))!.AsObject();
nullManifest["Files"]!["snapshot-config.bin"] = null;
File.WriteAllText(nullManifestPath, nullManifest.ToJsonString());
try { Check(!FlashbackZip.HasProtocolAdaptation(nullConfig), "null configuration fingerprint rejects cache without throwing"); }
catch (Exception) { Check(false, "null configuration fingerprint rejects cache without throwing"); }

var sourceChanged = Capture("source-changed", "1.21.10", 773, true);
Check(Adapt(sourceChanged).Ok, "source mutation fixture setup");
File.AppendAllText(sourceChanged, "\n");
Check(!FlashbackZip.HasProtocolAdaptation(sourceChanged), "modified recording invalidates protocol cache");

string ModernTransfers(string name, string transitions, params (int Tick, string Dimension)[] destinations)
{
    var source = Capture(name, "1.21.10", 773, true,
        "captureFormat=5\ncaptureClosed=true\nlocalHotbarSlots=true\nfixtureTransitions=" + transitions + "\n");
    File.WriteAllBytes(Path.Combine(Path.GetDirectoryName(source)!, "runtime-configurations.bin"), new byte[] { 1, 2, 3 });
    foreach (var row in destinations)
        File.AppendAllText(source, JsonSerializer.Serialize(new { type = "sample", t = row.Tick * 50,
            tick = row.Tick, dimension = row.Dimension, x = row.Tick, y = 64, z = 1, playerEntityId = row.Tick }) + "\n");
    return source;
}
var twoTransfers = ModernTransfers("two-transfers", "110,71,minecraft:overworld;120,72,minecraft:the_nether",
    (112, "minecraft:overworld"), (122, "minecraft:the_nether"));
Check(Adapt(twoTransfers).Ok, "format5 dynamic configuration sidecar is staged with two ordered transfers");
Check(File.ReadAllBytes(Path.Combine(Adapted(twoTransfers), "runtime-configurations.bin")).SequenceEqual(new byte[] { 1, 2, 3 }),
    "dynamic configuration source is preserved in immutable adaptation cache");
var twoZip = FlashbackZip.Convert(twoTransfers, Path.Combine(root, "skins"), out var twoQuality);
var twoActions = ReadActions(twoZip);
Check(twoQuality.IsComplete && twoActions.Count(action => action.Id == 4) == 3
    && twoActions.Any(action => action.Id == 3 && action.Data.SequenceEqual(new byte[] { 12, 71 }))
    && twoActions.Any(action => action.Id == 3 && action.Data.SequenceEqual(new byte[] { 12, 72 })),
    "two completed transfers rebuild local player twice and retain both complete configurations");
var tailTransfer = ModernTransfers("tail-transfer-no-sample", "110,71,minecraft:overworld;120,72,minecraft:the_nether", (112, "minecraft:overworld"));
Check(Adapt(tailTransfer).Ok, "tail transition fixture adapts");
var tailZip = FlashbackZip.Convert(tailTransfer, Path.Combine(root, "skins"), out var tailQuality);
var tailActions = ReadActions(tailZip);
Check(tailQuality.Status == "incomplete" && tailQuality.SkippedUnsampledWorldTransitions == 1
    && tailActions.Count(action => action.Id == 4) == 2
    && !tailActions.Any(action => action.Data.SequenceEqual(new byte[] { 12, 72 }) || action.Data.SequenceEqual(new byte[] { 24, 72 })),
    "stopping before destination sample preserves prior world and removes whole tail configuration/world segment");
using (var zip = ZipFile.OpenRead(tailZip))
{
    using var report = JsonDocument.Parse(zip.GetEntry("capture-diagnostics.json")!.Open());
    Check(report.RootElement.GetProperty("skippedUnsampledWorldTransitions").GetInt32() == 1
        && report.RootElement.GetProperty("status").GetString() == "incomplete", "tail trimming is disclosed in exported diagnostics");
}
var fastTransfers = ModernTransfers("instant-intermediate-transfers", "110,71,minecraft:overworld;115,72,minecraft:the_nether;120,73,minecraft:the_end",
    (122, "minecraft:the_end"));
Check(Adapt(fastTransfers).Ok, "two unsampled intermediate worlds adapt");
var fastZip = FlashbackZip.Convert(fastTransfers, Path.Combine(root, "skins"), out var fastQuality);
var fastActions = ReadActions(fastZip);
Check(fastQuality.SkippedUnsampledWorldTransitions == 2 && fastActions.Count(action => action.Id == 4) == 2
    && fastActions.Any(action => action.Id == 3 && action.Data.SequenceEqual(new byte[] { 12, 73 }))
    && !fastActions.Any(action => action.Data.SequenceEqual(new byte[] { 12, 71 }) || action.Data.SequenceEqual(new byte[] { 12, 72 })
        || action.Data.SequenceEqual(new byte[] { 24, 71 }) || action.Data.SequenceEqual(new byte[] { 24, 72 })),
    "instant intermediate worlds remove configuration and all world packets while next complete world resumes");

var respawnBeforeTail = ModernTransfers("sampled-respawn-before-incomplete-login", "110,R71,minecraft:overworld;120,72,minecraft:the_nether",
    (112, "minecraft:overworld"));
Check(Adapt(respawnBeforeTail).Ok, "sampled ordinary respawn before incomplete LOGIN adapts");
var respawnTailZip = FlashbackZip.Convert(respawnBeforeTail, Path.Combine(root, "skins"), out var respawnTailQuality);
var respawnTailActions = ReadActions(respawnTailZip);
Check(respawnTailQuality.SkippedUnsampledWorldTransitions == 1 && respawnTailQuality.Status == "incomplete"
    && respawnTailActions.Count(action => action.Id == 4) == 2
    && respawnTailActions.Any(action => action.Id == 2 && action.Data[0] == 80)
    && respawnTailActions.Any(action => action.Data.SequenceEqual(new byte[] { 24, 71 }))
    && !respawnTailActions.Any(action => action.Data.SequenceEqual(new byte[] { 12, 72 })),
    "valid sampled Respawn survives tail LOGIN trimming without requiring configuration on Respawn");
var missingRespawn = ModernTransfers("unsampled-respawn-before-complete-login", "110,R71,minecraft:the_nether;120,72,minecraft:the_end",
    (122, "minecraft:the_end"));
Check(Adapt(missingRespawn).Ok, "unsampled Respawn followed by full LOGIN adapts");
var missingRespawnZip = FlashbackZip.Convert(missingRespawn, Path.Combine(root, "skins"), out var missingRespawnQuality);
var missingRespawnActions = ReadActions(missingRespawnZip);
Check(missingRespawnQuality.SkippedUnsampledWorldTransitions == 1
    && !missingRespawnActions.Any(action => action.Id == 2 && action.Data[0] == 80)
    && !missingRespawnActions.Any(action => action.Data.SequenceEqual(new byte[] { 24, 71 }))
    && missingRespawnActions.Any(action => action.Data.SequenceEqual(new byte[] { 12, 72 })),
    "unsampled Respawn and its world packets are removed until a fully configured sampled world resumes");
var dependentRespawn = ModernTransfers("unsampled-login-with-dependent-respawn", "110,71,minecraft:overworld;115,R72,minecraft:the_nether;120,73,minecraft:the_end",
    (117, "minecraft:the_nether"), (122, "minecraft:the_end"));
Check(Adapt(dependentRespawn).Ok, "sampled Respawn dependent on missing LOGIN fixture adapts");
var dependentZip = FlashbackZip.Convert(dependentRespawn, Path.Combine(root, "skins"), out var dependentQuality);
var dependentActions = ReadActions(dependentZip);
Check(dependentQuality.SkippedUnsampledWorldTransitions == 2 && dependentActions.Count(action => action.Id == 4) == 2
    && !dependentActions.Any(action => action.Id == 2 && action.Data[0] == 80)
    && !dependentActions.Any(action => action.Data.SequenceEqual(new byte[] { 24, 71 }) || action.Data.SequenceEqual(new byte[] { 24, 72 }))
    && dependentActions.Any(action => action.Data.SequenceEqual(new byte[] { 12, 73 })),
    "later Respawn cannot falsely restore skipped LOGIN chunks or entities; entire dependent segment is removed");
var unboundTransition = ModernTransfers("unsampled-unbound-marker", "110,X71,minecraft:the_nether");
Check(Adapt(unboundTransition).Ok, "unbound marker fixture adapts structurally");
Reject(() => FlashbackZip.Convert(unboundTransition), "missing sample without verified LOGIN or Respawn boundary still rejects export");
var immediateRespawn = ModernTransfers("login-immediate-respawn", "110,C71,minecraft:overworld;120,C72,minecraft:overworld",
    (112, "minecraft:overworld"), (122, "minecraft:overworld"));
Check(Adapt(immediateRespawn).Ok, "two same-tick LOGIN Respawn sequences adapt");
var immediateZip = FlashbackZip.Convert(immediateRespawn, Path.Combine(root, "skins"), out var immediateQuality);
var immediateActions = ReadActions(immediateZip);
Check(immediateQuality.IsComplete && immediateQuality.CoalescedTransitionMarkers == 2
    && immediateQuality.SkippedUnsampledWorldTransitions == 0 && immediateActions.Count(action => action.Id == 4) == 3
    && immediateActions.Count(action => action.Id == 2 && action.Data[0] == 48) == 2
    && immediateActions.Count(action => action.Id == 2 && action.Data[0] == 80) == 2
    && immediateActions.Any(action => action.Data.SequenceEqual(new byte[] { 24, 71 }))
    && immediateActions.Any(action => action.Data.SequenceEqual(new byte[] { 24, 72 })),
    "same-tick LOGIN Respawn coalesces only synthetic markers and preserves both complete worlds");
var dummyDimension = ModernTransfers("login-dummy-dimension-respawns", "110,C71,minecraft:overworld", (112, "minecraft:overworld"));
File.AppendAllText(Path.Combine(Path.GetDirectoryName(dummyDimension)!, "capture.properties"), "fixtureRespawnChain=2\n");
Check(Adapt(dummyDimension).Ok, "proxy dummy dimension Respawn chain adapts");
var dummyZip = FlashbackZip.Convert(dummyDimension, Path.Combine(root, "skins"), out var dummyQuality);
var dummyActions = ReadActions(dummyZip);
Check(dummyQuality.IsComplete && dummyQuality.CoalescedTransitionMarkers == 2
    && dummyActions.Count(action => action.Id == 2 && action.Data[0] == 80) == 2
    && dummyActions.Count(action => action.Id == 4) == 2,
    "same-tick dummy dimensions retain both real Respawns but create local player only in sampled final dimension");
var intervening = ModernTransfers("login-respawn-world-packet-intervenes", "110,C71,minecraft:overworld", (112, "minecraft:overworld"));
File.AppendAllText(Path.Combine(Path.GetDirectoryName(intervening)!, "capture.properties"), "fixtureInterveningPacket=true\n");
Check(Adapt(intervening).Ok, "intervening packet fixture adapts");
_ = FlashbackZip.Convert(intervening, Path.Combine(root, "skins"), out var interveningQuality);
Check(interveningQuality.CoalescedTransitionMarkers == 0 && interveningQuality.Status == "incomplete",
    "coalescing never crosses intervening world packets or invents their local-player state");

foreach (var delay in new[] { 1, 2, 75 })
{
    var delayed = ModernTransfers("login-respawn-delay-" + delay, "110,C71,minecraft:overworld", (111 + delay, "minecraft:overworld"));
    File.AppendAllText(Path.Combine(Path.GetDirectoryName(delayed)!, "capture.properties"), "fixtureRespawnDelay=" + delay + "\n");
    Check(Adapt(delayed).Ok, $"LOGIN Respawn separated by {delay} ticks adapts");
    var delayedZip = FlashbackZip.Convert(delayed, Path.Combine(root, "skins"), out var delayedQuality);
    var delayedActions = ReadActions(delayedZip);
    var position = delayedActions.Last(action => action.Id == 4).Data;
    Check(delayedQuality.IsComplete && delayedQuality.CoalescedTransitionMarkers == 1
        && delayedQuality.SkippedUnsampledWorldTransitions == 0
        && delayedActions.Count(action => action.Id == 2 && action.Data[0] == 48) == 1
        && delayedActions.Count(action => action.Id == 2 && action.Data[0] == 80) == 1
        && delayedActions.Count(action => action.Id == 1) == 12 + delay
        && BitConverter.Int64BitsToDouble(BinaryPrimitives.ReadInt64BigEndian(position.AsSpan(16))) == 111 + delay,
        $"unobserved {delay}-tick boundary preserves real packets, elapsed ticks and actual later sample position");
}
var sampledGap = ModernTransfers("login-respawn-observed-interval", "110,C71,minecraft:overworld",
    (112, "minecraft:overworld"), (116, "minecraft:overworld"));
File.AppendAllText(Path.Combine(Path.GetDirectoryName(sampledGap)!, "capture.properties"), "fixtureRespawnDelay=5\n");
Check(Adapt(sampledGap).Ok, "LOGIN Respawn with observed intermediate world adapts");
var observedZip = FlashbackZip.Convert(sampledGap, Path.Combine(root, "skins"), out var observedQuality);
Check(observedQuality.IsComplete && observedQuality.CoalescedTransitionMarkers == 0
    && ReadActions(observedZip).Count(action => action.Id == 4) == 3,
    "actual interval sample requires both local creations instead of coalescing observed state");
var destinationTickSample = ModernTransfers("sample-on-destination-respawn-tick", "110,C71,minecraft:overworld",
    (115, "minecraft:overworld"), (116, "minecraft:overworld"));
File.AppendAllText(Path.Combine(Path.GetDirectoryName(destinationTickSample)!, "capture.properties"), "fixtureRespawnDelay=5\n");
Check(Adapt(destinationTickSample).Ok, "sample on destination Respawn tick fixture adapts");
_ = FlashbackZip.Convert(destinationTickSample, Path.Combine(root, "skins"), out var destinationTickQuality);
Check(destinationTickQuality.IsComplete && destinationTickQuality.CoalescedTransitionMarkers == 1,
    "destination-tick sample is not misclassified as an intervening observed world");
var tripleDelayed = ModernTransfers("three-respawns-cross-ticks", "110,C71,minecraft:overworld", (117, "minecraft:overworld"));
File.AppendAllText(Path.Combine(Path.GetDirectoryName(tripleDelayed)!, "capture.properties"), "fixtureRespawnDelay=2\nfixtureRespawnChain=3\n");
Check(Adapt(tripleDelayed).Ok, "three delayed proxy Respawns adapt");
var tripleZip = FlashbackZip.Convert(tripleDelayed, Path.Combine(root, "skins"), out var tripleQuality);
Check(tripleQuality.IsComplete && tripleQuality.CoalescedTransitionMarkers == 3
    && ReadActions(tripleZip).Count(action => action.Id == 2 && action.Data[0] == 80) == 3
    && ReadActions(tripleZip).Count(action => action.Id == 4) == 2,
    "cross-tick dummy dimension chain retains all Respawns and creates only observed final world");
var fakeBoundary = ModernTransfers("unverified-boundary-before-real-respawn", "110,C71,minecraft:overworld", (116, "minecraft:overworld"));
File.AppendAllText(Path.Combine(Path.GetDirectoryName(fakeBoundary)!, "capture.properties"), "fixtureRespawnDelay=5\nfixtureFakeBoundary=true\n");
Check(Adapt(fakeBoundary).Ok, "unverified boundary followed by real Respawn fixture adapts structurally");
Reject(() => FlashbackZip.Convert(fakeBoundary), "later genuine Respawn never legitimizes an unverified preceding boundary");
var transitionFile = Path.Combine(Path.GetDirectoryName(twoTransfers)!, "runtime-configurations.bin");
var originalStamp = File.GetLastWriteTimeUtc(transitionFile);
File.WriteAllBytes(transitionFile, new byte[] { 3, 2, 1 }); File.SetLastWriteTimeUtc(transitionFile, originalStamp);
Reject(() => FlashbackZip.Convert(twoTransfers), "full export rejects dynamic configuration tampering with unchanged size and mtime");
Check(Adapt(twoTransfers).Ok && File.ReadAllBytes(Path.Combine(Adapted(twoTransfers), "runtime-configurations.bin")).SequenceEqual(new byte[] { 3, 2, 1 }),
    "dynamic configuration content change rebuilds from raw rather than old cache");
File.Delete(transitionFile);
Check(FlashbackZip.HasProtocolAdaptation(twoTransfers), "removed raw configuration retains verified immutable capture copy");
var delayedStreams = ModernTransfers("late-config-flush", "");
File.SetLastWriteTimeUtc(Path.Combine(Path.GetDirectoryName(delayedStreams)!, "runtime-configurations.bin"), File.GetLastWriteTimeUtc(delayedStreams).AddMinutes(1));
Check(Adapt(delayedStreams).Ok, "closed exclusive format5 capture accepts config flushed after final position sample");
var activeStreams = ModernTransfers("unclosed-config", "");
var activeProperties = Path.Combine(Path.GetDirectoryName(activeStreams)!, "capture.properties");
File.WriteAllText(activeProperties, File.ReadAllText(activeProperties).Replace("captureClosed=true", "captureClosed=false"));
FailedAdaptation(activeStreams, "尚未完成收尾", "unclosed format5 recording");
var missingTransferFile = ModernTransfers("missing-config-stream", "");
File.Delete(Path.Combine(Path.GetDirectoryName(missingTransferFile)!, "runtime-configurations.bin"));
FailedAdaptation(missingTransferFile, "缺少转服配置文件", "format5 missing configuration stream");
var unreadyLocal = ModernTransfers("missing-initial-local-state-marker", "");
var unreadyProperties = Path.Combine(Path.GetDirectoryName(unreadyLocal)!, "capture.properties");
File.WriteAllText(unreadyProperties, File.ReadAllText(unreadyProperties).Replace("localHotbarSlots=true", "localHotbarSlots=false"));
Check(Adapt(unreadyLocal).Ok, "format5 missing local state fixture adapts");
Reject(() => FlashbackZip.Convert(unreadyLocal), "format5 requires verified complete local initialization just like format4");
var boundaryQuality = CaptureQualityReport.FromCaptureProperties(Encoding.UTF8.GetBytes("packetErrors=0\n"))
    .WithConversionProperties(Encoding.UTF8.GetBytes("incompleteWorldTransitions=1\nworldTransitions=2\n"));
Check(boundaryQuality.Status == "incomplete" && boundaryQuality.IncompleteWorldTransitions == 1 && boundaryQuality.PacketErrors == 0,
    "pending server configuration is incomplete independently of packet errors");
var repeatError = CaptureQualityReport.FromCaptureProperties(Encoding.UTF8.GetBytes("captureError=configuration failure\nerror=configuration failure\nstopWarning=configuration failure\n"));
Check(repeatError.DetailWarnings.Count == 1, "same recording failure is reported once across error aliases");
var terminal = new Dictionary<string, string> { ["recording"] = "false", ["state"] = "record-error", ["agentVersion"] = "16" };
Check(RecordingLifecycle.CanExportClosed(terminal) && RecordingLifecycle.CanReload("1.21.10", true, terminal),
    "closed old modern capture can be preserved and component can be explicitly reloaded");
terminal["recording"] = "true";
Check(!RecordingLifecycle.CanExportClosed(terminal) && !RecordingLifecycle.CanReload("1.21.10", true, terminal),
    "recording cannot be exported or reloaded even when stale error remains");
terminal["recording"] = "false"; terminal["state"] = "capturing-snapshot";
Check(!RecordingLifecycle.CanExportClosed(terminal) && !RecordingLifecycle.CanReload("1.21.10", true, terminal),
    "snapshot setup is never mistaken for a closed recording");
terminal["state"] = "ready";
Check(!RecordingLifecycle.CanReload("1.20.1", true, terminal) && !RecordingLifecycle.CanReload("1.21.10", false, terminal),
    "legacy and current modern agents do not expose unintended reload");

var markers = Capture("colliding-markers", "1.21.10", 773, true);
File.AppendAllText(markers, "{\"type\":\"mark\",\"t\":50,\"name\":\"Second marker\"}\n{\"type\":\"mark\",\"t\":50,\"name\":\"Second marker\"}\n");
Check(Adapt(markers).Ok, "colliding marker fixture adapts");
using (var zip = ZipFile.OpenRead(FlashbackZip.Convert(markers)))
{
    using var document = JsonDocument.Parse(zip.GetEntry("metadata.json")!.Open());
    var metadata = document.RootElement;
    var entries = metadata.GetProperty("markers").EnumerateObject().ToArray();
    Check(entries.Length == 1 && entries[0].Name == "1"
        && entries[0].Value.GetProperty("description").GetString() == "Compatibility marker / Second marker",
        "same-tick markers merge in place without duplicate text or time drift");
    Check(entries.All(entry => long.Parse(entry.Name) < metadata.GetProperty("total_ticks").GetInt64()),
        "markers at final tick remain inside replay duration");
}

ProcessStartInfo Helper(string mode, string? pidFile = null)
{
    var info = new ProcessStartInfo(executable);
    info.ArgumentList.Add("--child-helper"); info.ArgumentList.Add(mode);
    if (pidFile != null) info.ArgumentList.Add(pidFile);
    return info;
}
bool HelperStopped(string pidFile)
{
    if (!File.Exists(pidFile)) return false;
    try { using var child = Process.GetProcessById(int.Parse(File.ReadAllText(pidFile))); return child.WaitForExit(2500); }
    catch (ArgumentException) { return true; }
}
var outputResult = await ChildProcess.RunAsync(Helper("output"), TimeSpan.FromSeconds(5));
Check(outputResult.ExitCode == 7 && outputResult.Output.Contains(new string('x', 200000))
    && outputResult.Output.Contains(new string('y', 200000)), "child drains stdout and stderr concurrently and preserves nonzero exit");
var timeoutPid = Path.Combine(root, "timeout.pid");
try { await ChildProcess.RunAsync(Helper("sleep", timeoutPid), TimeSpan.FromMilliseconds(700)); Check(false, "child timeout terminates helper"); }
catch (TimeoutException) { Check(HelperStopped(timeoutPid), "child timeout terminates helper"); }
var cancelPid = Path.Combine(root, "cancel.pid");
using (var cancellation = new CancellationTokenSource(700))
{
    try { await ChildProcess.RunAsync(Helper("sleep", cancelPid), TimeSpan.FromSeconds(20), cancellation.Token); Check(false, "child cancellation terminates helper"); }
    catch (OperationCanceledException) { Check(HelperStopped(cancelPid), "child cancellation terminates helper"); }
}
var inheritedPid = Path.Combine(root, "inherited.pid");
var drainWatch = Stopwatch.StartNew();
try
{
    try { await ChildProcess.RunAsync(Helper("inherit", inheritedPid), TimeSpan.FromSeconds(20)); Check(false, "inherited open pipe has bounded drain"); }
    catch (TimeoutException) { Check(drainWatch.Elapsed < TimeSpan.FromSeconds(4), "inherited open pipe has bounded drain"); }
}
finally
{
    if (File.Exists(inheritedPid))
        try { using var child = Process.GetProcessById(int.Parse(File.ReadAllText(inheritedPid))); child.Kill(true); child.WaitForExit(2500); }
        catch (ArgumentException) { }
}
var cancelledAdapter = Capture("cancelled-adapter", "1.21.10", 773, true, "fixtureAdapterDelayMs=30000\n");
var beforeCancellation = SHA256.HashData(File.ReadAllBytes(cancelledAdapter));
using (var cancellation = new CancellationTokenSource(700))
{
    try { await FlashbackZip.RunProtocolAdapterAsync(cancelledAdapter, executable, fixtureJar, cancellation.Token); Check(false, "adapter propagates cancellation"); }
    catch (OperationCanceledException) { Check(true, "adapter propagates cancellation"); }
}
Check(!Directory.Exists(Adapted(cancelledAdapter))
    && !Directory.EnumerateDirectories(Path.GetDirectoryName(cancelledAdapter)!, ".flashback-adapted-*.tmp-*").Any()
    && SHA256.HashData(File.ReadAllBytes(cancelledAdapter)).SequenceEqual(beforeCancellation),
    "cancelled adapter preserves raw data and leaves no published or partial conversion");

var final = new { passed, failed = failed.Count, failures = failed, artifacts = root, scope = "C# routing, child lifecycle, validation and ZIP publication; mock packets are not a playback test" };
Console.WriteLine(JsonSerializer.Serialize(final));
File.WriteAllText(Path.Combine(root, "results.json"), JsonSerializer.Serialize(final, new JsonSerializerOptions { WriteIndented = true }));
Environment.ExitCode = failed.Count == 0 ? 0 : 1;

static Dictionary<string, string> ReadProperties(string path) => File.ReadAllLines(path)
    .Where(line => line.Contains('=')).Select(line => line.Split('=', 2))
    .ToDictionary(parts => parts[0], parts => parts[1], StringComparer.Ordinal);
static byte[] Frame(int header, long tick, byte[] payload)
{
    var bytes = new byte[header + payload.Length];
    if (header == 12) BinaryPrimitives.WriteInt64BigEndian(bytes, tick);
    else if (header == 8) BinaryPrimitives.WriteInt32BigEndian(bytes, checked((int)tick));
    BinaryPrimitives.WriteInt32BigEndian(bytes.AsSpan(header - 4), payload.Length);
    payload.CopyTo(bytes, header);
    return bytes;
}

static byte[] TimelineFrame(long tick, int kind, byte[] payload)
{
    var data = new byte[16 + payload.Length];
    BinaryPrimitives.WriteInt64BigEndian(data, tick);
    BinaryPrimitives.WriteInt32BigEndian(data.AsSpan(8), kind);
    BinaryPrimitives.WriteInt32BigEndian(data.AsSpan(12), payload.Length);
    payload.CopyTo(data, 16); return data;
}

static List<(int Id, byte[] Data)> ReadActions(string path)
{
    using var zip = ZipFile.OpenRead(path); using var stream = zip.GetEntry("c0.flashback")!.Open();
    using var reader = new BinaryReader(stream);
    int Int() => BinaryPrimitives.ReadInt32BigEndian(reader.ReadBytes(4));
    int VarInt() { var result = 0; for (var shift = 0; shift < 35; shift += 7) { var next = reader.ReadByte(); result |= (next & 127) << shift; if (next < 128) return result; } throw new InvalidDataException(); }
    _ = Int(); var count = VarInt(); for (var i = 0; i < count; i++) reader.ReadBytes(VarInt());
    _ = Int(); // Snapshot size; actions have the same framing on either side.
    var actions = new List<(int, byte[])>();
    try { while (true) { var id = VarInt(); var length = Int(); actions.Add((id, reader.ReadBytes(length))); } }
    catch (EndOfStreamException) { return actions; }
}
