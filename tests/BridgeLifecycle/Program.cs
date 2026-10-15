using System.Reflection;

internal static class BridgeFixture
{
    [STAThread]
    static void Main(string[] args)
    {
        if (args.Length is < 3 or > 4) throw new ArgumentException("Expected fixture-helper.exe, adapter-marker.jar, settings.json, optional --without-java");
        var assembly = Assembly.Load("ZJHUB.Recorder.Engine");
        var type = assembly.GetType("FlashbackRecorder.RecorderForm", true)!;
        using var form = (Form)Activator.CreateInstance(type, new object?[] { false, args[2], "dark", true })!;
        const BindingFlags fields = BindingFlags.NonPublic | BindingFlags.Instance;
        type.GetField("javaPath", fields)!.SetValue(form, args[0]);
        type.GetField("assetsReady", fields)!.SetValue(form, true);
        type.GetField("javaSearching", fields)!.SetValue(form, false);
        if (args.Length == 4 && args[3] == "--without-java")
        {
            type.GetField("javaPath", fields)!.SetValue(form, "");
            type.GetField("replayPath", fields)!.SetValue(form, Path.Combine(Path.GetDirectoryName(args[2])!, "recording.fbrl"));
            var gameType = assembly.GetType("FlashbackRecorder.GameProcess", true)!;
            var game = Activator.CreateInstance(gameType)!;
            gameType.GetProperty("Pid")!.SetValue(game, 1234);
            gameType.GetProperty("Version")!.SetValue(game, "1.21.10");
            gameType.GetProperty("Supported")!.SetValue(game, true);
            type.GetField("selected", fields)!.SetValue(form, game);
        }
        type.GetMethod("ApplyControls", fields)!.Invoke(form, null);
        assembly.GetType("FlashbackRecorder.RuntimeAssets", true)!
            .GetField("activeProtocolAdapter", BindingFlags.NonPublic | BindingFlags.Static)!.SetValue(null, args[1]);
        // Create only a message-pump handle. Never show or activate a window,
        // attach to Minecraft, extract runtime assets, or use the real settings.
        _ = form.Handle;
        if (form.Visible) throw new InvalidOperationException("Fixture must remain hidden.");
        form.FormClosed += (_, _) => Application.ExitThread();
        form.BeginInvoke((Action)(async () =>
        {
            try { await (Task)type.GetMethod("BridgeStdioLoopAsync", fields)!.Invoke(form, null)!; }
            finally { Application.ExitThread(); }
        }));
        Application.Run();
    }
}
