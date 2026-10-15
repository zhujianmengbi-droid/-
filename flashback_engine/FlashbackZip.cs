using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace FlashbackRecorder;

/// <summary>
/// Converts a runtime-recorder FBRL trace into a Flashback replay archive.
///
/// The archive writes the structural artifacts used by a Flashback replay ZIP:
/// metadata.json (identity, version, world, markers, chunks), the c0.flashback
/// chunk stream (header, action table, snapshot create_local_player, cached
/// chunks and real per-tick position actions), icon.png for the replay browser
/// and the level_chunk_caches stream. When the protocol adapter sidecars are
/// present, the archive also carries the converted 1.21.11 configuration and
/// game packets produced from a 1.20.1 or 1.21.10 capture.
///
/// A structurally valid archive is not proof that every game state was captured.
/// </summary>
internal static class FlashbackZip
{
    // A LOGIN is followed by the destination world's initial Respawn during
    // a server transfer.  Respawns much later are ordinary deaths/respawns and
    // must not move the transfer spawn to a later position sample.
    const long TransferRespawnWindowTicks = 40;
    static readonly string[] Actions =
    {
        "flashback:action/next_tick",
        "flashback:action/game_packet",
        "flashback:action/create_local_player",
        "flashback:action/move_entities",
        "flashback:action/level_chunk_cached",
        "flashback:action/accurate_player_position_optional",
    };

    static readonly string[] AdaptedActions =
    {
        "flashback:action/simple_voice_chat_sound_optional",
        "flashback:action/next_tick",
        "flashback:action/game_packet",
        "flashback:action/configuration_packet",
        "flashback:action/create_local_player",
        "flashback:action/move_entities",
        "flashback:action/level_chunk_cached",
        "flashback:action/accurate_player_position_optional",
    };

    const int IdNextTick = 0;
    const int IdGamePacket = 1;
    const int IdCreateLocalPlayer = 2;
    const int IdMoveEntities = 3;
    const int IdLevelChunkCached = 4;
    const int IdAccuratePlayerPosition = 5;

    const int AdaptedIdNextTick = 1;
    const int AdaptedIdGamePacket = 2;
    const int AdaptedIdConfigurationPacket = 3;
    const int AdaptedIdCreateLocalPlayer = 4;
    const int AdaptedIdMoveEntities = 5;
    const int AdaptedIdLevelChunkCached = 6;
    const int AdaptedIdAccuratePlayerPosition = 7;

    const int Magic = unchecked((int)0xD780E884);

    // ReplayGamePacketHandler.localPlayerId starts at -1 and is only set by a
    // login game packet. Our capture has none, so the spawned local player
    // keeps id -1 and every position action must reference that id.
    const int LocalPlayerEntityId = -1;

    const int MarkerColour = 16777215;

    sealed record EntityRow(int Id, double X, double Y, double Z, double Yaw, double Pitch,
        double HeadYaw, bool OnGround);

    sealed record SampleRow(long T, long Tick, double X, double Y, double Z, double Yaw, double Pitch,
        string Dimension, double VelX, double VelY, double VelZ, double HeadYaw, bool OnGround,
        List<EntityRow> Entities);

    sealed record PacketRow(long Tick, byte[] Data);
    sealed record TimelineRow(long Tick, int Kind, byte[] Data);
    sealed record TransferState(SampleRow Sample, string Dimension, int Gametype);

    sealed class MarkRow
    {
        public long T;
        public string Name = "";
    }

    sealed class MetaInfo
    {
        public int Format;
        public string Version = "";
        public int DataVersion = -1;
        public int ProtocolVersion = -1;
        public string WorldName = "";
        public string PlayerUuid = "";
        public string PlayerName = "";
        public int Gametype;
        public int PlayerEntityId = -1;
    }

    public static string Convert(string source, string? skinCacheDirectory = null) => Convert(source, skinCacheDirectory, out _);

