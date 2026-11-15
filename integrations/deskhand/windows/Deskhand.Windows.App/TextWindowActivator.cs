using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace Deskhand.Windows.App;

/// <summary>Finds the window created or changed by a text-file shell activation.</summary>
internal static class TextWindowActivator
{
    internal sealed record Window(IntPtr Handle, uint ProcessId, string Title, string ProcessName, string ClassName = "", long ProcessStartTicks = 0);
    internal static IReadOnlyList<Window> Snapshot()
    {
        var windows = new List<Window>();
        EnumWindows((handle, _) =>
        {
            if (!IsWindowVisible(handle) || GetWindow(handle, 4) != IntPtr.Zero) return true;
            var title = new StringBuilder(2048);
            if (GetWindowTextW(handle, title, title.Capacity) == 0) return true;
            GetWindowThreadProcessId(handle, out var pid);
            try
            {
                using var process = Process.GetProcessById((int)pid);
                var className = new StringBuilder(256);
                GetClassNameW(handle, className, className.Capacity);
                windows.Add(new Window(handle, pid, title.ToString(), process.ProcessName, className.ToString(), process.StartTime.ToUniversalTime().Ticks));
            }
            catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception) { }
            return true;
        }, IntPtr.Zero);
        return windows;
    }

    // Only match a new/changed window of the shell-returned process, or a Notepad
    // window naming the file. The latter supports modern Notepad's reused process.
    internal static Window? FindTarget(string fileName, IReadOnlyList<Window> before, IReadOnlyList<Window> after, int? processId)
    {
        var candidates = after.Where(w => NamesFile(w.Title, fileName) &&
            (w.ProcessId == processId || w.ProcessName.Equals("Notepad", StringComparison.OrdinalIgnoreCase))).ToList();
        var changed = candidates.Where(w => !before.Any(old => old.Handle == w.Handle && old.ProcessId == w.ProcessId && old.Title == w.Title)).ToList();
        if (changed.Count == 1) return changed[0];
        // A single already-open Notepad window with this title is a safe reuse case.
        return changed.Count == 0 && candidates.Count == 1 && candidates[0].ProcessName.Equals("Notepad", StringComparison.OrdinalIgnoreCase)
            ? candidates[0] : null;
    }

    private static bool NamesFile(string title, string fileName) =>
        title.TrimStart('*').StartsWith(fileName + " - ", StringComparison.OrdinalIgnoreCase) ||
        title.TrimStart('*').Equals(fileName, StringComparison.OrdinalIgnoreCase);

    // Batch 2 (100.030): the fire-and-forget activation poller is removed —
    // zero callers anywhere in the snapshot (its only consumer would have
    // been FindTarget). The live path is ActivateLaunchWhenReady.

    // Shell launches can reuse processes and windows. Prefer a changed, named window;
    // never choose arbitrarily when multiple windows could belong to this launch.
    internal static Window? FindLaunchTarget(string path, IReadOnlyList<Window> before,
        IReadOnlyList<Window> after, int? processId, bool folder)
    {
        var name = Path.GetFileName(path.TrimEnd('\\', '/'));
        var stem = Path.GetFileNameWithoutExtension(name);
        var executable = Path.GetExtension(path).Equals(".exe", StringComparison.OrdinalIgnoreCase);
        bool Named(Window w) => NamesFile(w.Title, name) || (!folder && stem.Length > 0 && NamesFile(w.Title, stem));
        var candidates = after.Where(w => w.ProcessId != Environment.ProcessId &&
            (folder ? w.ClassName == "CabinetWClass" && Named(w)
                : Named(w) || (processId.HasValue && w.ProcessId == processId)
                    || (executable && w.ProcessName.Equals(stem, StringComparison.OrdinalIgnoreCase)))).ToArray();
        var changed = candidates.Where(w => !before.Any(b => b.Handle == w.Handle && b.ProcessId == w.ProcessId && b.Title == w.Title)).ToArray();
        if (changed.Length == 1) return changed[0];
        return changed.Length == 0 && candidates.Length == 1 ? candidates[0] : null;
    }

    internal static async Task ActivateLaunchWhenReady(string path, IReadOnlyList<Window> before,
        int? processId, bool folder, bool maximize, CancellationToken cancellation)
    {
        try
        {
            for (var i = 0; i < 30; i++)
            {
                await Task.Delay(200, cancellation);
                var target = FindLaunchTarget(path, before, Snapshot(), processId, folder);
                if (target is null) continue;
                // Give a reused application time to finish switching documents.
                if (i < 3 && before.Any(w => w.Handle == target.Handle && w.Title == target.Title)) continue;
                cancellation.ThrowIfCancellationRequested();
                Activate(target.Handle, maximize);
                if (GetForegroundWindow() == target.Handle) return;
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception) { }
    }

    internal static void Activate(IntPtr handle, bool maximize = true)
    {
        if (!IsWindow(handle)) return;
        // Attach briefly to the foreground input queue to request activation while
        // respecting Windows' windowing policy; never leave a window always-on-top.
        var current = GetCurrentThreadId();
        var foreground = GetForegroundWindow();
        var foregroundThread = foreground == IntPtr.Zero ? 0 : GetWindowThreadProcessId(foreground, out _);
        var targetThread = GetWindowThreadProcessId(handle, out _);
        var foregroundAttached = foregroundThread != 0 && foregroundThread != current && AttachThreadInput(current, foregroundThread, true);
        var targetAttached = targetThread != 0 && targetThread != current && targetThread != foregroundThread && AttachThreadInput(current, targetThread, true);
        try
        {
            if (maximize) ShowWindow(handle, 3); // SW_MAXIMIZE
            else if (IsIconic(handle)) ShowWindow(handle, 9); // SW_RESTORE; preserve normal/maximized sizing otherwise.
            BringWindowToTop(handle);
            SetForegroundWindow(handle);
        }
        finally
        {
            if (targetAttached) AttachThreadInput(current, targetThread, false);
            if (foregroundAttached) AttachThreadInput(current, foregroundThread, false);
        }
    }

    private delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassNameW(IntPtr hwnd, StringBuilder text, int max);
    [DllImport("user32.dll")] private static extern IntPtr GetWindow(IntPtr hwnd, uint command);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowTextW(IntPtr hwnd, StringBuilder text, int max);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] private static extern bool BringWindowToTop(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool AttachThreadInput(uint from, uint to, bool attach);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
}
