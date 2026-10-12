using System.Buffers.Binary;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace FlashbackRecorder;

/// <summary>Portable recorded PNGs and Flashback 0.39.10's native file-skin overrides.</summary>
internal static class RecordedSkins
{
    const string ManifestName = "recorded-skins.json";
    const int MaxManifestBytes = 4 * 1024 * 1024;
    const int MaxPngBytes = 16 * 1024 * 1024;
    const long MaxBundleBytes = 128L * 1024 * 1024;
    static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };
    sealed record Skin(Guid Uuid, string Name, string File, string Model, string Hash);
    sealed record Bundle(List<Skin> Players, Dictionary<string, byte[]> Images, List<string> Warnings);
    sealed record PngInfo(int Width, int Height, string? Model, byte[]? Rgba);
    sealed record InstallImage(string Hash, byte[] Bytes);
    public sealed record InstallResult(int PlayerCount, int AddedOverrides, int PreservedOverrides,
        string? StatePath, string? BackupPath)
    { public IReadOnlyList<string> Warnings { get; init; } = Array.Empty<string>(); }

    public static void WriteToArchive(string source, ZipArchive archive)
    {
        var root = Path.GetDirectoryName(Path.GetFullPath(source))!;
        var manifest = Path.Combine(root, ManifestName);
        if (!File.Exists(manifest)) return; // Older recorders have no skin capture.
        var captures = Directory.EnumerateFiles(root, "*.fbrl", SearchOption.TopDirectoryOnly).Take(2).ToArray();
        if (captures.Length != 1 || !string.Equals(Path.GetFullPath(captures[0]), Path.GetFullPath(source), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("皮肤清单必须位于这条录制独占的目录，不能复用共享会话中的皮肤。");
        CheckNoReparse(root, manifest);
        var bundle = ParseBundle(ReadFile(manifest, MaxManifestBytes), name =>
        {
            var path = Path.Combine(root, name.Replace('/', Path.DirectorySeparatorChar));
            CheckNoReparse(root, path);
            return ReadFile(path, MaxPngBytes);
        });
        var bytes = JsonSerializer.SerializeToUtf8Bytes(new
        {
            version = 1,
            players = bundle.Players.Select(s => new { uuid = s.Uuid.ToString(), name = s.Name,
                file = s.File, model = s.Model, sha256 = s.Hash })
        }, JsonOptions);
        WriteEntry(archive, ManifestName, bytes);
        foreach (var image in bundle.Images) WriteEntry(archive, image.Key, image.Value);
    }

    public static int CountInReplay(string zip)
    {
        using var archive = ZipFile.OpenRead(zip);
        return ReadArchive(archive).Players.Count;
    }

    // Call only while this replay is closed: Flashback keeps the active editor
    // state in memory and would otherwise overwrite external changes on autosave.
    public static InstallResult InstallFromReplay(string zip, string gameDir)
    {
        using var archive = ZipFile.OpenRead(zip);
        var bundle = ReadArchive(archive);
        if (bundle.Players.Count == 0) return new(0, 0, 0, null, null);
        using var metadata = JsonDocument.Parse(ReadEntry(UniqueEntry(archive, "metadata.json", true)!, MaxManifestBytes));
        if (!metadata.RootElement.TryGetProperty("uuid", out var value)
            || value.ValueKind != JsonValueKind.String || !Guid.TryParse(value.GetString(), out var replayUuid))
            throw new InvalidDataException("回放 metadata.uuid 无效，无法绑定皮肤编辑状态。");

        var root = Path.GetFullPath(gameDir);
        if (!Directory.Exists(root)) throw new DirectoryNotFoundException("目标游戏目录不存在。");
        var skinsDir = Path.Combine(root, "flashback", "recorded_skins");
        var statesDir = Path.Combine(root, "flashback", "editor_states");
        var statePath = Path.Combine(statesDir, replayUuid.ToString() + ".json");
        var recoveryPath = statePath + ".old";
        CheckNoReparse(root, skinsDir); CheckNoReparse(root, statePath); CheckNoReparse(root, recoveryPath);

        byte[]? primary = File.Exists(statePath) ? ReadFile(statePath, MaxManifestBytes) : null;
        // Native Flashback loads .json.old when the primary is missing. A new
        // minimal primary would mask those recoverable camera/keyframe edits.
        var recoveredFromOld = primary == null && File.Exists(recoveryPath);
        byte[]? previous = recoveredFromOld ? ReadFile(recoveryPath, MaxManifestBytes) : primary;
        JsonObject state;
        try { state = previous == null ? new JsonObject() : JsonNode.Parse(previous) as JsonObject
                ?? throw new InvalidDataException("现有回放编辑状态不是 JSON 对象，未修改。"); }
        catch (JsonException ex) { throw new InvalidDataException("现有回放编辑状态损坏，未修改。", ex); }
        var overrides = ObjectField(state, "skinOverrideFromFile");
        var profileOverrides = ObjectField(state, "skinOverride");
        var preserved = 0;
        var additions = new List<Skin>();
        var computedImages = new Dictionary<(string Hash, string Model), InstallImage>();
        var installedImages = new Dictionary<(string Hash, string Model), InstallImage>();
        var notices = new List<string>();
        foreach (var skin in bundle.Players)
        {
            if (ContainsUuid(profileOverrides, skin.Uuid)) { preserved++; continue; }
            var existing = overrides.Where(pair => Guid.TryParse(pair.Key, out var id) && id == skin.Uuid).ToArray();
            string? previousPath = null, previousHash = null;
            // Native editor saves do not retain our own metadata, so recognize
            // only the exact content-addressed path shape we previously wrote.
            // Never access an old computer's absolute path or reinterpret an
            // arbitrary manual override as recorder-owned.
            if (existing.Length > 0 && (existing.Length != 1
                || !TryRecordedSkinPath(existing[0].Value, out previousPath, out previousHash)))
            { preserved++; continue; }
            var key = (skin.Hash, skin.Model);
            if (!computedImages.TryGetValue(key, out var image))
            {
                image = ImageForInstallation(bundle.Images[skin.File], skin.Hash, skin.Model);
                computedImages.Add(key, image);
            }
            if (previousHash != null && !string.Equals(previousHash, skin.Hash, StringComparison.OrdinalIgnoreCase)
                && !string.Equals(previousHash, image.Hash, StringComparison.OrdinalIgnoreCase))
            { preserved++; continue; }
            installedImages.TryAdd(key, image);
            var destination = Path.Combine(skinsDir, image.Hash + ".png");
            if (previousPath != null && string.Equals(previousPath, destination, StringComparison.OrdinalIgnoreCase))
            { preserved++; continue; } // Still restore a missing local asset below.
            if (image.Hash != skin.Hash)
                notices.Add($"玩家 {skin.Name} ({skin.Uuid}) 已生成 {skin.Model} 模型副本，仅调整原版检测点透明度；录制原图未修改。");
            additions.Add(skin);
            if (previousPath != null)
            {
                existing[0].Value!["pathToSkin"] = destination;
                notices.Add($"已将玩家 {skin.Name} ({skin.Uuid}) 的录制皮肤路径迁移到当前游戏目录。");
            }
            else overrides[skin.Uuid.ToString()] = new JsonObject { ["pathToSkin"] = destination };
        }

        // Validate everything before the first write. Assets are immutable and
        // content-addressed, so a failed state update can leave only reusable PNGs.
        foreach (var image in installedImages.Values)
        {
            var destination = Path.Combine(skinsDir, image.Hash + ".png");
            CheckNoReparse(root, destination);
            if (File.Exists(destination)) ValidatePng(ReadFile(destination, MaxPngBytes), image.Hash);
        }
        if (installedImages.Count > 0) Directory.CreateDirectory(skinsDir);
        foreach (var image in installedImages.Values)
        {
            var destination = Path.Combine(skinsDir, image.Hash + ".png");
            if (!File.Exists(destination)) WriteNewAtomic(destination, image.Bytes);
        }
        if (additions.Count == 0) return new(bundle.Players.Count, 0, preserved, statePath, null);
        state["skinOverrideFromFile"] = overrides;
        var next = JsonSerializer.SerializeToUtf8Bytes(state, JsonOptions);
        Directory.CreateDirectory(statesDir);
        var temporary = statePath + "." + Guid.NewGuid().ToString("N") + ".tmp";
        string? backup = null;
        try
        {
            WriteNewFile(temporary, next);
            if (primary == null)
            {
                if (recoveredFromOld && (!File.Exists(recoveryPath)
                    || !ReadFile(recoveryPath, MaxManifestBytes).AsSpan().SequenceEqual(previous)))
                    throw new IOException("回放恢复状态在安装期间发生变化，未覆盖；请退出此回放后重试。");
                File.Move(temporary, statePath); // Never overwrite a newly created editor state.
                if (recoveredFromOld)
                {
                    backup = recoveryPath; // Preserve the native recovery file byte for byte.
                    notices.Add("已保留并恢复此回放的 .json.old 编辑状态，再添加录制皮肤。");
                }
            }
            else
            {
                if (!File.Exists(statePath) || !ReadFile(statePath, MaxManifestBytes).AsSpan().SequenceEqual(previous))
                    throw new IOException("编辑状态在安装期间发生变化，未覆盖；请退出此回放后重试。");
                backup = statePath + ".before-recorded-skins-" + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss-fff")
                    + "-" + Guid.NewGuid().ToString("N")[..8] + ".bak";
                File.Replace(temporary, statePath, backup);
            }
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
        return new(bundle.Players.Count, additions.Count, preserved, statePath, backup) { Warnings = notices };
    }

    static JsonObject ObjectField(JsonObject root, string name)
    {
        if (!root.TryGetPropertyValue(name, out var node)) return new JsonObject();
        return node as JsonObject ?? throw new InvalidDataException($"编辑状态的 {name} 不是对象，未修改。");
    }
    static bool ContainsUuid(JsonObject values, Guid uuid) => values.Any(pair => Guid.TryParse(pair.Key, out var parsed) && parsed == uuid);

    static bool TryRecordedSkinPath(JsonNode? value, out string? path, out string? hash)
    {
        path = null; hash = null;
        if (value is not JsonObject entry || entry["pathToSkin"] is not JsonValue pathNode
            || !pathNode.TryGetValue<string>(out var stored) || string.IsNullOrEmpty(stored)) return false;
        try
        {
            if (!Path.IsPathFullyQualified(stored)) return false;
            var canonical = Path.GetFullPath(stored);
            if (!string.Equals(stored.Replace(Path.AltDirectorySeparatorChar, Path.DirectorySeparatorChar),
                canonical, StringComparison.OrdinalIgnoreCase)) return false;
            var folder = Path.GetDirectoryName(canonical);
            if (!string.Equals(Path.GetFileName(folder), "recorded_skins", StringComparison.OrdinalIgnoreCase)
                || !string.Equals(Path.GetFileName(Path.GetDirectoryName(folder)), "flashback", StringComparison.OrdinalIgnoreCase)) return false;
            var name = Path.GetFileName(canonical);
            if (name.Length != 68 || !name.EndsWith(".png", StringComparison.OrdinalIgnoreCase)) return false;
            var digest = name[..64];
            if (digest.Any(c => !Uri.IsHexDigit(c))) return false;
            path = canonical; hash = digest; return true;
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException) { return false; }
    }

    static Bundle ReadArchive(ZipArchive archive)
    {
        var entry = UniqueEntry(archive, ManifestName, false);
        if (entry == null) return new(new(), new(), new());
        return ParseBundle(ReadEntry(entry, MaxManifestBytes), name => ReadEntry(UniqueEntry(archive, name, true)!, MaxPngBytes));
    }
    static ZipArchiveEntry? UniqueEntry(ZipArchive archive, string name, bool required)
    {
        var matches = archive.Entries.Where(e => string.Equals(e.FullName, name, StringComparison.OrdinalIgnoreCase)).ToList();
        if (matches.Count > 1 || (matches.Count == 1 && matches[0].FullName != name))
            throw new InvalidDataException($"回放包含重复或大小写不一致的文件：{name}");
        if (matches.Count == 0 && required) throw new InvalidDataException($"回放缺少皮肤所需文件：{name}");
        return matches.FirstOrDefault();
    }
    static Bundle ParseBundle(byte[] manifest, Func<string, byte[]> readImage)
    {
        try
        {
            using var doc = JsonDocument.Parse(manifest);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("version", out var version)
                || version.ValueKind != JsonValueKind.Number || !version.TryGetInt32(out var number) || number != 1
                || !root.TryGetProperty("players", out var players) || players.ValueKind != JsonValueKind.Array
                || players.GetArrayLength() > 4096) throw new InvalidDataException("录制皮肤清单版本或玩家列表无效。");
            var result = new List<Skin>(); var ids = new HashSet<Guid>(); var images = new Dictionary<string, byte[]>();
            var models = new Dictionary<string, string?>(); var warnings = new List<string>(); long total = 0;
            foreach (var row in players.EnumerateArray())
            {
                var uuid = StringField(row, "uuid"); var name = StringField(row, "name");
                var file = StringField(row, "file"); var model = StringField(row, "model"); var hash = StringField(row, "sha256");
                if (!Guid.TryParseExact(uuid, "D", out var id) || !ids.Add(id))
                    throw new InvalidDataException("皮肤清单包含无效或重复的玩家 UUID。");
                if (name.Length > 256 || model is not ("slim" or "wide") || hash.Length != 64
                    || hash.Any(c => c is not (>= '0' and <= '9') and not (>= 'a' and <= 'f'))
                    || file != "recorded-skins/" + hash + ".png")
                    throw new InvalidDataException("皮肤路径、模型或 SHA-256 无效。");
                if (!images.ContainsKey(file))
                {
                    var bytes = readImage(file); models[file] = ValidatePng(bytes, hash).Model; total += bytes.Length;
                    if (total > MaxBundleBytes) throw new InvalidDataException("录制皮肤资产总大小超出 128 MiB。");
                    images.Add(file, bytes);
                }
                if (models[file] is { } detected && detected != model)
                    warnings.Add($"玩家 {name} ({id}) 的清单模型为 {model}，原版按 PNG 像素判断为 {detected}；已保留原图。");
                else if (models[file] == null)
                    warnings.Add($"玩家 {name} ({id}) 的 PNG 编码暂不支持模型核对；已保留原图。");
                result.Add(new(id, name, file, model, hash));
            }
            return new(result, images, warnings);
        }
        catch (JsonException ex) { throw new InvalidDataException("录制皮肤清单 JSON 损坏。", ex); }
    }
    static string StringField(JsonElement row, string name)
    {
        if (row.ValueKind != JsonValueKind.Object || !row.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.String)
            throw new InvalidDataException($"皮肤清单缺少字段：{name}");
        return value.GetString()!;
    }
    static PngInfo ValidatePng(byte[] bytes, string hash, bool includePixels = false)
    {
        if (bytes.Length is < 45 or > MaxPngBytes || !bytes.AsSpan(0, 8).SequenceEqual(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 })
            || Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant() != hash)
            throw new InvalidDataException("录制皮肤 PNG 头或 SHA-256 不匹配。");
        var offset = 8; var hasData = false; var hasEnd = false;
        byte[]? palette = null, transparency = null;
        int width = 0, height = 0, depth = 0, colour = 0, interlace = 0;
        using var imageData = new MemoryStream();
        while (offset < bytes.Length)
        {
            if (bytes.Length - offset < 12) throw new InvalidDataException("皮肤 PNG 数据被截断。");
            var size = BinaryPrimitives.ReadInt32BigEndian(bytes.AsSpan(offset));
            if (size < 0 || size > bytes.Length - offset - 12) throw new InvalidDataException("皮肤 PNG 块长度无效。");
            var type = Encoding.ASCII.GetString(bytes, offset + 4, 4);
            if (Crc32(bytes.AsSpan(offset + 4, size + 4)) != BinaryPrimitives.ReadUInt32BigEndian(bytes.AsSpan(offset + size + 8)))
                throw new InvalidDataException("皮肤 PNG CRC 校验失败。");
            if (offset == 8)
            {
                if (type != "IHDR" || size != 13) throw new InvalidDataException("皮肤 PNG 缺少 IHDR。");
                width = BinaryPrimitives.ReadInt32BigEndian(bytes.AsSpan(offset + 8));
                height = BinaryPrimitives.ReadInt32BigEndian(bytes.AsSpan(offset + 12));
                if (width is < 64 or > 2048 || (width & (width - 1)) != 0 || (height != width && height != width / 2))
                    throw new InvalidDataException("皮肤 PNG 尺寸须为 64 至 2048 的二次幂，宽高比 1:1 或 2:1。");
                depth = bytes[offset + 16]; colour = bytes[offset + 17]; interlace = bytes[offset + 20];
                var validDepth = colour switch { 0 => depth is 1 or 2 or 4 or 8 or 16, 2 or 4 or 6 => depth is 8 or 16,
                    3 => depth is 1 or 2 or 4 or 8, _ => false };
                if (!validDepth || bytes[offset + 18] != 0 || bytes[offset + 19] != 0 || interlace > 1)
                    throw new InvalidDataException("皮肤 PNG 的颜色、位深或压缩头无效。");
            }
            else if (type == "IHDR") throw new InvalidDataException("皮肤 PNG 包含重复 IHDR。");
            if (type == "PLTE")
            {
                if (palette != null || hasData || size is <= 0 or > 768 || size % 3 != 0)
                    throw new InvalidDataException("皮肤 PNG 调色板无效。");
                palette = bytes.AsSpan(offset + 8, size).ToArray();
            }
            if (type == "tRNS")
            {
                if (transparency != null || hasData || (colour == 3 && (palette == null || size > palette.Length / 3))
                    || (colour == 2 && size != 6) || (colour == 0 && size != 2) || colour is 4 or 6)
                    throw new InvalidDataException("皮肤 PNG 透明度表无效。");
                transparency = bytes.AsSpan(offset + 8, size).ToArray();
            }
            if (type == "IDAT") { hasData |= size > 0; imageData.Write(bytes, offset + 8, size); }
            offset += size + 12;
            if (type == "IEND") { hasEnd = size == 0 && offset == bytes.Length; break; }
        }
        if (!hasData || !hasEnd) throw new InvalidDataException("皮肤 PNG 缺少图像数据或结束块。");
        if (colour == 3 && (palette == null || palette.Length / 3 > 1 << depth))
            throw new InvalidDataException("索引色皮肤 PNG 缺少调色板或颜色数超出位深。");
        // Cached skins may preserve a palette and tRNS instead of NativeImage's
        // RGB(A). Reconstruct their exact RGBA values, including partial alpha.
        if (interlace != 0 || (colour != 3 && (depth != 8 || colour is not (2 or 6)))) return new(width, height, null, null);
        var channels = colour == 6 ? 4 : colour == 2 ? 3 : 1;
        var stride = colour == 3 ? checked((width * depth + 7) / 8) : checked(width * channels);
        imageData.Position = 0;
        using var decoded = new ZLibStream(imageData, CompressionMode.Decompress);
        var previous = new byte[stride]; var row = new byte[stride]; var alpha = 255;
        var pixels = includePixels ? new byte[checked(width * height * 4)] : null;
        try
        {
            for (var y = 0; y < height; y++)
            {
                var filter = decoded.ReadByte();
                if (filter is < 0 or > 4) throw new InvalidDataException("皮肤 PNG 像素过滤类型无效。");
                decoded.ReadExactly(row);
                for (var x = 0; x < stride; x++)
                {
                    var left = x >= channels ? row[x - channels] : 0; var up = previous[x]; var corner = x >= channels ? previous[x - channels] : 0;
                    var correction = filter switch { 1 => left, 2 => up, 3 => (left + up) / 2, 4 => Paeth(left, up, corner), _ => 0 };
                    row[x] = unchecked((byte)(row[x] + correction));
                }
                for (var x = 0; x < width; x++)
                {
                    byte r, g, b, a;
                    if (colour == 3)
                    {
                        var bit = x * depth; var index = (row[bit / 8] >> (8 - depth - bit % 8)) & ((1 << depth) - 1);
                        if (index >= palette!.Length / 3) throw new InvalidDataException("皮肤 PNG 像素引用了不存在的调色板颜色。");
                        r = palette[index * 3]; g = palette[index * 3 + 1]; b = palette[index * 3 + 2];
                        a = transparency != null && index < transparency.Length ? transparency[index] : (byte)255;
                    }
                    else
                    {
                        r = row[x * channels]; g = row[x * channels + 1]; b = row[x * channels + 2];
                        a = colour == 6 ? row[x * channels + 3] : (byte)255;
                        if (colour == 2 && transparency != null
                            && r == BinaryPrimitives.ReadUInt16BigEndian(transparency)
                            && g == BinaryPrimitives.ReadUInt16BigEndian(transparency.AsSpan(2))
                            && b == BinaryPrimitives.ReadUInt16BigEndian(transparency.AsSpan(4))) a = 0;
                    }
                    if (x == 54 * width / 64 && y == 20 * height / 64) alpha = a;
                    if (pixels == null) continue;
                    var destination = (y * width + x) * 4;
                    pixels[destination] = r; pixels[destination + 1] = g; pixels[destination + 2] = b; pixels[destination + 3] = a;
                }
                (previous, row) = (row, previous);
            }
            if (decoded.ReadByte() != -1) throw new InvalidDataException("皮肤 PNG 像素数据超过声明尺寸。");
        }
        catch (EndOfStreamException ex) { throw new InvalidDataException("皮肤 PNG 像素数据被截断。", ex); }
        return new(width, height, alpha < 20 ? "slim" : "wide", pixels);
    }
    static InstallImage ImageForInstallation(byte[] bytes, string hash, string model)
    {
        var image = ValidatePng(bytes, hash, includePixels: true);
        if (image.Width != image.Height)
            throw new InvalidDataException("皮肤采用 2:1 旧布局，无法保证原版文件皮肤的模型正确；未自动安装。");
        if (image.Rgba == null || image.Model == null)
            throw new InvalidDataException("皮肤 PNG 编码暂不支持模型规范化，无法保证模型正确；未自动安装。");
        if (image.Model == model) return new(hash, bytes);
        var alphaOffset = ((20 * image.Height / 64) * image.Width + 54 * image.Width / 64) * 4 + 3;
        image.Rgba[alphaOffset] = model == "wide" ? (byte)255 : (byte)0;
        var derived = EncodeRgbaPng(image.Width, image.Height, image.Rgba);
        var derivedHash = Convert.ToHexString(SHA256.HashData(derived)).ToLowerInvariant();
        var verification = ValidatePng(derived, derivedHash);
        if (verification.Model != model) throw new InvalidDataException("皮肤模型副本校验失败，未安装。");
        return new(derivedHash, derived);
    }
    static byte[] EncodeRgbaPng(int width, int height, byte[] rgba)
    {
        using var png = new MemoryStream(); png.Write(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 });
        var header = new byte[13]; BinaryPrimitives.WriteInt32BigEndian(header, width);
        BinaryPrimitives.WriteInt32BigEndian(header.AsSpan(4), height); header[8] = 8; header[9] = 6;
        WritePngChunk(png, "IHDR", header);
        using var compressed = new MemoryStream();
        using (var compressor = new ZLibStream(compressed, CompressionLevel.Optimal, leaveOpen: true))
            for (var y = 0; y < height; y++) { compressor.WriteByte(0); compressor.Write(rgba, y * width * 4, width * 4); }
        WritePngChunk(png, "IDAT", compressed.ToArray()); WritePngChunk(png, "IEND", Array.Empty<byte>());
        return png.ToArray();
    }
    static void WritePngChunk(Stream output, string type, byte[] data)
    {
        Span<byte> number = stackalloc byte[4]; BinaryPrimitives.WriteInt32BigEndian(number, data.Length); output.Write(number);
        var body = new byte[4 + data.Length]; Encoding.ASCII.GetBytes(type, body); data.CopyTo(body, 4); output.Write(body);
        BinaryPrimitives.WriteUInt32BigEndian(number, Crc32(body)); output.Write(number);
    }
    static int Paeth(int left, int up, int corner)
    {
        var p = left + up - corner; var a = Math.Abs(p - left); var b = Math.Abs(p - up); var c = Math.Abs(p - corner);
        return a <= b && a <= c ? left : b <= c ? up : corner;
    }
    static uint Crc32(ReadOnlySpan<byte> bytes)
    {
        var crc = uint.MaxValue;
        foreach (var value in bytes) { crc ^= value; for (var bit = 0; bit < 8; bit++) crc = (crc >> 1) ^ ((crc & 1) != 0 ? 0xedb88320u : 0); }
        return ~crc;
    }
    static byte[] ReadFile(string path, int limit)
    {
        using var stream = File.OpenRead(path); return ReadLimited(stream, stream.Length, limit);
    }
    static byte[] ReadEntry(ZipArchiveEntry entry, int limit)
    {
        using var stream = entry.Open(); return ReadLimited(stream, entry.Length, limit);
    }
    static byte[] ReadLimited(Stream stream, long length, int limit)
    {
        if (length < 0 || length > limit) throw new InvalidDataException("皮肤文件或编辑状态超出大小限制。");
        var result = new byte[(int)length]; stream.ReadExactly(result);
        if (stream.ReadByte() != -1) throw new InvalidDataException("皮肤文件长度与声明不符。");
        return result;
    }
    static void CheckNoReparse(string root, string path)
    {
        var relative = Path.GetRelativePath(root, path);
        if (Path.IsPathRooted(relative) || relative == ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar))
            throw new InvalidDataException("皮肤路径越出指定目录。");
        var current = root;
        foreach (var part in new[] { "" }.Concat(relative.Split(Path.DirectorySeparatorChar)))
        {
            if (part.Length > 0) current = Path.Combine(current, part);
            if ((File.Exists(current) || Directory.Exists(current)) && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException("皮肤路径不允许符号链接或目录联接。");
        }
    }
    static void WriteEntry(ZipArchive archive, string path, byte[] bytes)
    {
        var entry = archive.CreateEntry(path, CompressionLevel.Optimal);
        entry.LastWriteTime = new DateTimeOffset(1980, 1, 1, 0, 0, 0, TimeSpan.Zero);
        using var output = entry.Open(); output.Write(bytes);
    }
    static void WriteNewFile(string path, byte[] bytes)
    {
        using var output = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        output.Write(bytes); output.Flush(flushToDisk: true);
    }
    static void WriteNewAtomic(string path, byte[] bytes)
    {
        var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try { WriteNewFile(temporary, bytes); File.Move(temporary, path); }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}
