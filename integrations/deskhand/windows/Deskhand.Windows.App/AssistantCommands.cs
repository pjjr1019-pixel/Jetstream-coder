using System.Diagnostics;
using System.Text;

namespace Deskhand.Windows.App;

internal sealed record AssistantCommandResult(string Status, int? ExitCode, string Output, string? Error);

/// <summary>Runs an approved command as the current Windows user, without elevation.</summary>
internal static class AssistantCommands
{
    internal static async Task<AssistantCommandResult> RunAsync(string command, Func<bool> authorized, CancellationToken cancellation)
    {
        if (!authorized() || cancellation.IsCancellationRequested) return new("cancelled", null, "", "authorization_revoked");
        var start = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "cmd.exe"))
        {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
            RedirectStandardInput = true, WorkingDirectory = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile)
        };
        start.ArgumentList.Add("/d"); start.ArgumentList.Add("/s"); start.ArgumentList.Add("/c"); start.ArgumentList.Add(command);
        Process? process = null;
        try
        {
            if (!authorized() || cancellation.IsCancellationRequested) return new("cancelled", null, "", "authorization_revoked");
            process = Process.Start(start);
            if (process is null) return new("failed", null, "", "process_start_failed");
            process.StandardInput.Close();
            var stdout = Drain(process.StandardOutput); var stderr = Drain(process.StandardError);
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
            timeout.CancelAfter(TimeSpan.FromSeconds(30));
            try { await process.WaitForExitAsync(timeout.Token); }
            catch (OperationCanceledException)
            {
                try { process.Kill(entireProcessTree: true); } catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception) { }
                try { await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5)); } catch (TimeoutException) { }
                return new("uncertain", null, "", cancellation.IsCancellationRequested ? "interrupted" : "timed_out");
            }
            string output;
            try { output = (await stdout.WaitAsync(TimeSpan.FromSeconds(5))) + (await stderr.WaitAsync(TimeSpan.FromSeconds(5))); }
            catch (TimeoutException) { return new("uncertain", process.ExitCode, "", "output_unconfirmed"); }
            if (!authorized() || cancellation.IsCancellationRequested) return new("uncertain", process.ExitCode, output, "authorization_revoked");
            return new(process.ExitCode == 0 ? "completed" : "failed", process.ExitCode, output, process.ExitCode == 0 ? null : "nonzero_exit");
        }
        catch (Exception ex) when (ex is IOException or InvalidOperationException or System.ComponentModel.Win32Exception or UnauthorizedAccessException)
        { return new(process is null ? "failed" : "uncertain", null, "", process is null ? "process_start_failed" : "execution_unconfirmed"); }
        finally { process?.Dispose(); }
    }

    private static async Task<string> Drain(StreamReader reader)
    {
        var text = new StringBuilder(); var buffer = new char[4096]; int count;
        while ((count = await reader.ReadAsync(buffer)) != 0)
            if (text.Length < 16384) text.Append(buffer, 0, Math.Min(count, 16384 - text.Length));
        return text.ToString();
    }
}