    public static string Convert(string source, string? skinCacheDirectory, out CaptureQualityReport captureQuality)
    {
        var sourceFingerprint = Fingerprint(source);
        var meta = new MetaInfo();
        var samples = new List<SampleRow>();
        var marks = new List<MarkRow>();

        var lineNumber = 0;
        foreach (var line in File.ReadLines(source))
        {
            lineNumber++;
            if (string.IsNullOrWhiteSpace(line)) continue;
            try
            {
                using var doc = JsonDocument.Parse(line);
                var root = doc.RootElement;
                var type = root.TryGetProperty("type", out var t) ? t.GetString() : "";
                if (type == "meta")
                {
                    meta.Format = GetInt(root, "format", meta.Format);
                    meta.Version = GetString(root, "version", meta.Version);
                    meta.DataVersion = GetInt(root, "dataVersion", meta.DataVersion);
                    meta.ProtocolVersion = GetInt(root, "protocolVersion", meta.ProtocolVersion);
                    meta.WorldName = GetString(root, "worldName", meta.WorldName);
                    meta.PlayerUuid = GetString(root, "playerUuid", meta.PlayerUuid);
                    meta.PlayerName = GetString(root, "playerName", meta.PlayerName);
                    meta.Gametype = GetInt(root, "gametype", meta.Gametype);
                    meta.PlayerEntityId = GetInt(root, "playerEntityId", meta.PlayerEntityId);
                }
                else if (type == "sample")
                {
                    var tick = GetLong(root, "tick", long.MinValue);
                    if (tick == long.MinValue) continue;
                    samples.Add(new SampleRow(
                        GetLong(root, "t", 0), tick,
                        GetDouble(root, "x", 0), GetDouble(root, "y", 0), GetDouble(root, "z", 0),
                        GetDouble(root, "yaw", 0), GetDouble(root, "pitch", 0),
                        GetString(root, "dimension", ""),
                        GetDouble(root, "velX", 0), GetDouble(root, "velY", 0), GetDouble(root, "velZ", 0),
                        GetDouble(root, "headYaw", 0), GetBool(root, "onGround", false), ParseEntities(root)));
                }
                else if (type == "mark")
                {
                    marks.Add(new MarkRow { T = GetLong(root, "t", 0), Name = GetString(root, "name", "") });
                }
            }
            catch (JsonException ex) { throw new InvalidDataException($"录制文件第 {lineNumber} 行不完整或损坏", ex); }
        }

        if (samples.Count == 0) throw new InvalidDataException("FBRL 没有样本");
        for (var i = 1; i < samples.Count; i++)
            if (samples[i].Tick < samples[i - 1].Tick)
                throw new InvalidDataException("这条旧录制在切换世界时 tick 回退，动态数据无法可靠排序。请使用新版录制器重新录制；原文件已保留。");
        var adapted = HasProtocolAdaptation(source);
        if (!adapted && (Directory.Exists(AdaptationDirectory(source)) ||
            File.Exists(Path.Combine(Path.GetDirectoryName(source) ?? "", "converted.properties"))))
            throw new InvalidDataException("协议适配结果缺失、过期或属于另一条录制，请重新执行协议适配后导出。");
        if (adapted) ValidateAdaptation(source, null, verifyHashes: true);
        captureQuality = ReadCaptureQuality(source);
        var packetSidecar = adapted ? "converted-packets.bin" : "runtime-packets.bin";
        var packets = ReadPacketSidecar(source, packetSidecar);
        var timeline = adapted ? ReadTimelineSidecar(source) : null;
        var modernTransfers = meta.Version == "1.21.10" && meta.Format >= 5;
        if (modernTransfers && timeline != null)
            captureQuality = captureQuality with { CoalescedTransitionMarkers = CoalesceUnobservedRespawnMarkers(timeline, samples) };
        var transferStates = timeline == null ? new Dictionary<TimelineRow, TransferState>()
            : ResolveTransferStates(timeline, samples, allowMissing: modernTransfers);
        if (modernTransfers && timeline != null)
        {
            var skipped = RemoveUnsampledTransitions(timeline, samples, packets, transferStates);
            captureQuality = captureQuality with { SkippedUnsampledWorldTransitions = skipped };
        }
        var firstTick = samples.Min(s => s.Tick);
        var lastTick = samples.Max(s => s.Tick);
        if (packets.Count > 0)
        {
            firstTick = Math.Min(firstTick, packets.Min(p => p.Tick));
            lastTick = Math.Max(lastTick, packets.Max(p => p.Tick));
        }
        if (timeline is { Count: > 0 })
        {
            firstTick = Math.Min(firstTick, timeline[0].Tick);
            lastTick = Math.Max(lastTick, timeline[^1].Tick);
        }
        var duration = checked((int)(lastTick - firstTick + 1));
        if (duration <= 0 || duration > 10000000) throw new InvalidDataException("录制时长不合理");

        var byTick = samples.GroupBy(s => s.Tick).ToDictionary(g => g.Key, g => g.ToList());
        var packetsByTick = packets.GroupBy(p => p.Tick).ToDictionary(g => g.Key, g => g.ToList());
        var snapshotSample = samples[0];
        var playerUuid = ParseUuid(meta.PlayerUuid) ?? Guid.NewGuid();
        var loginPackets = ReadSidecar(source, "snapshot-packets.bin");
        var entityPackets = ReadSidecar(source, "snapshot-entities.bin");
        var worldPackets = ReadSidecar(source, adapted ? "converted-world.bin" : "snapshot-world.bin");
        var configurationPackets = adapted ? ReadSidecar(source, "converted-config.bin") : null;
        var gameSnapshotPackets = adapted ? ReadSidecar(source, "converted-game-snapshot.bin") : null;
        var convertedEntityPackets = adapted ? ReadSidecar(source, "converted-entities.bin") : null;
        var chunkPackets = ReadSidecar(source, adapted ? "converted-chunks.bin" : "snapshot-chunks.bin");
        var registrySnapshot = ReadSidecar(source, "forge_registry_snapshot.nbt");
        var initialLocalState = adapted
            ? InitialLocalState(source, meta, snapshotSample.Tick, packets) : new List<byte[]>();

        var chunk = adapted
            ? BuildAdaptedChunk(snapshotSample, meta, playerUuid, configurationPackets, gameSnapshotPackets,
                convertedEntityPackets, worldPackets, chunkPackets, initialLocalState, timeline, transferStates,
                firstTick, duration, byTick, packetsByTick)
            : BuildChunk(snapshotSample, meta, playerUuid, loginPackets, entityPackets, worldPackets, chunkPackets,
                firstTick, duration, byTick, packetsByTick);
        var output = adapted
            ? Path.Combine(Path.GetDirectoryName(source) ?? "", Path.GetFileNameWithoutExtension(source) + ".mc1.21.11.flashback.zip")
            : Path.ChangeExtension(source, ".flashback.zip");
        var cache = adapted ? BuildAdaptedCache(chunkPackets) : chunkPackets ?? Array.Empty<byte>();
        var identity = new ExportIdentity(Path.GetFileName(source), sourceFingerprint.Sha256,
            System.Convert.ToHexString(SHA256.HashData(chunk)), System.Convert.ToHexString(SHA256.HashData(cache)));
        var previousOutput = File.Exists(output) ? Fingerprint(output) : null;
        // Editor state belongs to the recording, not to an export attempt.
        // Reuse only a verified prior export of this same source.
        var replayUuid = previousOutput == null ? Guid.NewGuid() : ExistingReplayUuid(output, source, identity);
        var metadata = BuildMetadata(source, meta, replayUuid, duration, marks, samples, firstTick, lastTick, identity, adapted, captureQuality);
        var tempOutput = output + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (var archive = ZipFile.Open(tempOutput, ZipArchiveMode.Create))
            {
                WriteEntry(archive, "metadata.json", Encoding.UTF8.GetBytes(metadata), CompressionLevel.Optimal);
                WriteEntry(archive, "capture-diagnostics.json", captureQuality.ToJson(), CompressionLevel.Optimal);
                WriteEntry(archive, "c0.flashback", chunk, CompressionLevel.Optimal);
                WriteEntry(archive, "icon.png", BuildIconPng(), CompressionLevel.Optimal);
                WriteEntry(archive, "level_chunk_caches/0", cache, CompressionLevel.NoCompression);
                if (registrySnapshot is { Length: > 0 })
                    WriteEntry(archive, "forge_registry_snapshot.nbt", registrySnapshot, CompressionLevel.Optimal);
                RecordedSkins.WriteToArchive(source, archive, skinCacheDirectory);
            }
            if (Fingerprint(source) != sourceFingerprint)
                throw new IOException("录制文件在导出期间发生变化，未替换已有回放。");
            if (previousOutput == null ? File.Exists(output) : !File.Exists(output) || Fingerprint(output) != previousOutput)
                throw new IOException("目标回放在导出期间发生变化，未覆盖。");
            try { File.Move(tempOutput, output, overwrite: true); }
            catch (IOException) when (File.Exists(output))
            {
                var baseName = Path.GetFileName(output);
                if (baseName.EndsWith(".flashback.zip", StringComparison.OrdinalIgnoreCase))
                    baseName = baseName[..^".flashback.zip".Length] + "-" + Guid.NewGuid().ToString("N")[..8] + ".flashback.zip";
                else
                    baseName = Path.GetFileNameWithoutExtension(output) + "-" + Guid.NewGuid().ToString("N")[..8] + ".zip";
                output = Path.Combine(Path.GetDirectoryName(output) ?? "", baseName);
                File.Move(tempOutput, output);
            }
        }
        finally { if (File.Exists(tempOutput)) File.Delete(tempOutput); }
        return output;
    }

    internal static CaptureQualityReport ReadCaptureQuality(string source) =>
        CaptureQualityReport.FromCaptureProperties(ReadSidecar(source, "capture.properties"))
            .WithConversionProperties(HasProtocolAdaptation(source) ? ReadSidecar(source, "converted.properties") : null);

    sealed record ExportIdentity(string SourceName, string SourceSha256, string ChunkSha256, string CacheSha256);

    static Guid ExistingReplayUuid(string output, string source, ExportIdentity identity)
    {
        try
        {
            using var archive = ZipFile.OpenRead(output);
            ZipArchiveEntry Entry(string name)
            {
                var matches = archive.Entries.Where(e => string.Equals(e.FullName, name, StringComparison.OrdinalIgnoreCase)).ToArray();
                if (matches.Length != 1 || matches[0].FullName != name)
                    throw new InvalidDataException("已有回放缺少必需文件，或文件名重复。");
                return matches[0];
            }
            var metadataEntry = Entry("metadata.json");
            if (metadataEntry.Length > 4 * 1024 * 1024) throw new InvalidDataException("已有回放元数据过大。");
            using var metadataStream = metadataEntry.Open();
            using var document = JsonDocument.Parse(metadataStream);
            var root = document.RootElement;
            if (!root.TryGetProperty("uuid", out var uuidNode) || uuidNode.ValueKind != JsonValueKind.String
                || !Guid.TryParse(uuidNode.GetString(), out var uuid))
                throw new InvalidDataException("已有回放 UUID 无效。");
            string EntryHash(string name)
            {
                using var input = Entry(name).Open();
                return System.Convert.ToHexString(SHA256.HashData(input));
            }
            var oldChunkHash = EntryHash("c0.flashback");
            var oldCacheHash = EntryHash("level_chunk_caches/0");
            if (root.TryGetProperty("flashback_recorder", out var origin))
            {
                if (origin.ValueKind != JsonValueKind.Object
                    || GetInt(origin, "version", -1) != 1
                    || !string.Equals(GetString(origin, "source_file", ""), identity.SourceName, StringComparison.OrdinalIgnoreCase)
                    || GetString(origin, "source_sha256", "") != identity.SourceSha256)
                    throw new InvalidDataException("已有回放不属于当前录制，未覆盖。");
                if (GetString(origin, "chunk_sha256", "") != oldChunkHash || GetString(origin, "cache_sha256", "") != oldCacheHash)
                    throw new InvalidDataException("已有回放内容校验失败，未覆盖损坏文件。");
            }
            else if (GetString(root, "name", "") != Path.GetFileNameWithoutExtension(source)
                || oldChunkHash != identity.ChunkSha256 || oldCacheHash != identity.CacheSha256)
                throw new InvalidDataException("无法确认旧版回放属于当前录制，已保留原文件；请先移走或重命名旧 ZIP。");
            // Validate embedded recorded assets too, before replacing an older
            // archive whose replay identity and editing history will be reused.
            _ = RecordedSkins.CountInReplay(output);
            return uuid;
        }
        catch (Exception ex) when (ex is IOException or JsonException or InvalidOperationException or FormatException)
        {
            throw new InvalidDataException("已有回放无法安全重用，已保留原文件：" + ex.Message, ex);
        }
    }

    const string ReplayPlayerFile = "replay-player.properties";
    const string RecordedSkinManifest = "recorded-skins.json";
    const string RuntimeConfigurations = "runtime-configurations.bin";
    static readonly string[] CaptureFiles = { "snapshot-config.bin", "snapshot-packets.bin", "snapshot-chunks.bin", "snapshot-entities.bin",
        "snapshot-world.bin", "runtime-packets.bin", RuntimeConfigurations, "forge_registry_snapshot.nbt", "capture.properties", RecordedSkinManifest, ReplayPlayerFile };
    static readonly string[] ConvertedFiles = { "converted.properties", "converted-config.bin",
        "converted-game-snapshot.bin", "converted-chunks.bin", "converted-entities.bin", "converted-packets.bin" };
    static readonly string[] OptionalConvertedFiles = { "converted-world.bin", "converted-timeline.bin" };
    const int AdaptationManifestVersion = 1;
    sealed record FileFingerprint(long Length, long LastWriteUtcTicks, string Sha256);
    sealed record AdaptationManifest(int Version, string SourceName, FileFingerprint Source,
        string AdapterSha256, Dictionary<string, FileFingerprint> Files);

    static string AdaptationDirectory(string source) => Path.Combine(Path.GetDirectoryName(Path.GetFullPath(source))!,
        ".flashback-adapted-" + Path.GetFileNameWithoutExtension(source));

    static FileFingerprint Fingerprint(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        var hash = System.Convert.ToHexString(SHA256.HashData(stream));
        var info = new FileInfo(path);
        return new(info.Length, info.LastWriteTimeUtc.Ticks, hash);
    }

    // This inexpensive check is also safe to call from UI status polling.
    // Convert and RunProtocolAdapter perform the full hash verification off the UI thread.
    public static bool HasProtocolAdaptation(string source, string? adapterJar = null)
    {
        try { ValidateAdaptation(source, adapterJar, verifyHashes: false); return true; }
        catch (Exception ex) when (ex is IOException or InvalidDataException or UnauthorizedAccessException or JsonException
            or InvalidOperationException or ArgumentException) { return false; }
    }

    static void ValidateAdaptation(string source, string? adapterJar, bool verifyHashes)
    {
        var dir = AdaptationDirectory(source);
        var manifest = JsonSerializer.Deserialize<AdaptationManifest>(File.ReadAllText(Path.Combine(dir, "manifest.json")))
            ?? throw new InvalidDataException("协议适配清单为空");
        if (manifest.Source == null || manifest.Files == null || manifest.Version != AdaptationManifestVersion
            || manifest.SourceName != Path.GetFileName(source))
            throw new InvalidDataException("协议适配结果不属于这条录制");
        VerifyFile(source, manifest.Source, verifyHashes);
        if (adapterJar != null && Fingerprint(adapterJar).Sha256 != manifest.AdapterSha256)
            throw new InvalidDataException("协议适配器已更新，需要重新转换");
        foreach (var name in ConvertedFiles)
            if (!manifest.Files.ContainsKey(name)) throw new InvalidDataException("协议适配结果缺少 " + name);
        foreach (var name in new[] { "snapshot-packets.bin", "snapshot-chunks.bin" })
            if (!manifest.Files.ContainsKey(name)) throw new InvalidDataException("协议适配输入缺少 " + name);
        if (ReadSourceProtocol(source) == 773)
        {
            if (!manifest.Files.TryGetValue("snapshot-config.bin", out var config) || config is null || config.Length <= 0)
                throw new InvalidDataException("1.21.10 协议适配缺少配置注册表快照。");
            var properties = File.ReadAllLines(Path.Combine(dir, "converted.properties"));
            if (!properties.Contains("sourceProtocol=773") || !properties.Contains("targetProtocol=774"))
                throw new InvalidDataException("1.21.10 协议适配版本不匹配。");
            if (ValidateClosedModernCapture(dir) && !manifest.Files.ContainsKey(RuntimeConfigurations))
                throw new InvalidDataException("格式 5 协议适配输入缺少转服配置文件指纹，请重新适配。");
            VerifyCurrentModernSidecars(source, manifest.Files, verifyHashes);
        }
        if (manifest.Files.ContainsKey("snapshot-world.bin") && !manifest.Files.ContainsKey("converted-world.bin"))
            throw new InvalidDataException("协议适配结果缺少初始计分板、时间与天气数据");
        if (File.ReadLines(Path.Combine(dir, "converted.properties")).Any(line => line.Trim() == "timelineFormat=1")
            && !manifest.Files.ContainsKey("converted-timeline.bin"))
            throw new InvalidDataException("协议适配结果缺少有序配置与转世界时间线。");
        foreach (var file in manifest.Files)
        {
            if (!CaptureFiles.Contains(file.Key) && !ConvertedFiles.Contains(file.Key) && !OptionalConvertedFiles.Contains(file.Key))
                throw new InvalidDataException("协议适配清单存在未知文件");
            if (file.Value == null) throw new InvalidDataException("协议适配清单文件校验值为空");
            VerifyFile(Path.Combine(dir, file.Key), file.Value, verifyHashes);
        }
    }

    static void VerifyFile(string path, FileFingerprint expected, bool verifyHash)
    {
        var info = new FileInfo(path);
        if (!info.Exists || info.Length != expected.Length || info.LastWriteTimeUtc.Ticks != expected.LastWriteUtcTicks
            || (verifyHash && Fingerprint(path).Sha256 != expected.Sha256))
            throw new InvalidDataException("录制或转换文件已改变，请重新适配：" + Path.GetFileName(path));
    }

    static void VerifyCurrentModernSidecars(string source, Dictionary<string, FileFingerprint> files, bool verifyHashes)
    {
        // The immutable cache remains usable after the raw sidecar was removed.
        // While present, late flushes must invalidate older profiles/configuration.
        foreach (var name in new[] { RecordedSkinManifest, RuntimeConfigurations, "capture.properties" })
        {
            var raw = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(source))!, name);
            if (!File.Exists(raw)) continue;
            if (!files.TryGetValue(name, out var expected) || expected is null)
                throw new InvalidDataException("协议适配输入缺少当前录制的采集文件，请重新适配：" + name);
            VerifyFile(raw, expected, verifyHashes);
        }
    }

    static void CheckCaptureOwnership(string source)
    {
        var dir = Path.GetDirectoryName(Path.GetFullPath(source))!;
        var info = new FileInfo(source);
        DateTime? recordedStartUtc = null;
        foreach (var line in File.ReadLines(source))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            using var json = JsonDocument.Parse(line);
            if (json.RootElement.TryGetProperty("createdUtc", out var created)
                && created.ValueKind == JsonValueKind.String && created.TryGetDateTimeOffset(out var started))
                recordedStartUtc = started.UtcDateTime;
            break;
        }
        // Older agents overwrote sidecars in one session directory. Only its
        // latest recording can still own those files; an older FBRL cannot be recovered by guessing.
        var captures = Directory.EnumerateFiles(dir, "*.fbrl").ToArray();
        var format5 = ValidateClosedModernCapture(dir);
        if (captures.Any(path => !Path.GetFullPath(path).Equals(info.FullName,
                StringComparison.OrdinalIgnoreCase) && File.GetLastWriteTimeUtc(path) > info.LastWriteTimeUtc))
            throw new InvalidDataException("旧会话的采集文件已被下一次录制覆盖，不能用于这条旧录制。请使用已导出的回放或重新录制。");
        if (format5 || File.Exists(Path.Combine(dir, RecordedSkinManifest)))
        {
            if (captures.Length != 1 || !Path.GetFullPath(captures[0]).Equals(info.FullName, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("皮肤清单必须位于这条录制独占的目录，不能复用共享会话中的皮肤。");
            if (!recordedStartUtc.HasValue)
                throw new InvalidDataException("录制缺少开始时间，无法验证皮肤清单是否属于这次录制。");
        }
        foreach (var name in CaptureFiles)
        {
            if (name == ReplayPlayerFile) continue; // Derived from the FBRL, never adopted from shared sidecars.
            var sidecar = new FileInfo(Path.Combine(dir, name));
            // Stop appends diagnostics after closing the FBRL. If the player
            // left the world earlier, the trace's last write can be much older.
            var closedDynamicFile = format5 && name is "runtime-packets.bin" or RuntimeConfigurations;
            if (sidecar.Exists && name != "capture.properties" && name != RecordedSkinManifest && !closedDynamicFile
                && sidecar.LastWriteTimeUtc > info.LastWriteTimeUtc.AddSeconds(2))
                throw new InvalidDataException("采集文件比录制更新，可能仍在录制或已被覆盖：" + name);
            if (sidecar.Exists && recordedStartUtc.HasValue && sidecar.LastWriteTimeUtc < recordedStartUtc.Value.AddSeconds(-2))
                throw new InvalidDataException("采集文件早于这次录制，可能是上次快照残留：" + name);
        }
    }

    static bool ValidateClosedModernCapture(string directory)
    {
        var file = Path.Combine(directory, "capture.properties");
        if (!File.Exists(file)) return false;
        var properties = File.ReadLines(file).Select(line => line.Trim().Split('=', 2))
            .Where(parts => parts.Length == 2).GroupBy(parts => parts[0])
            .ToDictionary(group => group.Key, group => group.Last()[1]);
        if (properties.GetValueOrDefault("captureFormat") != "5") return false;
        if (properties.GetValueOrDefault("captureClosed") != "true")
            throw new InvalidDataException("录制文件尚未完成收尾，请停止录制后再导出。");
        if (!File.Exists(Path.Combine(directory, RuntimeConfigurations)))
            throw new InvalidDataException("格式 5 录制缺少转服配置文件：" + RuntimeConfigurations);
        return true;
    }

    public static (bool Ok, string Text) RunProtocolAdapter(string source, string javaPath, string adapterJar)
        => RunProtocolAdapterAsync(source, javaPath, adapterJar).GetAwaiter().GetResult();

    public static async Task<(bool Ok, string Text)> RunProtocolAdapterAsync(string source, string javaPath,
        string adapterJar, CancellationToken cancellation = default)
    {
        cancellation.ThrowIfCancellationRequested();
        if (!File.Exists(javaPath)) return (false, "Java 17+ 不存在");
        if (!File.Exists(adapterJar)) return (false, "协议适配器组件不存在");
        string? staging = null;
        try
        {
            var existingInput = HasProtocolAdaptation(source) ? AdaptationDirectory(source) : null;
            // Size/mtime checks above are cheap for UI polling; on the worker,
            // also rebuild if a raw skin manifest changed without a timestamp change.
            if (existingInput != null && ReadSourceProtocol(source) == 773)
                foreach (var name in new[] { RecordedSkinManifest, RuntimeConfigurations, "capture.properties" })
                {
                    var raw = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(source))!, name);
                    if (File.Exists(raw) && Fingerprint(raw) != Fingerprint(Path.Combine(existingInput, name)))
                    { existingInput = null; break; }
                }
            if (existingInput != null && HasProtocolAdaptation(source, adapterJar)
                && File.Exists(Path.Combine(AdaptationDirectory(source), ReplayPlayerFile)))
            {
                ValidateAdaptation(source, adapterJar, verifyHashes: true);
                return (true, "已验证此录制专用的协议适配结果");
            }
            // Rebuilding with a new adapter can use a previous immutable copy
            // even if a legacy agent has overwritten the shared session sidecars.
            if (existingInput != null) ValidateAdaptation(source, null, verifyHashes: true);
            else CheckCaptureOwnership(source);
            var inputDir = existingInput ?? Path.GetDirectoryName(Path.GetFullPath(source))!;
            var before = Fingerprint(source);
            var adapterHash = Fingerprint(adapterJar).Sha256;
            staging = AdaptationDirectory(source) + ".tmp-" + Guid.NewGuid().ToString("N");
            Directory.CreateDirectory(staging);
            foreach (var name in CaptureFiles)
            {
                cancellation.ThrowIfCancellationRequested();
                if (name == ReplayPlayerFile) continue;
                var input = Path.Combine(inputDir, name);
                if (File.Exists(input))
                {
                    var original = Fingerprint(input);
                    File.Copy(input, Path.Combine(staging, name));
                    if (Fingerprint(Path.Combine(staging, name)).Sha256 != original.Sha256)
                        throw new IOException("录制数据正在变化，请停止录制后再导出：" + name);
                }
            }
            WriteReplayPlayerIdentity(source, Path.Combine(staging, ReplayPlayerFile));
            foreach (var name in new[] { "snapshot-packets.bin", "snapshot-chunks.bin" })
                if (!File.Exists(Path.Combine(staging, name)) || new FileInfo(Path.Combine(staging, name)).Length == 0)
                    throw new InvalidDataException("缺少录制的初始世界数据：" + name);
            var psi = new ProcessStartInfo(javaPath)
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            psi.ArgumentList.Add("-cp");
            psi.ArgumentList.Add(adapterJar);
            var sourceProtocol = ReadSourceProtocol(source);
            if (sourceProtocol == 773 && (!File.Exists(Path.Combine(staging, "snapshot-config.bin"))
                || new FileInfo(Path.Combine(staging, "snapshot-config.bin")).Length == 0))
                throw new InvalidDataException("1.21.10 录制缺少配置注册表快照，不能安全转换。");
            psi.ArgumentList.Add(sourceProtocol == 773 ? "ModernFlashbackProtocolAdapter" : "FlashbackProtocolAdapter");
            psi.ArgumentList.Add(staging);
            var result = await ChildProcess.RunAsync(psi, TimeSpan.FromSeconds(120), cancellation).ConfigureAwait(false);
            if (result.ExitCode != 0) return (false, result.Output);
            if (Fingerprint(source) != before)
                throw new IOException("转换期间录制文件发生变化，请停止录制后重试");
            foreach (var name in ConvertedFiles)
                if (!File.Exists(Path.Combine(staging, name))) throw new InvalidDataException("适配器未生成 " + name);
            if (File.Exists(Path.Combine(staging, "snapshot-world.bin")) && !File.Exists(Path.Combine(staging, "converted-world.bin")))
                throw new InvalidDataException("适配器未生成初始计分板、时间与天气数据");
            if (File.ReadLines(Path.Combine(staging, "converted.properties")).Any(line => line.Trim() == "timelineFormat=1")
                && !File.Exists(Path.Combine(staging, "converted-timeline.bin")))
                throw new InvalidDataException("适配器未生成有序配置与转世界时间线。");
            var errorProperty = File.ReadLines(Path.Combine(staging, "converted.properties"))
                .FirstOrDefault(line => line.StartsWith("dynamicPacketErrors=", StringComparison.Ordinal));
            if (errorProperty != null && int.TryParse(errorProperty[(errorProperty.IndexOf('=') + 1)..], out var packetErrors)
                && packetErrors > 0)
                throw new InvalidDataException($"协议转换有 {packetErrors} 个动态数据包失败，未发布不完整回放。\n" + result.Output);
            foreach (var name in new[] { "converted-config.bin", "converted-game-snapshot.bin", "converted-chunks.bin" })
                if (EnumerateFramedPayloads(File.ReadAllBytes(Path.Combine(staging, name))).Count() == 0)
                    throw new InvalidDataException("适配器生成的世界数据为空：" + name);
            _ = EnumerateFramedPayloads(File.ReadAllBytes(Path.Combine(staging, "converted-entities.bin"))).Count();
            if (File.Exists(Path.Combine(staging, "converted-world.bin")))
                _ = EnumerateFramedPayloads(File.ReadAllBytes(Path.Combine(staging, "converted-world.bin"))).Count();
            _ = ReadPacketSidecarAt(Path.Combine(staging, "converted-packets.bin"));
            if (File.Exists(Path.Combine(staging, "converted-timeline.bin")))
                _ = ReadTimelineSidecarAt(Path.Combine(staging, "converted-timeline.bin"));
            var fingerprints = CaptureFiles.Concat(ConvertedFiles).Concat(OptionalConvertedFiles).Where(name => File.Exists(Path.Combine(staging, name)))
                .ToDictionary(name => name, name => Fingerprint(Path.Combine(staging, name)));
            if (sourceProtocol == 773) VerifyCurrentModernSidecars(source, fingerprints, verifyHashes: true);
            var manifest = new AdaptationManifest(AdaptationManifestVersion, Path.GetFileName(source), before, adapterHash, fingerprints);
            File.WriteAllText(Path.Combine(staging, "manifest.json"), JsonSerializer.Serialize(manifest));
            var destination = AdaptationDirectory(source);
            cancellation.ThrowIfCancellationRequested();
            // Keep any previous successful conversion until the replacement is ready.
            var previous = Directory.Exists(destination) ? destination + ".previous-" + Guid.NewGuid().ToString("N") : null;
            if (previous != null) Directory.Move(destination, previous);
            try { Directory.Move(staging, destination); staging = null; }
            catch { if (previous != null) Directory.Move(previous, destination); throw; }
            if (previous != null)
            {
                try { Directory.Delete(previous, recursive: true); }
                catch (IOException) { /* Successful replacement remains valid if old files are still open. */ }
                catch (UnauthorizedAccessException) { }
            }
            return (true, result.Output);
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested) { throw; }
        catch (Exception ex) { return (false, ex.Message); }
        finally
        {
            if (staging != null && Directory.Exists(staging))
            {
                // This is always the fresh directory created by this invocation.
                try { Directory.Delete(staging, recursive: true); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }

    static int ReadSourceProtocol(string source)
    {
        var first = File.ReadLines(source).FirstOrDefault(line => !string.IsNullOrWhiteSpace(line))
            ?? throw new InvalidDataException("FBRL 没有录制元数据。");
        using var document = JsonDocument.Parse(first);
        var meta = document.RootElement;
        if (GetString(meta, "type", "") != "meta")
            throw new InvalidDataException("FBRL 缺少开头的录制元数据。");
        var version = GetString(meta, "version", "");
        var expected = version switch { "1.20.1" => 763, "1.21.10" => 773, _ => -1 };
        if (expected < 0)
            throw new InvalidDataException($"尚不支持录制版本 {version}，已保留原录制。");
        var protocol = GetInt(meta, "protocolVersion", expected);
        if (protocol is not (763 or 773))
            throw new InvalidDataException($"尚不支持源协议 {protocol}，已保留原录制。");
        if (protocol != expected)
            throw new InvalidDataException($"录制版本 {version} 与源协议 {protocol} 不一致，已保留原录制。");
        return protocol;
    }

    static void WriteReplayPlayerIdentity(string source, string destination)
    {
        // Source identity belongs to the captured player. It must never come
        // from metadata.json's replay UUID or the complemented replay entity id.
        foreach (var line in File.ReadLines(source))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            using var document = JsonDocument.Parse(line);
            var meta = document.RootElement;
            if (!meta.TryGetProperty("type", out var type) || type.GetString() != "meta")
                throw new InvalidDataException("FBRL 缺少开头的录制元数据，无法确定录制玩家身份。");
            if (!meta.TryGetProperty("playerEntityId", out var idValue) || idValue.ValueKind != JsonValueKind.Number
                || !idValue.TryGetInt32(out var sourcePlayerId) || sourcePlayerId < 0)
                throw new InvalidDataException("FBRL 缺少有效的源玩家实体 ID，不能安全适配本地玩家重生与回放。");
            var uuidText = GetString(meta, "playerUuid", "");
            if (!Guid.TryParse(uuidText, out var playerUuid) || playerUuid == Guid.Empty)
                throw new InvalidDataException("FBRL 缺少有效的玩家 UUID，不能安全适配本地玩家资料。");
            var playerName = GetString(meta, "playerName", "");
            if (string.IsNullOrWhiteSpace(playerName))
                throw new InvalidDataException("FBRL 缺少玩家名称，不能安全适配本地玩家资料。");
            File.WriteAllText(destination,
                "sourcePlayerId=" + sourcePlayerId.ToString(System.Globalization.CultureInfo.InvariantCulture) + "\n"
                + "playerUuid=" + playerUuid.ToString("D") + "\n"
                + "playerNameBase64=" + System.Convert.ToBase64String(Encoding.UTF8.GetBytes(playerName)) + "\n"
                + "gametype=" + Math.Clamp(GetInt(meta, "gametype", 0), 0, 3).ToString(System.Globalization.CultureInfo.InvariantCulture) + "\n"
                + "sourceProtocol=" + ReadSourceProtocol(source).ToString(System.Globalization.CultureInfo.InvariantCulture) + "\n",
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            return;
        }
        throw new InvalidDataException("FBRL 没有录制元数据。");
    }

    // ---------------------------------------------------------------- chunk

    static byte[]? ReadSidecar(string source, string fileName)
    {
        var dir = HasProtocolAdaptation(source) ? AdaptationDirectory(source) : Path.GetDirectoryName(source) ?? "";
        var path = Path.Combine(dir, fileName);
        return File.Exists(path) ? File.ReadAllBytes(path) : null;
    }

    static List<PacketRow> ReadPacketSidecar(string source, string fileName)
    {
        var dir = HasProtocolAdaptation(source) ? AdaptationDirectory(source) : Path.GetDirectoryName(source) ?? "";
        return ReadPacketSidecarAt(Path.Combine(dir, fileName));
    }

    static List<PacketRow> ReadPacketSidecarAt(string path)
    {
        var result = new List<PacketRow>();
        if (!File.Exists(path)) return result;
        using var stream = File.OpenRead(path);
        using var reader = new BinaryReader(stream, Encoding.UTF8, leaveOpen: false);
        while (stream.Position < stream.Length)
        {
            if (stream.Length - stream.Position < 12) throw new InvalidDataException("动态数据帧头不完整：" + Path.GetFileName(path));
            var tick = ReadInt64BigEndian(reader);
            var length = ReadInt32BigEndian(reader);
            if (length < 0 || length > stream.Length - stream.Position)
                throw new InvalidDataException("动态数据帧长度损坏：" + Path.GetFileName(path));
            if (length == 0) continue;
            result.Add(new PacketRow(tick, reader.ReadBytes(length)));
        }
        return result;
    }

    static List<TimelineRow>? ReadTimelineSidecar(string source)
    {
        var path = Path.Combine(AdaptationDirectory(source), "converted-timeline.bin");
        return File.Exists(path) ? ReadTimelineSidecarAt(path) : null;
    }

    static List<TimelineRow> ReadTimelineSidecarAt(string path)
    {
        var result = new List<TimelineRow>();
        using var stream = File.OpenRead(path);
        using var reader = new BinaryReader(stream, Encoding.UTF8, leaveOpen: false);
        while (stream.Position < stream.Length)
        {
            if (stream.Length - stream.Position < 16) throw new InvalidDataException("转世界时间线帧头不完整。");
            var tick = ReadInt64BigEndian(reader);
            var kind = ReadInt32BigEndian(reader);
            var length = ReadInt32BigEndian(reader);
            if (tick < 0 || (result.Count > 0 && tick < result[^1].Tick))
                throw new InvalidDataException("转世界时间线 tick 回退。");
            if (kind is < 0 or > 2 || length <= 0 || length > stream.Length - stream.Position)
                throw new InvalidDataException("转世界时间线帧类型或长度无效。");
            var data = reader.ReadBytes(length);
            if (kind == 2) ParseTransferMarker(data);
            result.Add(new TimelineRow(tick, kind, data));
        }
        return result;
    }

    static (int Gametype, string Dimension) ParseTransferMarker(byte[] data)
    {
        if (data.Length < 3 || data[0] > 3) throw new InvalidDataException("转世界标记的游戏模式或数据长度无效。");
        var cursor = 1;
        var length = ReadPacketVarInt(data, ref cursor);
        if (length <= 0 || length != data.Length - cursor) throw new InvalidDataException("转世界维度长度无效或包含尾部数据。");
        string dimension;
        try { dimension = new UTF8Encoding(false, true).GetString(data, cursor, length); }
        catch (DecoderFallbackException ex) { throw new InvalidDataException("转世界维度不是有效 UTF-8。", ex); }
        if (!TryDimensionKey(dimension, out var key) || key != dimension)
            throw new InvalidDataException("转世界维度资源键无效。");
        return (data[0], dimension);
    }

    static int CoalesceUnobservedRespawnMarkers(List<TimelineRow> timeline, List<SampleRow> samples)
    {
        bool VerifiedBoundary(int index)
        {
            if (index == 0 || timeline[index - 1].Kind != 0 || timeline[index - 1].Tick != timeline[index].Tick)
                return false;
            var previous = index - 1;
            var id = PacketId(timeline[previous].Data);
            if (id == 80) return TryReadRespawnDimension(timeline[previous].Data, out var dimension)
                && dimension == ParseTransferMarker(timeline[index].Data).Dimension;
            // The modern adapter inserts one local PlayerInfo immediately after LOGIN.
            if (id == 68 && previous > 0) previous--;
            if (timeline[previous].Kind != 0 || PacketId(timeline[previous].Data) != 48
                || timeline[previous].Tick != timeline[index].Tick) return false;
            return previous > 0 && timeline[previous - 1].Kind == 1
                && timeline[previous - 1].Tick == timeline[index].Tick;
        }
        var count = 0;
        for (var i = 0; i + 2 < timeline.Count; i++)
        {
            var marker = timeline[i]; var respawn = timeline[i + 1]; var destination = timeline[i + 2];
            if (marker.Kind != 2 || respawn.Kind != 0 || destination.Kind != 2
                || respawn.Tick < marker.Tick || respawn.Tick != destination.Tick || !VerifiedBoundary(i)
                || samples.Any(sample => sample.Tick > marker.Tick && sample.Tick < respawn.Tick)
                || !TryReadRespawnDimension(respawn.Data, out var dimension)
                || dimension != ParseTransferMarker(destination.Data).Dimension) continue;
            // LOGIN -> Respawn (and proxy dummy-dimension Respawns) may be one
            // unobserved transition even if packet handling crosses tick boundaries.
            // Require no intervening sample or real packet; no elapsed-time
            // threshold can establish that invariant. Preserve the actual ticks
            // and every real packet in order, and create
            // the local entity only after the final Respawn, using its actual
            // later destination sample. No world packet may intervene here.
            timeline.RemoveAt(i);
            count++;
        }
        return count;
    }

    static Dictionary<TimelineRow, TransferState> ResolveTransferStates(List<TimelineRow> timeline, List<SampleRow> samples, bool allowMissing = false)
    {
        var result = new Dictionary<TimelineRow, TransferState>();
        // Keep the source position of each marker.  A transfer marker and the
        // following Respawn packet can share a tick, and ordering between the
        // recorder's sample queue and packet queue is not stable enough to
        // recover that relationship from ticks alone.
        var markers = timeline.Select((row, index) => (row, index))
            .Where(item => item.row.Kind == 2).ToList();
        for (var i = 0; i < markers.Count; i++)
        {
            var marker = markers[i].row;
            var (gametype, dimension) = ParseTransferMarker(marker.Data);
            var nextTransition = i + 1 < markers.Count ? markers[i + 1].row.Tick : long.MaxValue;
            var sampleAfter = marker.Tick;
            // A LOGIN alone does not mean the client has entered the new
            // server world.  On a repeated transfer to the same dimension the
            // first samples after LOGIN may still be flushed from the old
            // level.  A matching-dimension Respawn is the earliest ordered
            // boundary we have in the converted stream; require a strictly
            // later sample.  A Respawn into another dimension belongs to a
            // later in-server world change and must not delay this marker.
            for (var j = markers[i].index + 1; j < timeline.Count; j++)
            {
                var row = timeline[j];
                if (row.Tick >= nextTransition || row.Kind == 2
                    || row.Tick - marker.Tick > TransferRespawnWindowTicks) break;
                if (row.Kind != 0) continue;
                var cursor = 0;
                if (ReadPacketVarInt(row.Data, ref cursor) == 80
                    && TryReadRespawnDimension(row.Data, out var respawnDimension)
                    && respawnDimension == dimension)
                {
                    sampleAfter = Math.Max(sampleAfter, row.Tick);
                    break;
                }
            }
            // Samples and queued packets are flushed by different parts of the
            // recorder loop, so a later sample is preferred.  At a fast sample
            // rate the first destination sample can land on the exact Respawn
            // tick, however, and the next sample may already belong to the
            // following transfer.  Accept that same-tick sample only when we
            // observed a matching Respawn boundary; without it, same-tick data
            // could still be a stale sample from the old world.
            var sample = samples.FirstOrDefault(row => row.Tick > sampleAfter && row.Tick < nextTransition
                && TryDimensionKey(row.Dimension, out var key) && key == dimension);
            if (sample == null && sampleAfter > marker.Tick)
                sample = samples.FirstOrDefault(row => row.Tick == sampleAfter && row.Tick < nextTransition
                    && TryDimensionKey(row.Dimension, out var key) && key == dimension);
            if (sample == null)
            {
                if (allowMissing) continue;
                throw new InvalidDataException($"tick {marker.Tick} 转入 {dimension} 后，没有在 Respawn 边界之后、下一次转场之前采到该世界的人物位置；原录制保留。");
            }
            result.Add(marker, new TransferState(sample, dimension, gametype));
        }
        return result;
    }

    static int RemoveUnsampledTransitions(List<TimelineRow> timeline, List<SampleRow> samples,
        List<PacketRow> packets, Dictionary<TimelineRow, TransferState> states)
    {
        var markers = timeline.Select((row, index) => (row, index)).Where(item => item.row.Kind == 2).ToArray();
        if (markers.All(item => states.ContainsKey(item.row))) return 0;
        int FullConfigurationStart(int i)
        {
            var previous = i == 0 ? -1 : markers[i - 1].index;
            var join = -1;
            for (var j = markers[i].index - 1; j > previous; j--)
                if (timeline[j].Kind == 0 && PacketId(timeline[j].Data) == 48)
                { join = j; break; }
            if (join < 0 || timeline[join].Tick != markers[i].row.Tick) return -1;
            return timeline.FindIndex(previous + 1, join - previous - 1,
                row => row.Kind == 1 && row.Tick == markers[i].row.Tick);
        }
        int RemovalStart(int i)
        {
            var start = FullConfigurationStart(i);
            if (start >= 0) return start;
            // Ordinary Respawn has no configuration phase. Its packet is the
            // ordered boundary, but a subsequent Respawn alone cannot restore
            // chunks/entities discarded with this incomplete world segment.
            var previous = markers[i].index - 1;
            if (previous >= 0 && timeline[previous].Kind == 0
                && timeline[previous].Tick == markers[i].row.Tick
                && TryReadRespawnDimension(timeline[previous].Data, out var dimension)
                && dimension == ParseTransferMarker(markers[i].row.Data).Dimension)
                return previous;
            throw new InvalidDataException("未采样转服缺少可验证的配置登录或重生边界，不能安全保留后续片段；原录制已保留。");
        }
        var ranges = new List<(int Start, int End, long FromTick, long ToTick)>();
        var skipped = 0;
        // Remove complete configuration/world segments, never just their local-player marker.
        // A later retained world starts with its own complete config and LOGIN.
        for (var i = 0; i < markers.Length; i++)
        {
            if (states.ContainsKey(markers[i].row)) continue;
            var start = RemovalStart(i);
            var next = i + 1;
            var end = timeline.Count;
            for (; next < markers.Length; next++)
                if (states.ContainsKey(markers[next].row) && FullConfigurationStart(next) is var resume && resume >= 0)
                { end = resume; break; }
            ranges.Add((start, end, timeline[start].Tick, next < markers.Length ? timeline[end].Tick : long.MaxValue));
            skipped += next - i;
            i = next - 1;
        }
        foreach (var range in ranges.AsEnumerable().Reverse())
        {
            timeline.RemoveRange(range.Start, range.End - range.Start);
            samples.RemoveAll(row => row.Tick >= range.FromTick && row.Tick < range.ToTick);
            packets.RemoveAll(row => row.Tick >= range.FromTick && row.Tick < range.ToTick);
        }
        if (samples.Count == 0) throw new InvalidDataException("未采到任何已完成世界的人物位置，不能生成回放；原录制已保留。");
        return skipped;
    }

    static int PacketId(byte[] packet) { var cursor = 0; return ReadPacketVarInt(packet, ref cursor); }

    static bool TryReadRespawnDimension(byte[] data, out string dimension)
    {
        dimension = "";
        var cursor = 0;
        if (ReadPacketVarInt(data, ref cursor) != 80) return false;
        // The adapted 1.21 play Respawn packet starts with the dimension type
        // registry id, followed by the destination dimension resource key.
        ReadPacketVarInt(data, ref cursor);
        var length = ReadPacketVarInt(data, ref cursor);
        if (length <= 0 || length > data.Length - cursor)
            throw new InvalidDataException("Respawn 数据包的维度长度无效。");
        try { dimension = new UTF8Encoding(false, true).GetString(data, cursor, length); }
        catch (DecoderFallbackException ex) { throw new InvalidDataException("Respawn 数据包的维度不是有效 UTF-8。", ex); }
        return TryDimensionKey(dimension, out var key) && key == dimension;
    }

    static List<byte[]> InitialLocalState(string source, MetaInfo meta, long firstSampleTick, List<PacketRow> packets)
    {
        var properties = Encoding.UTF8.GetString(ReadSidecar(source, "capture.properties") ?? Array.Empty<byte>())
            .Split('\n').Select(line => line.TrimEnd('\r').Split('=', 2)).Where(parts => parts.Length == 2)
            .GroupBy(parts => parts[0]).ToDictionary(group => group.Key, group => group.Last()[1]);
        if (properties.GetValueOrDefault("captureFormat") is not ("4" or "5")) return new List<byte[]>();
        if (properties.GetValueOrDefault("localHotbarSlots") != "true")
            throw new InvalidDataException("格式 4/5 录制缺少本地快捷栏采集标记，不能可靠重建首帧。");
        var start = -1;
        for (var i = 0; i < packets.Count; i++)
        {
            var packet = packets[i];
            var cursor = 0;
            var id = ReadPacketVarInt(packet.Data, ref cursor);
            if (packet.Tick > firstSampleTick) break;
            if (id is 48 or 80)
                throw new InvalidDataException("首次本地状态采集前已切换世界，无法可靠重建首帧；原始录制保留。");
            if (packet.Tick == firstSampleTick && id == 20 && ReadLocalSlot(packet.Data) == 0)
            { start = i; break; }
        }
        if (start < 0) throw new InvalidDataException("首次采样缺少本地快捷栏初始化批次，不能用后续状态代替首帧。");
        var result = new List<byte[]>();
        var index = start;
        byte[] Next(int expectedId)
        {
            if (index >= packets.Count || packets[index].Tick != firstSampleTick)
                throw new InvalidDataException("首次采样的本地状态不完整，不能从其他 tick 拼凑首帧。");
            var data = packets[index++].Data;
            var cursor = 0;
            if (ReadPacketVarInt(data, ref cursor) != expectedId)
                throw new InvalidDataException("首次采样的本地初始化批次不完整或顺序异常。");
            result.Add(data);
            return data;
        }
        for (var slot = 0; slot < 9; slot++)
            if (ReadLocalSlot(Next(20)) != slot)
                throw new InvalidDataException("首次采样缺少完整的 9 格本地快捷栏。");
        var selected = Next(103);
        var selectedCursor = 0;
        ReadPacketVarInt(selected, ref selectedCursor);
        if (ReadPacketVarInt(selected, ref selectedCursor) is < 0 or > 8)
            throw new InvalidDataException("首帧选中的快捷栏位置不合法。");
        Next(101); // experience
        Next(102); // health, food and saturation
        foreach (var (flag, packetId) in new[] { ("localEquipment", 100), ("localMetadata", 97) })
        {
            if (properties.GetValueOrDefault(flag) != "true") continue;
            var data = Next(packetId);
            var cursor = 0;
            ReadPacketVarInt(data, ref cursor);
            if (meta.PlayerEntityId < 0 || ReadPacketVarInt(data, ref cursor) != ~meta.PlayerEntityId)
                throw new InvalidDataException("首帧装备或人物状态不属于录制玩家，拒绝混入远端实体状态。");
        }
        return result;
    }

    static int ReadLocalSlot(byte[] data)
    {
        var cursor = 0;
        if (ReadPacketVarInt(data, ref cursor) != 20) return -1;
        var container = ReadPacketVarInt(data, ref cursor);
        ReadPacketVarInt(data, ref cursor); // state id
        if (cursor + 2 > data.Length) throw new InvalidDataException("本地快捷栏数据不完整。");
        return container == 0 ? BinaryPrimitives.ReadInt16BigEndian(data.AsSpan(cursor, 2)) : -1;
    }

    static int ReadPacketVarInt(byte[] data, ref int cursor)
    {
        var result = 0;
        for (var shift = 0; shift < 35; shift += 7)
        {
            if (cursor >= data.Length) throw new InvalidDataException("数据包 VarInt 不完整。");
            var value = data[cursor++];
            if (shift == 28 && (value & 0xF0) != 0)
                throw new InvalidDataException("数据包 VarInt 第五字节溢出。");
            result |= (value & 127) << shift;
            if ((value & 128) == 0) return result;
        }
        throw new InvalidDataException("数据包 VarInt 长度不合法。");
    }

    static long ReadInt64BigEndian(BinaryReader reader)
    {
        var bytes = reader.ReadBytes(8);
        if (bytes.Length != 8) throw new EndOfStreamException();
        if (BitConverter.IsLittleEndian) Array.Reverse(bytes);
        return BitConverter.ToInt64(bytes, 0);
    }

    static int ReadInt32BigEndian(BinaryReader reader)
    {
        var bytes = reader.ReadBytes(4);
        if (bytes.Length != 4) throw new EndOfStreamException();
        return BinaryPrimitives.ReadInt32BigEndian(bytes);
    }

    static byte[] BuildChunk(SampleRow snapshot, MetaInfo meta, Guid playerUuid, byte[]? loginPackets,
        byte[]? entityPackets, byte[]? worldPackets, byte[]? chunkPackets,
        long firstTick, int duration, Dictionary<long, List<SampleRow>> byTick,
        Dictionary<long, List<PacketRow>> packetsByTick)
    {
        // create_local_player does not carry an entity id. Without a login
        // packet Flashback keeps localPlayerId at -1, so synthetic motion
        // records must use -1 as well.
        var entityId = loginPackets is { Length: > 0 } && meta.PlayerEntityId >= 0
            ? meta.PlayerEntityId : LocalPlayerEntityId;
        using var stream = new MemoryStream();
        using (var writer = new BinaryWriter(stream, Encoding.UTF8, leaveOpen: true))
        {
            WriteInt(writer, Magic);
            WriteVarInt(writer, Actions.Length);
            foreach (var action in Actions)
            {
                var bytes = Encoding.UTF8.GetBytes(action);
                WriteVarInt(writer, bytes.Length);
                writer.Write(bytes);
            }

            var snapshotRecords = new List<byte[]>();
            if (loginPackets is { Length: > 0 })
                snapshotRecords.Add(BuildRecord(IdGamePacket, loginPackets));
            foreach (var packet in EnumerateLengthPrefixedPayloads(entityPackets))
                snapshotRecords.Add(BuildRecord(IdGamePacket, packet));
            foreach (var packet in EnumerateLengthPrefixedPayloads(worldPackets))
                snapshotRecords.Add(BuildRecord(IdGamePacket, packet));
            snapshotRecords.Add(BuildRecord(IdCreateLocalPlayer, BuildCreateLocalPlayer(snapshot, meta, playerUuid)));
            foreach (var index in EnumerateChunkIndexes(chunkPackets))
                snapshotRecords.Add(BuildRecord(IdLevelChunkCached, BuildVarInt(index)));
            var snapshotBytes = snapshotRecords.SelectMany(bytes => bytes).ToArray();
            WriteInt(writer, snapshotBytes.Length);
            writer.Write(snapshotBytes);

            for (var tick = firstTick; tick < firstTick + duration; tick++)
            {
                if (packetsByTick.TryGetValue(tick, out var packets))
                    foreach (var packet in packets)
                        writer.Write(BuildRecord(IdGamePacket, packet.Data));
                if (byTick.TryGetValue(tick, out var rows) && rows.Count > 0)
                {
                    if (TryDimensionKey(rows[^1].Dimension, out var key))
                        writer.Write(BuildRecord(IdMoveEntities, BuildMoveEntities(rows[^1], key, entityId)));
                    // These are 20 Hz tick samples, not render partial-tick samples.
                    // A one-entry accurate-position list overrides normal interpolation
                    // in Flashback and visibly snaps the spectator camera every tick.
                }
                writer.Write(BuildRecord(IdNextTick, Array.Empty<byte>()));
            }
            writer.Flush();
        }
        return stream.ToArray();
    }

    static byte[] BuildAdaptedChunk(SampleRow snapshot, MetaInfo meta, Guid playerUuid,
        byte[]? configurationPackets, byte[]? gameSnapshotPackets, byte[]? entityPackets, byte[]? worldPackets, byte[]? chunkPackets,
        List<byte[]> initialLocalState,
        List<TimelineRow>? timeline, Dictionary<TimelineRow, TransferState> transferStates,
        long firstTick, int duration, Dictionary<long, List<SampleRow>> byTick,
        Dictionary<long, List<PacketRow>> packetsByTick)
    {
        // The adapter applies Flashback ReplayInputStream semantics to Join Game:
        // it complements the server id and switches the replay to spectator mode.
        var entityId = meta.PlayerEntityId >= 0 ? ~meta.PlayerEntityId : LocalPlayerEntityId;
        var playerInfos = new ReplayPlayerInfoLifetimes();
        var scoreboardResets = BuildScoreboardResets(EnumerateFramedPayloads(gameSnapshotPackets)
            .Concat(EnumerateFramedPayloads(worldPackets)).Concat(EnumerateFramedPayloads(entityPackets))
            .Concat(timeline != null ? timeline.Where(row => row.Kind == 0).Select(row => row.Data)
                : packetsByTick.Values.SelectMany(rows => rows).Select(row => row.Data)));
        var destructionResets = BuildBlockDestructionResets(EnumerateFramedPayloads(gameSnapshotPackets)
            .Concat(EnumerateFramedPayloads(worldPackets)).Concat(EnumerateFramedPayloads(entityPackets))
            .Concat(timeline != null ? timeline.Where(row => row.Kind == 0).Select(row => row.Data)
                : packetsByTick.Values.SelectMany(rows => rows).Select(row => row.Data)));
        using var stream = new MemoryStream();
        using (var writer = new BinaryWriter(stream, Encoding.UTF8, leaveOpen: true))
        {
            WriteInt(writer, Magic);
            WriteVarInt(writer, AdaptedActions.Length);
            foreach (var action in AdaptedActions)
            {
                var bytes = Encoding.UTF8.GetBytes(action);
                WriteVarInt(writer, bytes.Length);
                writer.Write(bytes);
            }

            var snapshotRecords = new List<byte[]>();
            var initialDestruction = new List<byte[]>();
            void SnapshotGame(byte[] packet, bool deferDestruction = false)
            {
                var cursor = 0;
                if (deferDestruction && ReadPacketVarInt(packet, ref cursor) == 5)
                { initialDestruction.Add(packet); return; }
                foreach (var normalized in playerInfos.Rewrite(packet))
                    snapshotRecords.Add(BuildRecord(AdaptedIdGamePacket, normalized));
            }
            foreach (var packet in EnumerateFramedPayloads(configurationPackets))
                snapshotRecords.Add(BuildRecord(AdaptedIdConfigurationPacket, packet));
            // Register the local profile before rewriting any snapshot packet.
            // A PlayerInfoRemove can legally precede the first AddEntity; the
            // rewriter must still recognize our UUID as the local profile.
            playerInfos.SetLocalPlayer(entityId, playerUuid, snapshot.Dimension);
            var initialGamePackets = EnumerateFramedPayloads(gameSnapshotPackets).ToList();
            var allRecordedDimensions = byTick.Values.SelectMany(rows => rows).Select(row => row.Dimension)
                .Concat(timeline is null ? Enumerable.Empty<string>() : timeline
                    .Where(row => row.Kind == 2).Select(row => ParseTransferMarker(row.Data).Dimension));
            foreach (var packet in BuildSnapshotSeekBridge(initialGamePackets, entityId,
                allRecordedDimensions))
                snapshotRecords.Add(BuildRecord(AdaptedIdGamePacket, packet));
            foreach (var packet in initialGamePackets)
                SnapshotGame(packet, deferDestruction: true);
            snapshotRecords.Add(BuildRecord(AdaptedIdCreateLocalPlayer, BuildCreateLocalPlayer(snapshot, meta, playerUuid)));
            playerInfos.SetLocalPlayer(entityId, playerUuid, snapshot.Dimension);
            // A same-world seek clears replay entities, not the viewer's
            // vanilla sound channels. Stop prior playback before initial audio.
            SnapshotGame(new byte[] { 117, 0 }); // STOP_SOUND, no source/name filter
            foreach (var packet in scoreboardResets) SnapshotGame(packet);
            foreach (var packet in EnumerateFramedPayloads(worldPackets))
                SnapshotGame(packet, deferDestruction: true);
            var cacheCount = 0;
            foreach (var packet in EnumerateFramedPayloads(chunkPackets))
                snapshotRecords.Add(BuildRecord(AdaptedIdLevelChunkCached, BuildVarInt(cacheCount++)));
            foreach (var packet in destructionResets) SnapshotGame(packet);
            foreach (var packet in initialDestruction) SnapshotGame(packet);
            foreach (var packet in initialLocalState)
                SnapshotGame(packet);
            foreach (var packet in EnumerateFramedPayloads(entityPackets))
                SnapshotGame(packet);
            var snapshotBytes = snapshotRecords.SelectMany(bytes => bytes).ToArray();
            WriteInt(writer, snapshotBytes.Length);
            writer.Write(snapshotBytes);

            var timelineByTick = timeline?.GroupBy(row => row.Tick).ToDictionary(group => group.Key, group => group.ToList());
            var currentDimension = TryDimensionKey(snapshot.Dimension, out var initialDimension) ? initialDimension : "";
            var resumeMovementTick = long.MinValue;
            var waitingForTransferSample = false;
            void TimelineGame(byte[] packet)
            {
                foreach (var normalized in playerInfos.Rewrite(packet))
                    writer.Write(BuildRecord(AdaptedIdGamePacket, normalized));
                var cursor = 0;
                if (timeline is not null && ReadPacketVarInt(packet, ref cursor) == 48)
                {
                    // Force the local entity to be recreated when a server
                    // reuses its id. RemoveEntities discards the current
                    // level's entity; CreateLocalPlayer then spawns it again.
                    var remove = BuildRemoveEntities(entityId);
                    foreach (var normalized in playerInfos.Rewrite(remove))
                        writer.Write(BuildRecord(AdaptedIdGamePacket, normalized));
                    foreach (var reset in scoreboardResets) writer.Write(BuildRecord(AdaptedIdGamePacket, reset));
                    foreach (var reset in destructionResets) writer.Write(BuildRecord(AdaptedIdGamePacket, reset));
                    writer.Write(BuildRecord(AdaptedIdGamePacket, new byte[] { 117, 0 }));
                }
            }
            for (var tick = firstTick; tick < firstTick + duration; tick++)
            {
                if (timelineByTick != null)
                {
                    if (timelineByTick.TryGetValue(tick, out var actions))
                    {
                        foreach (var action in actions)
                        {
                            if (action.Kind == 0) TimelineGame(action.Data);
                            else if (action.Kind == 1) writer.Write(BuildRecord(AdaptedIdConfigurationPacket, action.Data));
                            else
                            {
                                var transfer = transferStates[action];
                                var playerMeta = new MetaInfo { PlayerName = meta.PlayerName, Gametype = transfer.Gametype };
                                writer.Write(BuildRecord(AdaptedIdCreateLocalPlayer,
                                    BuildCreateLocalPlayer(transfer.Sample, playerMeta, playerUuid)));
                                playerInfos.SetLocalPlayer(entityId, playerUuid, transfer.Dimension);
                                currentDimension = transfer.Dimension;
                                resumeMovementTick = transfer.Sample.Tick;
                                waitingForTransferSample = true;
                            }
                        }
                    }
                }
                else if (packetsByTick.TryGetValue(tick, out var packets))
                    foreach (var packet in packets)
                        TimelineGame(packet.Data);
                if (byTick.TryGetValue(tick, out var rows) && rows.Count > 0)
                {
                    if (TryDimensionKey(rows[^1].Dimension, out var key)
                        && (!waitingForTransferSample || (tick >= resumeMovementTick && key == currentDimension)))
                    {
                        writer.Write(BuildRecord(AdaptedIdMoveEntities, BuildMoveEntities(rows[^1], key, entityId)));
                        waitingForTransferSample = false;
                    }
                }
                writer.Write(BuildRecord(AdaptedIdNextTick, Array.Empty<byte>()));
            }
            writer.Flush();
        }
        return stream.ToArray();
    }

    static List<byte[]> BuildSnapshotSeekBridge(List<byte[]> snapshotPackets, int localEntityId,
        IEnumerable<string> capturedDimensions)
    {
        var logins = snapshotPackets.Where(packet =>
        { var offset = 0; return ReadPacketVarInt(packet, ref offset) == 48; }).ToList();
        // Older diagnostic captures without a LOGIN have no world transition
        // contract to repair. Never manufacture registry or spawn information.
        if (logins.Count == 0) return new List<byte[]>();
        if (logins.Count != 1) throw new InvalidDataException("初始快照包含多个登录包，无法确定回拖重建的世界。");
        var login = logins[0]; var cursor = 0;
        int Number() => ReadPacketVarInt(login, ref cursor);
        int FixedInt()
        {
            if (login.Length - cursor < 4)
                throw new InvalidDataException("初始世界的人物 ID 字段不完整。");
            var value = BinaryPrimitives.ReadInt32BigEndian(login.AsSpan(cursor, 4));
            cursor += 4;
            return value;
        }
        void Skip(int count)
        {
            if (count < 0 || count > login.Length - cursor)
                throw new InvalidDataException("回拖世界信息不完整。");
            cursor += count;
        }
        byte Flag()
        {
            if (cursor >= login.Length || login[cursor] > 1)
                throw new InvalidDataException("回拖世界信息包含无效布尔字段。");
            return login[cursor++];
        }
        string Dimension()
        {
            var length = Number(); var start = cursor; Skip(length);
            string value;
            try { value = new UTF8Encoding(false, true).GetString(login, start, length); }
            catch (DecoderFallbackException ex) { throw new InvalidDataException("世界名称不是有效 UTF-8。", ex); }
            if (!TryDimensionKey(value, out var key) || key != value)
                throw new InvalidDataException("回拖世界名称不合法。");
            return value;
        }
        Number(); // LOGIN
        // ClientboundLogin/Join Game encodes the entity ID as a signed fixed
        // width int. The remaining fields are network VarInts/strings; using
        // the generic VarInt reader here shifts every field for negative IDs.
        if (FixedInt() != localEntityId)
            throw new InvalidDataException("初始世界的本地人物 ID 与录制信息不一致。");
        Flag(); // hardcore
        var count = Number();
        if (count < 0 || count > login.Length - cursor)
            throw new InvalidDataException("初始世界列表损坏。");
        var usedKeys = new HashSet<string>(StringComparer.Ordinal);
        for (var i = 0; i < count; i++) usedKeys.Add(Dimension());
        Number(); Number(); Number(); // max players, view and simulation distances
        Flag(); Flag(); Flag(); // reduced debug, respawn screen, limited crafting
        var commonStart = cursor;
        if (Number() < 0) throw new InvalidDataException("世界类型注册 ID 不合法。");
        var dimensionStart = cursor;
        usedKeys.Add(Dimension());
        var dimensionEnd = cursor;
        Skip(8 + 2); // seed and current/previous game modes
        Flag(); Flag(); // debug, flat
        if (Flag() != 0) { usedKeys.Add(Dimension()); Skip(8); } // optional last death GlobalPos
        Number(); Number(); // portal cooldown and sea level
        var commonEnd = cursor;
        Flag(); // secure chat belongs to LOGIN, not CommonPlayerSpawnInfo
        if (cursor != login.Length) throw new InvalidDataException("初始世界信息包含未知尾部字段。");
        foreach (var dimension in capturedDimensions)
            if (TryDimensionKey(dimension, out var key)) usedKeys.Add(key);
        const string bridgeBase = "flashback_recorder:seek_reset";
        var bridge = bridgeBase;
        for (var suffix = 1; usedKeys.Contains(bridge); suffix++) bridge = bridgeBase + "_" + suffix;

        // Flashback 0.39.10 can close the viewer's old level while replaying a
        // snapshot, then recreate the same dimension before the deferred move.
        // Vanilla's same-key teleport leaves that viewer in the closed object.
        // This temporary level forces a real cross-world move first. The
        // original LOGIN closes it again, so every later seek can recreate it.
        using var spawn = new MemoryStream();
        using (var writer = new BinaryWriter(spawn, Encoding.UTF8, leaveOpen: true))
        {
            WriteVarInt(writer, 80); // RESPAWN
            writer.Write(login.AsSpan(commonStart, dimensionStart - commonStart));
            WriteString(writer, bridge);
            writer.Write(login.AsSpan(dimensionEnd, commonEnd - dimensionEnd));
            writer.Write((byte)0); // no previous entity data to keep
        }
        using var remove = new MemoryStream();
        using (var writer = new BinaryWriter(remove, Encoding.UTF8, leaveOpen: true))
        {
            // Remove only the old recorded self after a level exists. Removing
            // before the bridge would dereference a null level on first open.
            WriteVarInt(writer, 75); WriteVarInt(writer, 1); WriteVarInt(writer, localEntityId);
        }
        return new List<byte[]> { spawn.ToArray(), remove.ToArray() };
    }

    static List<byte[]> BuildBlockDestructionResets(IEnumerable<byte[]> packets)
    {
        // LevelRenderer retains destruction progress by breaker ID across a
        // replay seek. Cancellation removes that ID and its old position entry;
        // the packet's position is ignored, including for negative replay IDs.
        // Include all recorded IDs, even already-cancelled or future-only ones,
        // because a backward seek can begin before their later cancellation.
        var ids = new SortedSet<int>();
        foreach (var packet in packets)
        {
            var cursor = 0;
            if (ReadPacketVarInt(packet, ref cursor) != 5) continue;
            var entity = ReadPacketVarInt(packet, ref cursor);
            if (packet.Length - cursor != 9)
                throw new InvalidDataException("方块挖掘裂纹数据损坏：需要位置和进度字段且不能含多余数据。");
            ids.Add(entity);
        }
        var result = new List<byte[]>();
        foreach (var id in ids)
        {
            using var output = new MemoryStream(); using var writer = new BinaryWriter(output);
            WriteVarInt(writer, 5); WriteVarInt(writer, id);
            writer.Write(new byte[8]); writer.Write((byte)255);
            result.Add(output.ToArray());
        }
        return result;
    }

    static List<byte[]> BuildScoreboardResets(IEnumerable<byte[]> packets)
    {
        // Flashback forwards scoreboard packets to the viewer's persistent
        // connection. Seeking resets entities, but not that scoreboard. Team
        // CREATE reuses an existing team and adds members without clearing old
        // ones; its objective mixin replaces only names recreated in snapshot.
        // Reset only names belonging to this recording before replaying snapshot
        // state or a new LOGIN, preserving every original subsequent packet.
        var names = new SortedSet<(int Id, string Name)>(Comparer<(int Id, string Name)>.Create((a, b) =>
        { var order = a.Id.CompareTo(b.Id); return order != 0 ? order : StringComparer.Ordinal.Compare(a.Name, b.Name); }));
        foreach (var packet in packets)
        {
            var cursor = 0; var id = ReadPacketVarInt(packet, ref cursor);
            if (id is not (104 or 107)) continue;
            var length = ReadPacketVarInt(packet, ref cursor);
            if (length < 0 || length >= packet.Length - cursor) throw new InvalidDataException("队伍或计分板名称字段损坏。");
            string name;
            try { name = new UTF8Encoding(false, true).GetString(packet, cursor, length); }
            catch (DecoderFallbackException ex) { throw new InvalidDataException("队伍或计分板名称不是有效 UTF-8。", ex); }
            var mode = packet[cursor + length];
            if (mode > (id == 107 ? 4 : 2)) throw new InvalidDataException("队伍或计分板操作类型不合法。");
            names.Add((id, name));
        }
        var result = new List<byte[]>();
        foreach (var (id, name) in names)
        {
            using var output = new MemoryStream(); using var writer = new BinaryWriter(output);
            WriteVarInt(writer, id); WriteString(writer, name); writer.Write((byte)1);
            result.Add(output.ToArray());
        }
        return result;
    }

    // 1.21.11 PlayerInfoRemove removes the profile required by the vanilla
    // AddEntity decoder. Servers also use it merely to hide live NPCs from tab.
    // Flashback can start tracking those NPCs again after a seek, at which point
    // the client rejects their spawn. Keep the profile while its entity lives,
    // express tab hiding with UPDATE_LISTED, and defer real removal until despawn.
    sealed class ReplayPlayerInfoLifetimes
    {
        const int PlayerEntityType = 155; // EntityTypes1_21_11.PLAYER
        readonly Dictionary<int, Guid> entities = new();
        readonly HashSet<Guid> deferred = new();
        // Keep the local profile known across LOGIN.  Flashback clears its
        // entity map while rebuilding a transferred world, but a remove packet
        // from the old connection can arrive before CreateLocalPlayer.
        Guid profileUuid;
        bool hasProfile;
        int localId = -1;
        string dimension = "";

        public void SetLocalPlayer(int id, Guid uuid, string world)
        {
            profileUuid = uuid; hasProfile = true;
            localId = id; entities[id] = uuid;
            dimension = TryDimensionKey(world, out var key) ? key : "";
        }

        public IEnumerable<byte[]> Rewrite(byte[] packet)
        {
            var cursor = 0;
            var id = ReadPacketVarInt(packet, ref cursor);
            if (id == 48) // A fresh connection replaces all entity identities.
            {
                entities.Clear(); deferred.Clear(); localId = -1; dimension = "";
            }
            else if (id == 1)
            {
                var entity = ReadPacketVarInt(packet, ref cursor);
                var uuid = ReadNetworkUuid(packet, ref cursor);
                var type = ReadPacketVarInt(packet, ref cursor);
                // Entity IDs can be reused by a non-player too.
                entities.Remove(entity);
                if (type == PlayerEntityType) entities[entity] = uuid;
            }
            else if (id == 68 && deferred.Count > 0)
            {
                foreach (var uuid in AddedPlayerInfoUuids(packet)) deferred.Remove(uuid);
            }
            else if (id == 67)
            {
                var count = ReadPacketVarInt(packet, ref cursor);
                if (count < 0 || count > (packet.Length - cursor) / 16)
                    throw new InvalidDataException("玩家资料移除列表损坏。");
                var removed = new List<Guid>(); var hidden = new List<Guid>();
                for (var i = 0; i < count; i++)
                {
                    var uuid = ReadNetworkUuid(packet, ref cursor);
                    if ((hasProfile && uuid == profileUuid) || entities.ContainsValue(uuid))
                    { deferred.Add(uuid); hidden.Add(uuid); }
                    else { deferred.Remove(uuid); removed.Add(uuid); }
                }
                if (cursor != packet.Length) throw new InvalidDataException("玩家资料移除列表存在多余数据。");
                if (removed.Count > 0) yield return PlayerInfoPacket(removed, hide: false);
                if (hidden.Count > 0) yield return PlayerInfoPacket(hidden, hide: true);
                yield break;
            }
            else if (id == 75)
            {
                var count = ReadPacketVarInt(packet, ref cursor);
                if (count < 0 || count > packet.Length - cursor) throw new InvalidDataException("实体移除列表损坏。");
                for (var i = 0; i < count; i++) entities.Remove(ReadPacketVarInt(packet, ref cursor));
                if (cursor != packet.Length) throw new InvalidDataException("实体移除列表存在多余数据。");
            }
            else if (id == 80)
            {
                ReadPacketVarInt(packet, ref cursor); // CommonPlayerSpawnInfo dimension-type registry id.
                var length = ReadPacketVarInt(packet, ref cursor);
                if (length < 0 || length > packet.Length - cursor) throw new InvalidDataException("重生维度信息不完整。");
                var nextDimension = Encoding.UTF8.GetString(packet, cursor, length);
                if (!TryDimensionKey(nextDimension, out var key) || key != nextDimension)
                    throw new InvalidDataException("重生维度名称不合法。");
                // Same-dimension respawn keeps remote entities. A dimension
                // change makes ReplayGamePacketHandler clear/close the old level.
                if (dimension != key)
                    foreach (var entity in entities.Keys.Where(entity => entity != localId).ToArray()) entities.Remove(entity);
                dimension = key;
            }
            yield return packet;
            // Do this after AddEntity/RemoveEntities so observers never lose a
            // live entity's profile. A subsequent ADD_PLAYER cancels old removal.
            if (id is 1 or 75 or 80)
            {
                // Our own profile must also survive the gap between LOGIN
                // and CreateLocalPlayer, when no local entity is registered.
                var removed = deferred.Where(uuid => !(hasProfile && uuid == profileUuid)
                    && !entities.ContainsValue(uuid)).OrderBy(uuid => uuid).ToArray();
                foreach (var uuid in removed) deferred.Remove(uuid);
                if (removed.Length > 0) yield return PlayerInfoPacket(removed, hide: false);
            }
        }

        static byte[] PlayerInfoPacket(IReadOnlyCollection<Guid> uuids, bool hide)
        {
            using var output = new MemoryStream(); using var writer = new BinaryWriter(output);
            WriteVarInt(writer, hide ? 68 : 67);
            if (hide) writer.Write((byte)8); // UPDATE_LISTED only; preserve name, skin and game mode.
            WriteVarInt(writer, uuids.Count);
            foreach (var uuid in uuids) { writer.Write(uuid.ToByteArray(bigEndian: true)); if (hide) writer.Write(false); }
            return output.ToArray();
        }
    }

    static Guid ReadNetworkUuid(byte[] data, ref int cursor)
    {
        if (data.Length - cursor < 16) throw new InvalidDataException("玩家 UUID 不完整。");
        var uuid = new Guid(data.AsSpan(cursor, 16), bigEndian: true); cursor += 16; return uuid;
    }

    static List<Guid> AddedPlayerInfoUuids(byte[] data)
    {
        var p = 0; ReadPacketVarInt(data, ref p);
        byte Byte() { if (p >= data.Length) throw new InvalidDataException("玩家资料数据不完整。"); return data[p++]; }
        void Skip(int count) { if (count < 0 || count > data.Length - p) throw new InvalidDataException("玩家资料字段长度损坏。"); p += count; }
        int VarInt() => ReadPacketVarInt(data, ref p);
        void Blob() => Skip(VarInt());
        void Nbt(int type, int depth)
        {
            if (depth > 64) throw new InvalidDataException("玩家显示名称 NBT 嵌套过深。");
            int Int32() { if (data.Length - p < 4) throw new InvalidDataException("NBT 长度不完整。"); var n = BinaryPrimitives.ReadInt32BigEndian(data.AsSpan(p)); p += 4; return n; }
            void Utf() { if (data.Length - p < 2) throw new InvalidDataException("NBT 字符串不完整。"); var n = BinaryPrimitives.ReadUInt16BigEndian(data.AsSpan(p)); p += 2; Skip(n); }
            switch (type)
            {
                case 0: return;
                case 1: Skip(1); break; case 2: Skip(2); break;
                case 3: case 5: Skip(4); break; case 4: case 6: Skip(8); break;
                case 7: Skip(Int32()); break; case 8: Utf(); break;
                case 9:
                    var element = Byte(); var count = Int32();
                    if (count < 0 || count > data.Length - p || (element == 0 && count != 0)) throw new InvalidDataException("NBT 列表长度损坏。");
                    for (var i = 0; i < count; i++) Nbt(element, depth + 1);
                    break;
                case 10:
                    for (var child = Byte(); child != 0; child = Byte()) { Utf(); Nbt(child, depth + 1); }
                    break;
                case 11: case 12:
                    var length = Int32(); var width = type == 11 ? 4 : 8;
                    if (length < 0 || length > (data.Length - p) / width) throw new InvalidDataException("NBT 数组长度损坏。");
                    Skip(length * width); break;
                default: throw new InvalidDataException("玩家显示名称 NBT 类型不支持。");
            }
        }
        var mask = Byte(); var count = VarInt(); var added = new List<Guid>();
        if ((mask & 1) == 0) return added;
        if (count < 0 || count > (data.Length - p) / 16) throw new InvalidDataException("玩家资料列表长度损坏。");
        for (var i = 0; i < count; i++)
        {
            added.Add(ReadNetworkUuid(data, ref p));
            Blob(); // profile name
            var properties = VarInt();
            if (properties < 0 || properties > data.Length - p) throw new InvalidDataException("玩家资料属性数量损坏。");
            for (var j = 0; j < properties; j++) { Blob(); Blob(); if (Byte() != 0) Blob(); }
            if ((mask & 2) != 0 && Byte() != 0) { Skip(16 + 8); Blob(); Blob(); }
            if ((mask & 4) != 0) VarInt();
            if ((mask & 8) != 0) Byte();
            if ((mask & 16) != 0) VarInt();
            if ((mask & 32) != 0 && Byte() != 0) Nbt(Byte(), 0);
            if ((mask & 64) != 0) VarInt();
            if ((mask & 128) != 0) Byte();
        }
        if (p != data.Length) throw new InvalidDataException("玩家资料数据存在多余字段。");
        return added;
    }

    static IEnumerable<byte[]> EnumerateFramedPayloads(byte[]? sidecar)
    {
        if (sidecar is not { Length: > 0 }) yield break;
        var offset = 0;
        while (offset < sidecar.Length)
        {
            if (sidecar.Length - offset < 8) throw new InvalidDataException("适配数据帧头不完整");
            offset += 4; // adapter frame timestamp; Flashback actions are ordered
            var length = BinaryPrimitives.ReadInt32BigEndian(sidecar.AsSpan(offset, 4));
            offset += 4;
            if (length < 0 || length > sidecar.Length - offset) throw new InvalidDataException("适配数据帧长度损坏");
            if (length == 0) continue;
            yield return sidecar.AsSpan(offset, length).ToArray();
            offset += length;
        }
    }

    static IEnumerable<byte[]> EnumerateLengthPrefixedPayloads(byte[]? sidecar)
    {
        if (sidecar is not { Length: > 0 }) yield break;
        var offset = 0;
        while (offset < sidecar.Length)
        {
            if (sidecar.Length - offset < 4) throw new InvalidDataException("快照数据帧头不完整");
            var length = BinaryPrimitives.ReadInt32BigEndian(sidecar.AsSpan(offset, 4));
            offset += 4;
            if (length <= 0 || length > sidecar.Length - offset) throw new InvalidDataException("快照数据帧长度损坏");
            yield return sidecar.AsSpan(offset, length).ToArray();
            offset += length;
        }
    }

    static byte[] BuildAdaptedCache(byte[]? sidecar)
    {
        using var stream = new MemoryStream();
        using var writer = new BinaryWriter(stream, Encoding.UTF8, leaveOpen: true);
        foreach (var packet in EnumerateFramedPayloads(sidecar))
        {
            WriteInt(writer, packet.Length);
            writer.Write(packet);
        }
        writer.Flush();
        return stream.ToArray();
    }

    static IEnumerable<int> EnumerateChunkIndexes(byte[]? sidecar)
    {
        if (sidecar is not { Length: > 0 }) yield break;
        var index = 0;
        foreach (var packet in EnumerateLengthPrefixedPayloads(sidecar))
            yield return index++;
    }

    static byte[] BuildVarInt(int value)
    {
        using var stream = new MemoryStream();
        using var writer = new BinaryWriter(stream, Encoding.UTF8, leaveOpen: true);
        WriteVarInt(writer, value);
        writer.Flush();
        return stream.ToArray();
    }

    static byte[] BuildRemoveEntities(int entityId)
    {
        using var stream = new MemoryStream();
        using var writer = new BinaryWriter(stream, Encoding.UTF8, leaveOpen: true);
        WriteVarInt(writer, 75); // ClientboundRemoveEntities
        WriteVarInt(writer, 1);
        WriteVarInt(writer, entityId);
        writer.Flush();
        return stream.ToArray();
    }

    static byte[] BuildRecord(int actionId, byte[] payload)
    {
        using var stream = new MemoryStream();
        using (var writer = new BinaryWriter(stream, Encoding.UTF8, leaveOpen: true))
        {
            WriteVarInt(writer, actionId);
            WriteInt(writer, payload.Length);
            writer.Write(payload);
            writer.Flush();
        }
        return stream.ToArray();
    }

    /// <summary>
    /// ActionCreateLocalPlayer payload: UUID, x/y/z doubles, pitch/yaw/head-yaw
    /// floats, velocity doubles, GameProfile (UUID + name + no properties),
    /// gametype VarInt.
    /// </summary>
    static byte[] BuildCreateLocalPlayer(SampleRow sample, MetaInfo meta, Guid playerUuid)
    {
        using var stream = new MemoryStream();
        using (var writer = new BinaryWriter(stream, Encoding.UTF8, leaveOpen: true))
        {
            writer.Write(UuidBytes(playerUuid));
            writer.Write(BigEndian(sample.X));
            writer.Write(BigEndian(sample.Y));
            writer.Write(BigEndian(sample.Z));
            writer.Write(BigEndian((float)sample.Pitch));
            writer.Write(BigEndian((float)sample.Yaw));
            writer.Write(BigEndian((float)(sample.HeadYaw != 0 ? sample.HeadYaw : sample.Yaw)));
            writer.Write(BigEndian(sample.VelX));
            writer.Write(BigEndian(sample.VelY));
            writer.Write(BigEndian(sample.VelZ));
            writer.Write(UuidBytes(playerUuid));
            WriteString(writer, string.IsNullOrEmpty(meta.PlayerName) ? "Player" : meta.PlayerName);
            WriteVarInt(writer, 0); // no GameProfile properties
            WriteVarInt(writer, Math.Clamp(meta.Gametype, 0, 3));
            writer.Flush();
        }
        return stream.ToArray();
    }

    /// <summary>
    /// ActionMoveEntities payload: VarInt dimension count, dimension ResourceKey
    /// string, VarInt entity count, then entity id VarInt + x/y/z doubles +
    /// yaw/pitch/head-yaw floats + on-ground boolean.
    /// </summary>
    static byte[] BuildMoveEntities(SampleRow sample, string dimensionKey, int entityId)
    {
        var entities = new List<EntityRow>(sample.Entities.Count + 1)
        {
            new(entityId, sample.X, sample.Y, sample.Z, sample.Yaw, sample.Pitch,
                sample.HeadYaw != 0 ? sample.HeadYaw : sample.Yaw, sample.OnGround),
        };
        entities.AddRange(sample.Entities.Where(entity => entity.Id != entityId));
        using var stream = new MemoryStream();
        using (var writer = new BinaryWriter(stream, Encoding.UTF8, leaveOpen: true))
        {
            WriteVarInt(writer, 1);
            WriteString(writer, dimensionKey);
            WriteVarInt(writer, entities.Count);
            foreach (var entity in entities)
            {
                WriteVarInt(writer, entity.Id);
                writer.Write(BigEndian(entity.X));
                writer.Write(BigEndian(entity.Y));
                writer.Write(BigEndian(entity.Z));
                writer.Write(BigEndian((float)entity.Yaw));
                writer.Write(BigEndian((float)entity.Pitch));
                writer.Write(BigEndian((float)entity.HeadYaw));
                writer.Write(entity.OnGround ? (byte)1 : (byte)0);
            }
            writer.Flush();
        }
        return stream.ToArray();
    }

    /// <summary>
    /// ActionAccuratePlayerPosition payload: VarInt entity id, VarInt count,
    /// then per position x/y/z doubles and yaw/pitch floats.
    /// </summary>
    static byte[] BuildAccuratePositions(List<SampleRow> rows, int entityId)
    {
        using var stream = new MemoryStream();
        using (var writer = new BinaryWriter(stream, Encoding.UTF8, leaveOpen: true))
        {
            WriteVarInt(writer, entityId);
            WriteVarInt(writer, rows.Count);
            foreach (var row in rows)
            {
                writer.Write(BigEndian(row.X));
                writer.Write(BigEndian(row.Y));
                writer.Write(BigEndian(row.Z));
                writer.Write(BigEndian((float)row.Yaw));
                writer.Write(BigEndian((float)row.Pitch));
            }
            writer.Flush();
        }
        return stream.ToArray();
    }

    // -------------------------------------------------------------- metadata

    static string BuildMetadata(string source, MetaInfo meta, Guid uuid, int duration, List<MarkRow> marks,
        List<SampleRow> samples, long firstTick, long lastTick, ExportIdentity identity, bool adapted,
        CaptureQualityReport quality)
    {
        var version = adapted ? "1.21.11" : (string.IsNullOrWhiteSpace(meta.Version) ? "1.20.1" : meta.Version);
        var (dataVersion, protocolVersion) = adapted ? (4671, 774) : ResolveVersion(version, meta);

        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream, new JsonWriterOptions { Indented = true }))
        {
            writer.WriteStartObject();
            writer.WriteString("uuid", uuid.ToString());
            writer.WriteString("name", Path.GetFileNameWithoutExtension(source));
            writer.WriteString("version_string", version);
            if (!string.IsNullOrWhiteSpace(meta.WorldName)) writer.WriteString("world_name", meta.WorldName);
            writer.WriteNumber("data_version", dataVersion);
            writer.WriteNumber("protocol_version", protocolVersion);
            writer.WriteNumber("total_ticks", duration);
            writer.WritePropertyName("flashback_recorder");
            writer.WriteStartObject();
            writer.WriteNumber("version", 1);
            writer.WriteString("source_file", identity.SourceName);
            writer.WriteString("source_sha256", identity.SourceSha256);
            writer.WriteString("chunk_sha256", identity.ChunkSha256);
            writer.WriteString("cache_sha256", identity.CacheSha256);
            writer.WritePropertyName("quality");
            writer.WriteStartObject();
            writer.WriteBoolean("available", quality.Available);
            writer.WriteString("status", quality.Status);
            writer.WriteBoolean("complete", quality.IsComplete);
            writer.WriteBoolean("hasDataLoss", quality.HasDataLoss);
            writer.WriteBoolean("hasCompatibilityLimitations", quality.HasCompatibilityLimitations);
            writer.WriteNumber("packetErrors", quality.PacketErrors);
            writer.WriteNumber("packetDrops", quality.PacketDrops);
            writer.WriteNumber("packetSkippedCustomPayload", quality.PacketSkippedCustomPayload);
            writer.WriteEndObject();
            writer.WriteEndObject();
            WriteMarkers(writer, marks, samples, firstTick, lastTick);
            writer.WritePropertyName("chunks");
            writer.WriteStartObject();
            writer.WritePropertyName("c0.flashback");
            writer.WriteStartObject();
            writer.WriteNumber("duration", duration);
            writer.WriteBoolean("forcePlaySnapshot", false);
            writer.WriteEndObject();
            writer.WriteEndObject();
            writer.WriteEndObject();
            writer.Flush();
        }
        return Encoding.UTF8.GetString(stream.ToArray());
    }

    /// <summary>
    /// FlashbackMeta.fromJson reads markers as { tickString: ReplayMarker }.
    /// ReplayMarker is a Gson record {colour, position{position:[x,y,z],
    /// dimension}, description, event}; unparseable entries are skipped by the
    /// mod, so best-effort markers can never break the archive.
    /// </summary>
    static void WriteMarkers(Utf8JsonWriter writer, List<MarkRow> marks, List<SampleRow> samples,
        long firstTick, long lastTick)
    {
        if (marks.Count == 0 || samples.Count == 0) return;
        writer.WritePropertyName("markers");
        writer.WriteStartObject();
        var grouped = new Dictionary<long, (SampleRow Position, List<string> Names)>();
        foreach (var mark in marks)
        {
            if (string.IsNullOrWhiteSpace(mark.Name)) continue;
            var nearest = samples[0];
            var best = long.MaxValue;
            foreach (var sample in samples)
            {
                var delta = Math.Abs(sample.T - mark.T);
                if (delta < best) { best = delta; nearest = sample; }
            }
            var tick = Math.Clamp(nearest.Tick, firstTick, lastTick);
            var offset = Math.Max(0L, tick - firstTick);
            if (!grouped.TryGetValue(offset, out var group))
            {
                if (grouped.Count >= 100) continue;
                group = (nearest, new List<string>());
                grouped.Add(offset, group);
            }
            if (!group.Names.Contains(mark.Name)) group.Names.Add(mark.Name);
        }
        foreach (var (offset, group) in grouped.OrderBy(entry => entry.Key))
        {
            var nearest = group.Position;
            var dimension = TryDimensionKey(nearest.Dimension, out var parsed) ? parsed : "minecraft:overworld";
            writer.WritePropertyName(offset.ToString(System.Globalization.CultureInfo.InvariantCulture));
            writer.WriteStartObject();
            writer.WriteNumber("colour", MarkerColour);
            writer.WritePropertyName("position");
            writer.WriteStartObject();
            writer.WritePropertyName("position");
            writer.WriteStartArray();
            writer.WriteNumberValue(nearest.X);
            writer.WriteNumberValue(nearest.Y);
            writer.WriteNumberValue(nearest.Z);
            writer.WriteEndArray();
            writer.WriteString("dimension", dimension);
            writer.WriteEndObject();
            writer.WriteString("description", string.Join(" / ", group.Names));
            writer.WritePropertyName("event");
            writer.WriteNullValue();
            writer.WriteEndObject();
        }
        writer.WriteEndObject();
    }

    /// <summary>
    /// The agent records the exact data/protocol versions; the table is a
    /// fallback for traces recorded before those fields existed.
    /// </summary>
    static (int Data, int Protocol) ResolveVersion(string version, MetaInfo meta)
    {
        if (meta.DataVersion > 0 && meta.ProtocolVersion > 0) return (meta.DataVersion, meta.ProtocolVersion);
        var normalized = version.Trim();
        if (normalized == "1.21.10") return (4556, 773);
        if (normalized.StartsWith("1.20.1")) return (3465, 763);
        if (normalized.StartsWith("1.20.2")) return (3578, 764);
        if (normalized.StartsWith("1.20.3")) return (3698, 765);
        if (normalized.StartsWith("1.20.4")) return (3700, 765);
        if (normalized.StartsWith("1.20.5")) return (3837, 766);
        if (normalized.StartsWith("1.20.6")) return (3839, 766);
        if (normalized.StartsWith("1.21.2")) return (4080, 768);
        if (normalized.StartsWith("1.21.3")) return (4082, 768);
        if (normalized.StartsWith("1.21.4")) return (4189, 769);
        if (normalized.StartsWith("1.21.5")) return (4325, 770);
        if (normalized.StartsWith("1.21")) return (3955, 767);
        return (3465, 763);
    }

    // ------------------------------------------------------------------ io

    static string GetString(JsonElement root, string name, string fallback) =>
        root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? fallback : fallback;

    static int GetInt(JsonElement root, string name, int fallback) =>
        root.TryGetProperty(name, out var value) && value.TryGetInt32(out var number) ? number : fallback;

    static long GetLong(JsonElement root, string name, long fallback) =>
        root.TryGetProperty(name, out var value) && value.TryGetInt64(out var number) ? number : fallback;

    static double GetDouble(JsonElement root, string name, double fallback) =>
        root.TryGetProperty(name, out var value) && value.TryGetDouble(out var number) ? number : fallback;

    static bool GetBool(JsonElement root, string name, bool fallback) =>
        root.TryGetProperty(name, out var value) &&
        (value.ValueKind == JsonValueKind.True || value.ValueKind == JsonValueKind.False)
            ? value.GetBoolean() : fallback;

    static List<EntityRow> ParseEntities(JsonElement root)
    {
        var result = new List<EntityRow>();
        if (!root.TryGetProperty("entities", out var value) || value.ValueKind != JsonValueKind.Array)
            return result;
        foreach (var item in value.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object) continue;
            var id = GetInt(item, "id", int.MinValue);
            if (id == int.MinValue) continue;
            result.Add(new EntityRow(
                id,
                GetDouble(item, "x", 0), GetDouble(item, "y", 0), GetDouble(item, "z", 0),
                GetDouble(item, "yaw", 0), GetDouble(item, "pitch", 0),
                GetDouble(item, "headYaw", 0), GetBool(item, "onGround", false)));
        }
        return result;
    }

    static void WriteEntry(ZipArchive archive, string name, byte[] bytes, CompressionLevel level)
    {
        var entry = archive.CreateEntry(name, level);
        // Re-exporting unchanged content must retain its archive hash too.
        entry.LastWriteTime = new DateTimeOffset(1980, 1, 1, 0, 0, 0, TimeSpan.Zero);
        using var stream = entry.Open();
        stream.Write(bytes);
    }

    static void WriteInt(BinaryWriter writer, int value) => writer.Write(BigEndian(value));

    static void WriteVarInt(BinaryWriter writer, int value)
    {
        var current = (uint)value;
        while ((current & ~0x7Fu) != 0)
        {
            writer.Write((byte)((current & 0x7F) | 0x80));
            current >>= 7;
        }
        writer.Write((byte)current);
    }

    static void WriteString(BinaryWriter writer, string value)
    {
        var bytes = Encoding.UTF8.GetBytes(value);
        WriteVarInt(writer, bytes.Length);
        writer.Write(bytes);
    }

    static byte[] BigEndian(double value) => BitConverter.GetBytes(Reverse(BitConverter.DoubleToInt64Bits(value)));

    static byte[] BigEndian(float value)
    {
        var bits = BitConverter.SingleToInt32Bits(value);
        return new[]
        {
            (byte)(bits >> 24), (byte)(bits >> 16), (byte)(bits >> 8), (byte)bits,
        };
    }

    static byte[] BigEndian(int value)
    {
        var buffer = new byte[4];
        BinaryPrimitives.WriteInt32BigEndian(buffer, value);
        return buffer;
    }

    static long Reverse(long value)
    {
        var bytes = BitConverter.GetBytes(value);
        Array.Reverse(bytes);
        return BitConverter.ToInt64(bytes);
    }

    /// <summary>
    /// Parses "minecraft:overworld" out of the recorder's dimension strings such
    /// as "ResourceKey[minecraft:dimension / minecraft:overworld]".
    /// </summary>
    static bool TryDimensionKey(string dimension, out string key)
    {
        key = "";
        if (string.IsNullOrWhiteSpace(dimension)) return false;
        var candidate = dimension.Trim();
        if (candidate.StartsWith("ResourceKey[", StringComparison.Ordinal) && candidate.EndsWith(']'))
        {
            var separator = candidate.LastIndexOf(" / ", StringComparison.Ordinal);
            if (separator < 0) return false;
            candidate = candidate[(separator + 3)..^1].Trim();
        }
        var colon = candidate.IndexOf(':');
        if (colon <= 0 || colon == candidate.Length - 1) return false;
        for (var i = 0; i < candidate.Length; i++)
        {
            var c = candidate[i];
            if (i == colon || c is >= 'a' and <= 'z' or >= '0' and <= '9' or '_' or '.' or '-'
                || (i > colon && c == '/')) continue;
            return false;
        }
        key = candidate;
        return true;
    }

    /// <summary>
    /// Java writeUUID writes the two longs big-endian, i.e. the canonical UUID
    /// hex string byte order.
    /// </summary>
    static byte[] UuidBytes(Guid value) => System.Convert.FromHexString(value.ToString("N"));

    static Guid? ParseUuid(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        var hex = value.Replace("-", "");
        if (hex.Length != 32) return null;
        var formatted = hex.Insert(8, "-").Insert(13, "-").Insert(18, "-").Insert(23, "-");
        try { return Guid.Parse(formatted); }
        catch (FormatException) { return null; }
    }

    // ----------------------------------------------------------------- icon

    /// <summary>
    /// 64x64 RGB PNG drawn at runtime: dark background with a blue play
    /// triangle, so the replay browser shows a proper thumbnail.
    /// </summary>
    static byte[] BuildIconPng()
    {
        const int size = 64;
        var raw = new byte[size * (1 + size * 3)];
        for (var y = 0; y < size; y++)
        {
            var row = y * (1 + size * 3);
            raw[row] = 0; // filter: none
            for (var x = 0; x < size; x++)
            {
                byte r = 0x13, g = 0x19, b = 0x24; // dark slate background
                if (InsideTriangle(x, y, 26, 20, 26, 44, 46, 32))
                {
                    r = 0x4B; g = 0x91; b = 0xE4; // blue play triangle
                }
                raw[row + 1 + x * 3] = r;
                raw[row + 2 + x * 3] = g;
                raw[row + 3 + x * 3] = b;
            }
        }

        static bool InsideTriangle(double px, double py, double ax, double ay, double bx, double by, double cx, double cy)
        {
            var d1 = Sign(px, py, ax, ay, bx, by);
            var d2 = Sign(px, py, bx, by, cx, cy);
            var d3 = Sign(px, py, cx, cy, ax, ay);
            var hasNeg = d1 < 0 || d2 < 0 || d3 < 0;
            var hasPos = d1 > 0 || d2 > 0 || d3 > 0;
            return !(hasNeg && hasPos);
        }

        static double Sign(double px, double py, double ax, double ay, double bx, double by) =>
            (px - bx) * (ay - by) - (ax - bx) * (py - by);

        using var output = new MemoryStream();
        output.Write(new byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A });

        var ihdr = new byte[13];
        BinaryPrimitives.WriteInt32BigEndian(ihdr.AsSpan(0, 4), size);
        BinaryPrimitives.WriteInt32BigEndian(ihdr.AsSpan(4, 4), size);
        ihdr[8] = 8; // bit depth
        ihdr[9] = 2; // truecolor
        WritePngChunk(output, "IHDR", ihdr);

        using (var deflate = new MemoryStream())
        {
            using (var zlib = new ZLibStream(deflate, CompressionLevel.Optimal, leaveOpen: true))
                zlib.Write(raw);
            WritePngChunk(output, "IDAT", deflate.ToArray());
        }

        WritePngChunk(output, "IEND", Array.Empty<byte>());
        return output.ToArray();
    }

    static void WritePngChunk(MemoryStream output, string type, byte[] data)
    {
        var length = new byte[4];
        BinaryPrimitives.WriteInt32BigEndian(length, data.Length);
        output.Write(length);
        var typeBytes = Encoding.ASCII.GetBytes(type);
        output.Write(typeBytes);
        output.Write(data);
        var crcInput = new byte[typeBytes.Length + data.Length];
        typeBytes.CopyTo(crcInput, 0);
        data.CopyTo(crcInput, typeBytes.Length);
        var crc = new byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(crc, Crc32(crcInput));
        output.Write(crc);
    }

    static uint Crc32(byte[] data)
    {
        var crc = 0xFFFFFFFFu;
        foreach (var value in data)
        {
            crc ^= value;
            for (var i = 0; i < 8; i++)
                crc = (crc & 1) != 0 ? (crc >> 1) ^ 0xEDB88320u : crc >> 1;
        }
        return ~crc;
    }
}
