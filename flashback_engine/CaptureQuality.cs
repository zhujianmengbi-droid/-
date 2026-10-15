using System.Text;
using System.Text.Json;

namespace FlashbackRecorder;

/// <summary>
/// Quality information written by the runtime recorder next to an FBRL trace.
/// Skipped custom payloads and unsupported registry tag groups are cross-version
/// compatibility limitations, kept separate from accidental packet loss.
/// </summary>
internal sealed record CaptureQualityReport(
    bool Available,
    int PacketErrors,
    int PacketDrops,
    int PacketSkippedCustomPayload,
    int EntitySnapshotErrors,
    int SkinCaptureErrors,
    int AttributeEntriesSkipped,
    IReadOnlyList<string> DetailWarnings)
{
    public int CustomRegistryTagGroupsDropped { get; init; }
    public int MissingPlayerProfiles { get; init; }
    public int RecordedProfileManifestInvalid { get; init; }
    public int IncompleteWorldTransitions { get; init; }
    public int SkippedUnsampledWorldTransitions { get; init; }
    public int CoalescedTransitionMarkers { get; init; }

    public bool HasDataLoss => PacketErrors > 0 || PacketDrops > 0 || EntitySnapshotErrors > 0
        || SkinCaptureErrors > 0 || AttributeEntriesSkipped > 0 || MissingPlayerProfiles > 0
        || RecordedProfileManifestInvalid > 0 || IncompleteWorldTransitions > 0
        || SkippedUnsampledWorldTransitions > 0 || DetailWarnings.Any(IsDataLossWarning);

    public bool HasCompatibilityLimitations => PacketSkippedCustomPayload > 0 || CustomRegistryTagGroupsDropped > 0;

    public bool HasWarnings => !Available || HasDataLoss || HasCompatibilityLimitations;

    public bool IsComplete => Available && !HasDataLoss;

    public string Status => HasDataLoss ? "incomplete"
        : !Available ? "unknown"
        : HasCompatibilityLimitations ? "complete_with_compatibility_limits" : "complete";

    public string Summary
    {
        get
        {
            var parts = new List<string>();
            if (!Available) parts.Add("未找到 capture.properties，无法确认采集完整性。");
            if (PacketErrors > 0) parts.Add($"动态数据包异常 {PacketErrors} 个");
            if (PacketDrops > 0) parts.Add($"数据包丢失 {PacketDrops} 个");
            if (EntitySnapshotErrors > 0) parts.Add($"实体快照异常 {EntitySnapshotErrors} 个");
            if (SkinCaptureErrors > 0) parts.Add($"皮肤采集异常 {SkinCaptureErrors} 个");
            if (AttributeEntriesSkipped > 0) parts.Add($"跳过未注册实体属性 {AttributeEntriesSkipped} 项，实体属性可能不完整");
            if (MissingPlayerProfiles > 0) parts.Add(MissingPlayerProfilesWarning);
            if (RecordedProfileManifestInvalid > 0) parts.Add(InvalidProfileManifestWarning);
            if (IncompleteWorldTransitions > 0) parts.Add(IncompleteTransitionWarning);
            if (SkippedUnsampledWorldTransitions > 0) parts.Add(UnsampledTransitionWarning);
            if (PacketSkippedCustomPayload > 0)
                parts.Add($"跳过自定义插件数据包 {PacketSkippedCustomPayload} 个（跨版本兼容限制，不计为意外丢包）");
            if (CustomRegistryTagGroupsDropped > 0) parts.Add(RegistryTagWarning);
            parts.AddRange(DetailWarnings.Where(item => !item.Contains("自定义插件数据包", StringComparison.Ordinal)
                && !item.StartsWith("采集过程中有 ", StringComparison.Ordinal)
                && !item.StartsWith("初始实体快照有 ", StringComparison.Ordinal)
                && !item.StartsWith("有 ", StringComparison.Ordinal)));
            return parts.Count == 0 ? "采集完整。" : string.Join("；", parts);
        }
    }

    public static CaptureQualityReport FromCaptureProperties(byte[]? bytes)
    {
        if (bytes is null || bytes.Length == 0)
            return new(false, 0, 0, 0, 0, 0, 0, Array.Empty<string>());

        var values = ReadProperties(bytes);

        var packetErrors = ReadCount(values, "packetErrors");
        var packetDrops = ReadCount(values, "packetDrops");
        var skipped = ReadCount(values, "packetSkippedCustomPayload");
        var entityErrors = ReadCount(values, "entitySnapshotErrors");
        var skinErrors = ReadCount(values, "skinCaptureErrors");
        var attributeEntriesSkipped = ReadCount(values, "attributeEntriesSkipped");
        var warnings = new List<string>();
        if (packetErrors > 0) warnings.Add($"采集过程中有 {packetErrors} 个动态数据包处理失败，回放可能缺失数据。");
        if (packetDrops > 0) warnings.Add($"采集过程中丢失 {packetDrops} 个数据包，回放可能缺失数据。");
        if (entityErrors > 0) warnings.Add($"初始实体快照有 {entityErrors} 个异常，部分实体可能缺失。");
        if (skinErrors > 0) warnings.Add($"有 {skinErrors} 个皮肤采集异常，部分皮肤可能缺失。");
        if (attributeEntriesSkipped > 0)
            warnings.Add($"跳过未注册实体属性 {attributeEntriesSkipped} 项，实体属性可能不完整。");
        if (skipped > 0)
            warnings.Add($"跳过自定义插件数据包 {skipped} 个（跨版本兼容限制，不计为意外丢包）。");

        var seenProblems = new HashSet<string>(StringComparer.Ordinal);
        foreach (var key in new[] { "captureError", "error", "stopWarning", "chunkSnapshotError",
            "entitySnapshotError", "entityPacketError", "localStateError", "sampleError",
            "worldSnapshotError", "skinError" })
        {
            if (!values.TryGetValue(key, out var value) || IsEmptyValue(value) || !seenProblems.Add(value)) continue;
            warnings.Add($"{key}: {value}");
        }
        if (values.TryGetValue("localEffectsHook", out var effects) &&
            effects.StartsWith("error:", StringComparison.OrdinalIgnoreCase))
            warnings.Add("本地交互声音采集未就绪；服务器数据包仍保留。");

        return new(true, packetErrors, packetDrops, skipped, entityErrors, skinErrors, attributeEntriesSkipped, warnings);
    }

    public CaptureQualityReport WithConversionProperties(byte[]? bytes)
    {
        var values = ReadProperties(bytes);
        return this with
        {
            CustomRegistryTagGroupsDropped = ReadCount(values, "customRegistryTagGroupsDropped"),
            MissingPlayerProfiles = ReadCount(values, "playerProfilesMissing"),
            RecordedProfileManifestInvalid = ReadCount(values, "recordedProfileManifestInvalid"),
            IncompleteWorldTransitions = ReadCount(values, "incompleteWorldTransitions")
        };
    }

    string RegistryTagWarning => $"跳过 {CustomRegistryTagGroupsDropped} 个自定义注册表标签组（目标客户端不支持，属于跨版本兼容限制，不计为意外丢包）";
    string MissingPlayerProfilesWarning => $"{MissingPlayerProfiles} 个玩家/NPC 缺少资料，可能不显示";
    const string InvalidProfileManifestWarning = "皮肤身份清单无效，已禁用清单中的玩家资料恢复；回放仍可导出";
    string IncompleteTransitionWarning => $"{IncompleteWorldTransitions} 次转服尚未完成，已保留此前录制内容";
    string UnsampledTransitionWarning => $"{SkippedUnsampledWorldTransitions} 个转服或重生片段缺少可靠人物位置或完整世界重建边界，已跳过并保留已完成的世界";

    public byte[] ToJson()
    {
        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream, new JsonWriterOptions { Indented = true }))
        {
            writer.WriteStartObject();
            writer.WriteBoolean("available", Available);
            writer.WriteString("status", Status);
            writer.WriteBoolean("complete", IsComplete);
            writer.WriteBoolean("hasDataLoss", HasDataLoss);
            writer.WriteBoolean("hasCompatibilityLimitations", HasCompatibilityLimitations);
            writer.WriteNumber("packetErrors", PacketErrors);
            writer.WriteNumber("packetDrops", PacketDrops);
            writer.WriteNumber("packetSkippedCustomPayload", PacketSkippedCustomPayload);
            writer.WriteNumber("customRegistryTagGroupsDropped", CustomRegistryTagGroupsDropped);
            writer.WriteNumber("missingPlayerProfiles", MissingPlayerProfiles);
            writer.WriteNumber("recordedProfileManifestInvalid", RecordedProfileManifestInvalid);
            writer.WriteNumber("incompleteWorldTransitions", IncompleteWorldTransitions);
            writer.WriteNumber("skippedUnsampledWorldTransitions", SkippedUnsampledWorldTransitions);
            writer.WriteNumber("coalescedTransitionMarkers", CoalescedTransitionMarkers);
            writer.WriteNumber("entitySnapshotErrors", EntitySnapshotErrors);
            writer.WriteNumber("skinCaptureErrors", SkinCaptureErrors);
            writer.WriteNumber("attributeEntriesSkipped", AttributeEntriesSkipped);
            writer.WritePropertyName("warnings");
            writer.WriteStartArray();
            foreach (var warning in DetailWarnings) writer.WriteStringValue(warning);
            if (CustomRegistryTagGroupsDropped > 0) writer.WriteStringValue(RegistryTagWarning);
            if (MissingPlayerProfiles > 0) writer.WriteStringValue(MissingPlayerProfilesWarning);
            if (RecordedProfileManifestInvalid > 0) writer.WriteStringValue(InvalidProfileManifestWarning);
            if (IncompleteWorldTransitions > 0) writer.WriteStringValue(IncompleteTransitionWarning);
            if (SkippedUnsampledWorldTransitions > 0) writer.WriteStringValue(UnsampledTransitionWarning);
            writer.WriteEndArray();
            writer.WriteEndObject();
        }
        return stream.ToArray();
    }

    static Dictionary<string, string> ReadProperties(byte[]? bytes)
    {
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (bytes is null || bytes.Length == 0) return values;
        var text = new UTF8Encoding(false, false).GetString(bytes);
        foreach (var line in text.Split('\n'))
        {
            var trimmed = line.Trim().TrimStart('\uFEFF');
            if (trimmed.Length == 0 || trimmed.StartsWith('#') || trimmed.StartsWith(';')) continue;
            var separator = trimmed.IndexOf('=');
            if (separator <= 0) continue;
            values[trimmed[..separator].Trim()] = trimmed[(separator + 1)..].Trim();
        }
        return values;
    }

    static int ReadCount(IReadOnlyDictionary<string, string> values, string key)
    {
        if (!values.TryGetValue(key, out var value) || !int.TryParse(value, out var count)) return 0;
        return Math.Max(0, count);
    }

    static bool IsEmptyValue(string value) => string.IsNullOrWhiteSpace(value)
        || value is "0" or "false" or "none" or "ok";

    static bool IsDataLossWarning(string warning) => !warning.Contains("自定义插件数据包", StringComparison.Ordinal);
}

internal static class BridgeErrorClassifier
{
    public static bool IsCommandFailure(string context) => context is "检测进程失败" or "注入失败"
        or "录制命令失败" or "导出失败" or "保存设置失败";
}

internal static class RecordingLifecycle
{
    public static bool CanExportClosed(IReadOnlyDictionary<string, string> status) =>
        status.GetValueOrDefault("recording") == "false"
        && status.GetValueOrDefault("state") is "ready" or "record-error";

    public static bool CanReload(string? gameVersion, bool outdated, IReadOnlyDictionary<string, string> status) =>
        gameVersion == "1.21.10" && outdated && status.GetValueOrDefault("recording") == "false"
        && int.TryParse(status.GetValueOrDefault("agentVersion"), out var version) && version >= 14
        && status.GetValueOrDefault("state") is "ready" or "attached" or "record-error" or "error";
}
