using System.Diagnostics;
using System.Text.RegularExpressions;

namespace FlashbackRecorder;

internal static class JavaRuntimeProbe
{
    internal static bool IsX64Executable(string path)
    {
        try
        {
            using var stream = File.OpenRead(path);
            using var reader = new BinaryReader(stream);
            if (stream.Length < 64 || reader.ReadUInt16() != 0x5a4d) return false;
            stream.Position = 0x3c;
            var offset = reader.ReadInt32();
            if (offset < 64 || offset > stream.Length - 6) return false;
            stream.Position = offset;
            return reader.ReadUInt32() == 0x00004550 && reader.ReadUInt16() == 0x8664;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { return false; }
    }

    internal static async Task<bool> IsUsableAsync(string path, CancellationToken cancellation = default)
    {
        if (!IsX64Executable(path)) return false;
        var info = new ProcessStartInfo(path);
        info.ArgumentList.Add("--add-modules"); info.ArgumentList.Add("jdk.attach"); info.ArgumentList.Add("-version");
        try
        {
            var result = await ChildProcess.RunAsync(info, TimeSpan.FromSeconds(3), cancellation).ConfigureAwait(false);
            var match = Regex.Match(result.Output, "version\\s+\"(?<major>\\d+)", RegexOptions.IgnoreCase);
            return result.ExitCode == 0 && match.Success && int.TryParse(match.Groups["major"].Value, out var major) && major >= 17;
        }
        catch (Exception error) when (error is TimeoutException or IOException or UnauthorizedAccessException
            or System.ComponentModel.Win32Exception or InvalidOperationException) { return false; }
    }
}
