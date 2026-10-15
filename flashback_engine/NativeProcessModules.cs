using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace FlashbackRecorder;

internal static class NativeProcessModules
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct ModuleEntry
    {
        public uint Size, ModuleId, ProcessId, GlobalUsage, ProcessUsage;
        public IntPtr BaseAddress;
        public uint BaseSize;
        public IntPtr ModuleHandle;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Name;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string Path;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern SafeFileHandle CreateToolhelp32Snapshot(uint flags, uint processId);
    [DllImport("kernel32.dll", EntryPoint = "Module32FirstW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool First(SafeFileHandle snapshot, ref ModuleEntry entry);
    [DllImport("kernel32.dll", EntryPoint = "Module32NextW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool Next(SafeFileHandle snapshot, ref ModuleEntry entry);

    public static string[] Read(int pid, out int error)
    {
        error = 0;
        // Loader changes can transiently produce ERROR_BAD_LENGTH. Retry a
        // bounded number of times so detection cannot monopolize the worker.
        for (var attempt = 0; attempt < 4; attempt++)
        {
            using var snapshot = CreateToolhelp32Snapshot(0x8 | 0x10, (uint)pid); // SNAPMODULE | SNAPMODULE32
            if (snapshot.IsInvalid)
            {
                error = Marshal.GetLastWin32Error();
                if (error == 24) continue;
                return Array.Empty<string>();
            }
            var entry = new ModuleEntry { Size = (uint)Marshal.SizeOf<ModuleEntry>() };
            var paths = new List<string>();
            if (First(snapshot, ref entry))
            {
                do { paths.Add(entry.Path); } while (Next(snapshot, ref entry));
            }
            error = Marshal.GetLastWin32Error();
            if (error == 18) { error = 0; return paths.ToArray(); } // NO_MORE_FILES
            if (error != 24) return Array.Empty<string>();
        }
        return Array.Empty<string>();
    }
}
