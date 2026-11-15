namespace Deskhand.Windows.App;

internal sealed record AssistantWindowSummary(string Application, int Windows);

/// <summary>Conservative, read-only inventory; it does not grant input authority.</summary>
internal static class AssistantWindows
{
    internal static AssistantWindowSummary[] Summarize(IEnumerable<TextWindowActivator.Window> windows, Func<TextWindowActivator.Window, bool>? eligible = null) =>
        windows.Where(eligible ?? AssistantWindowPolicy.Eligible)
            .GroupBy(w => w.ProcessName, StringComparer.OrdinalIgnoreCase)
            .Select(group => new AssistantWindowSummary(group.Key, group.Count()))
            .OrderBy(item => item.Application, StringComparer.OrdinalIgnoreCase).ToArray();
}

internal sealed record AssistantObservedWindow(string Token, string Application, string Title);
internal interface IAssistantWindowNative
{
    IReadOnlyList<TextWindowActivator.Window> Snapshot();
    bool Activate(IntPtr handle);
    bool Close(IntPtr handle);
}

/// <summary>Short-lived, task-scoped tokens for a conservative application subset.</summary>
internal sealed class AssistantWindowSession(IAssistantWindowNative native, Func<TextWindowActivator.Window, bool>? eligible = null)
{
    private readonly Dictionary<string, (TextWindowActivator.Window Window, DateTimeOffset Expiry)> _observations = [];
    private bool Eligible(TextWindowActivator.Window w) => (eligible ?? AssistantWindowPolicy.Eligible)(w);

    internal AssistantObservedWindow[] Observe()
    {
        _observations.Clear();
        var windows = native.Snapshot().Where(Eligible).Take(50).ToArray();
        return windows.Select(window =>
        {
            var token = Guid.NewGuid().ToString("N");
            // Local models can take longer than ten seconds to choose their next
            // action. Take() still rechecks the window identity and authority.
            _observations[token] = (window, DateTimeOffset.UtcNow.AddSeconds(90));
            return new AssistantObservedWindow(token, window.ProcessName, window.Title);
        }).ToArray();
    }

    internal TextWindowActivator.Window? Peek(string token) => _observations.TryGetValue(token, out var observed) && observed.Expiry > DateTimeOffset.UtcNow ? observed.Window : null;
    internal void Forget(string token) => _observations.Remove(token);

    internal string? Activate(TextWindowActivator.Window observed, Func<bool> authorized)
    {
        if (!authorized()) return "authorization_revoked";
        var current = native.Snapshot().SingleOrDefault(w => w.Handle == observed.Handle && w.ProcessId == observed.ProcessId &&
            w.ClassName == observed.ClassName && w.Title == observed.Title && w.ProcessStartTicks == observed.ProcessStartTicks && Eligible(w));
        if (current is null || !authorized()) return "stale_or_denied";
        return native.Activate(current.Handle) ? null : "foreground_denied";
    }

    internal TextWindowActivator.Window? Take(string token, Func<bool> authorized)
    {
        if (!authorized() || !_observations.Remove(token, out var observation) || observation.Expiry <= DateTimeOffset.UtcNow) return null;
        var current = native.Snapshot().SingleOrDefault(w => w.Handle == observation.Window.Handle && w.ProcessId == observation.Window.ProcessId &&
            w.ClassName == observation.Window.ClassName && w.Title == observation.Window.Title && w.ProcessStartTicks == observation.Window.ProcessStartTicks && Eligible(w));
        return current is null || !authorized() ? null : current;
    }

