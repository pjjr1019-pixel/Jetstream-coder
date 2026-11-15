using System.Diagnostics;
using System.Net;
using System.Net.Sockets;

namespace Deskhand.Windows.App;

internal static class TailscaleAddress
{
    internal static string RelayUrl(string output, int port)
    {
        if (port is < 1 or > 65535 || !IPAddress.TryParse(output.Trim(), out var ip) || ip.AddressFamily != AddressFamily.InterNetwork)
            throw new InvalidOperationException("Tailscale did not return an IPv4 address.");
        var bytes = ip.GetAddressBytes();
        if (bytes[0] != 100 || bytes[1] is < 64 or > 127)
            throw new InvalidOperationException("The address is not a Tailscale IPv4 address.");
        return $"ws://{ip}:{port}";
    }

    internal static async Task<string> DiscoverAsync(int port)
    {
        var exe = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Tailscale", "tailscale.exe");
        if (!File.Exists(exe)) throw new InvalidOperationException("Install and connect Tailscale on this PC first.");
        using var process = new Process { StartInfo = new ProcessStartInfo(exe) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true } };
        process.StartInfo.ArgumentList.Add("ip");
        process.StartInfo.ArgumentList.Add("-4");
        if (!process.Start()) throw new InvalidOperationException("Could not check Tailscale.");
        var output = process.StandardOutput.ReadToEndAsync();
        var error = process.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try { await process.WaitForExitAsync(timeout.Token); }
        catch (OperationCanceledException) { try { process.Kill(); } catch (InvalidOperationException) { } throw new InvalidOperationException("Tailscale did not respond."); }
        await error;
        if (process.ExitCode != 0) throw new InvalidOperationException("Connect Tailscale on this PC and try again.");
        return RelayUrl(await output, port);
    }
}
