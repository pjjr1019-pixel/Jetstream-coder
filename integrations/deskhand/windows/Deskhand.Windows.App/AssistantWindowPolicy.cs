using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;

namespace Deskhand.Windows.App;

/// <summary>Only ordinary, same-user windows on the current interactive session are eligible.</summary>
internal static class AssistantWindowPolicy
{
    private static readonly HashSet<string> DeniedProcesses = new(StringComparer.OrdinalIgnoreCase)
    {
        "deskhand", "credentialuibroker", "logonui", "winlogon", "lockapp", "consent", "lsass", "securityhealthsystray",
        // Commands require the separate per-command approval flow. Do not let
        // control input become an alternate path into a terminal process.
        "cmd", "powershell", "pwsh", "windowsterminal", "wt", "conhost", "bash", "wsl", "ssh", "mintty", "wezterm-gui", "alacritty"
    };
    private static readonly string[] ProtectedTerms = ["password", "credential", "sign in", "sign-in", "windows security"];

    internal static bool Eligible(TextWindowActivator.Window window)
    {
        if (window.ProcessId == 0 || window.ProcessId == Environment.ProcessId || DeniedProcesses.Contains(window.ProcessName) ||
            string.IsNullOrWhiteSpace(window.Title) || window.ClassName is "Credential Dialog Xaml Host" or "CredentialUIBroker" or "#32770" or "ConsoleWindowClass" or "CASCADIA_HOSTING_WINDOW_CLASS" ||
            ProtectedTerms.Any(term => window.Title.Contains(term, StringComparison.OrdinalIgnoreCase)) ||
            (window.ProcessName.Equals("explorer", StringComparison.OrdinalIgnoreCase) && window.ClassName != "CabinetWClass")) return false;
        try
        {
            using var process = Process.GetProcessById(checked((int)window.ProcessId));
            using var current = Process.GetCurrentProcess();
            if (process.SessionId != current.SessionId || process.StartTime.ToUniversalTime().Ticks != window.ProcessStartTicks ||
                !process.ProcessName.Equals(window.ProcessName, StringComparison.OrdinalIgnoreCase)) return false;
            if (!OpenProcessToken(process.Handle, 0x0008, out var token)) return false;
            using (token)
            using (var owner = new WindowsIdentity(token.DangerousGetHandle()))
            using (var self = WindowsIdentity.GetCurrent())
            {
                if (owner.User is null || self.User is null || !owner.User.Equals(self.User)) return false;
                // Never use this path to target an elevated process.
                if (!GetTokenInformation(token, 20, out var elevation, sizeof(int), out _) || elevation != 0) return false;
            }
            return true;
        }
        catch (Exception ex) when (ex is ArgumentException or OverflowException or InvalidOperationException or Win32Exception or UnauthorizedAccessException or System.Security.SecurityException)
        { return false; }
    }

    internal static bool EligibleChildProcess(int processId)
    {
        if (processId <= 0 || processId == Environment.ProcessId) return false;
        try
        {
            using var process = Process.GetProcessById(processId);
            using var current = Process.GetCurrentProcess();
            if (DeniedProcesses.Contains(process.ProcessName) || process.SessionId != current.SessionId) return false;
            if (!OpenProcessToken(process.Handle, 0x0008, out var token)) return false;
            using (token)
            using (var owner = new WindowsIdentity(token.DangerousGetHandle()))
            using (var self = WindowsIdentity.GetCurrent())
                return owner.User is not null && self.User is not null && owner.User.Equals(self.User) &&
                    GetTokenInformation(token, 20, out var elevation, sizeof(int), out _) && elevation == 0;
        }
        catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or Win32Exception or UnauthorizedAccessException or System.Security.SecurityException)
        { return false; }
    }

    [DllImport("advapi32.dll", SetLastError = true)] private static extern bool OpenProcessToken(IntPtr process, uint access, out SafeAccessTokenHandle token);
    [DllImport("advapi32.dll", SetLastError = true)] private static extern bool GetTokenInformation(SafeAccessTokenHandle token, int kind, out int info, int length, out int returned);
}