    internal string? Close(TextWindowActivator.Window observed, Func<bool> authorized)
    {
        if (!authorized()) return "authorization_revoked";
        var current = native.Snapshot().SingleOrDefault(w => w.Handle == observed.Handle && w.ProcessId == observed.ProcessId &&
            w.ClassName == observed.ClassName && w.Title == observed.Title && w.ProcessStartTicks == observed.ProcessStartTicks && Eligible(w));
        if (current is null || !authorized()) return "stale_or_denied";
        if (!native.Close(current.Handle)) return "foreground_or_close_denied";
        // Batch 2 (100.030, H6): PostMessage only queues the close. Verify
        // the window actually went away before reporting success, so a close
        // the app ignored (e.g. a save prompt) stays "close_unconfirmed"
        // (uncertain) instead of claiming completion. Disappearance is keyed
        // on handle + process id + process start ticks only: a title change
        // while the window lives must not read as "gone".
        for (var attempt = 0; attempt < 5; attempt++)
        {
            Thread.Sleep(100);
            if (!authorized()) return "close_unconfirmed";
            if (!native.Snapshot().Any(w => w.Handle == observed.Handle && w.ProcessId == observed.ProcessId &&
                w.ProcessStartTicks == observed.ProcessStartTicks))
                return null;
        }
        return "close_unconfirmed";
    }
}

internal sealed class AssistantWindowNative : IAssistantWindowNative
{
    public IReadOnlyList<TextWindowActivator.Window> Snapshot() => TextWindowActivator.Snapshot();
    public bool Activate(IntPtr handle)
    {
        if (SetForegroundWindow(handle) && GetForegroundWindow() == handle) return true;
        // Batch 2 (100.030): Windows routinely declines a bare foreground
        // request from a background process with no recent local input — the
        // exact state when driving from the phone. Retry once via the
        // foreground thread's input queue (same technique as
        // TextWindowActivator.Activate), restoring a minimized window first.
        // Windows may still decline; the caller then reports
        // foreground_denied as before.
        var current = GetCurrentThreadId();
        var foreground = GetForegroundWindow();
        var foregroundThread = foreground == IntPtr.Zero ? 0 : GetWindowThreadProcessId(foreground, out _);
        var targetThread = GetWindowThreadProcessId(handle, out _);
        var foregroundAttached = foregroundThread != 0 && foregroundThread != current && AttachThreadInput(current, foregroundThread, true);
        var targetAttached = targetThread != 0 && targetThread != current && targetThread != foregroundThread && AttachThreadInput(current, targetThread, true);
        try
        {
            if (IsIconic(handle)) ShowWindow(handle, 9);
            BringWindowToTop(handle);
            SetForegroundWindow(handle);
        }
        finally
        {
            if (targetAttached) AttachThreadInput(current, targetThread, false);
            if (foregroundAttached) AttachThreadInput(current, foregroundThread, false);
        }
        return GetForegroundWindow() == handle;
    }
    public bool Close(IntPtr handle)
    {
        // Batch 2 (100.030): the old foreground precondition made approved
        // closes fail whenever another window came forward between approval
        // and close. WM_CLOSE to a background window is safe and normal, so
        // try to foreground first (most reliable) but post the close by
        // handle regardless. The engine verifies the window actually went
        // away before reporting success.
        if (GetForegroundWindow() != handle) SetForegroundWindow(handle);
        return PostMessage(handle, 0x0010, IntPtr.Zero, IntPtr.Zero);
    }
    [System.Runtime.InteropServices.DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr handle);
    [System.Runtime.InteropServices.DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)] private static extern bool PostMessage(IntPtr handle, uint message, IntPtr wParam, IntPtr lParam);
    [System.Runtime.InteropServices.DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);
    [System.Runtime.InteropServices.DllImport("user32.dll")] private static extern bool AttachThreadInput(uint from, uint to, bool attach);
    [System.Runtime.InteropServices.DllImport("user32.dll")] private static extern bool BringWindowToTop(IntPtr handle);
    [System.Runtime.InteropServices.DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr handle, int command);
    [System.Runtime.InteropServices.DllImport("user32.dll")] private static extern bool IsIconic(IntPtr handle);
    [System.Runtime.InteropServices.DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
}
