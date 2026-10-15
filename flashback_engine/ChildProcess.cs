using System.Diagnostics;

namespace FlashbackRecorder;

internal static class ChildProcess
{
    public sealed record Result(int ExitCode, string Output);
    public static async Task<Result> RunAsync(ProcessStartInfo info, TimeSpan timeout, CancellationToken cancellation = default)
    {
        info.RedirectStandardOutput = info.RedirectStandardError = true;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        using var child = Process.Start(info) ?? throw new IOException("无法启动辅助进程。");
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        deadline.CancelAfter(timeout);
        var stdout = child.StandardOutput.ReadToEndAsync(deadline.Token);
        var stderr = child.StandardError.ReadToEndAsync(deadline.Token);
        var streams = Task.WhenAll(stdout, stderr);
        // Observe failures even when cancellation forces us to close an inherited pipe.
        _ = streams.ContinueWith(task => _ = task.Exception, CancellationToken.None,
            TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
        try
        {
            await child.WaitForExitAsync(deadline.Token).ConfigureAwait(false);
            // A descendant may keep the parent's stdio handles open after it exits.
            // Never await EOF indefinitely or consume the remainder of a long run timeout.
            await streams.WaitAsync(TimeSpan.FromSeconds(1), deadline.Token).ConfigureAwait(false);
            return new Result(child.ExitCode, ((await stdout.ConfigureAwait(false)) + "\n" + (await stderr.ConfigureAwait(false))).Trim());
        }
        catch (OperationCanceledException)
        {
            cancellation.ThrowIfCancellationRequested();
            throw new TimeoutException($"辅助进程超过 {timeout.TotalSeconds:0} 秒未完成。");
        }
        catch (TimeoutException)
        { throw new TimeoutException("辅助进程已退出，但输出管道未关闭；操作已停止。"); }
        finally
        {
            // Only the helper created here and its descendants are terminated.
            try { if (!child.HasExited) child.Kill(entireProcessTree: true); }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
            deadline.Cancel();
            child.StandardOutput.Dispose();
            child.StandardError.Dispose();
        }
    }
}

