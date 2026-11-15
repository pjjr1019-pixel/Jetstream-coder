using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Deskhand.Windows.App;

internal sealed record LaunchWindow(IntPtr Handle, uint ProcessId, long ProcessStart, long Generation,
    string ClassName, string Title, string ProcessName);
internal interface ILaunchWindows : IDisposable
{
    IReadOnlyList<LaunchWindow> Snapshot();
    bool IsCurrent(LaunchWindow window);
    string? RequestClose(LaunchWindow window);
}

/// <summary>Only new windows attributed to explicit Files launches enter this session's stack.</summary>
internal sealed class WindowLaunchHistory(ILaunchWindows windows) : IDisposable
{
    internal sealed class Launch(string path, string target, IReadOnlyList<LaunchWindow> before)
    {
        public string Path { get; } = path;
        public string Target { get; } = target;
        public IReadOnlyList<LaunchWindow> Before { get; } = before;
        public LaunchWindow? Window;
        public bool Pending = true;
    }
    private readonly object _gate = new();
    private readonly List<Launch> _stack = [];
    private bool _ended;

    public Launch Begin(string path)
    {
        var launch = new Launch(path, ResolveShortcut(path), windows.Snapshot());
        lock (_gate) { if (!_ended) _stack.Add(launch); }
        return launch;
    }
    public void Failed(Launch launch) { lock (_gate) launch.Pending = false; }
    internal void Resolved(Launch launch, LaunchWindow? window)
    { lock (_gate) { if (!_ended) { launch.Window = window; launch.Pending = false; } } }

    public void ResolveWhenReady(Launch launch, int? processId)
    {
        _ = Task.Run(async () =>
        {
            try
            {
                for (var i = 0; i < 60; i++)
                {
                    lock (_gate) { if (_ended) return; }
                    await Task.Delay(100);
                    var found = FindTarget(launch.Path, launch.Target, processId, launch.Before, windows.Snapshot());
                    if (found is null) continue;
                    Resolved(launch, found);
                    return;
                }
            }
            catch (Exception ex) when (ex is InvalidOperationException or ObjectDisposedException or COMException or System.ComponentModel.Win32Exception) { }
            finally { lock (_gate) launch.Pending = false; }
        });
    }

    internal static LaunchWindow? FindTarget(string path, string target, int? pid,
        IReadOnlyList<LaunchWindow> before, IReadOnlyList<LaunchWindow> after)
    {
        var name = Path.GetFileName(target.TrimEnd(Path.DirectorySeparatorChar));
        var shortcutName = Path.GetFileNameWithoutExtension(path);
        var executable = Path.GetExtension(target).Equals(".exe", StringComparison.OrdinalIgnoreCase)
            ? Path.GetFileNameWithoutExtension(target) : null;
        var candidates = after.Where(w => !before.Any(b => b.Handle == w.Handle && b.ProcessId == w.ProcessId && b.Generation == w.Generation)
            && (w.ProcessId == pid
                || (executable is not null && w.ProcessName.Equals(executable, StringComparison.OrdinalIgnoreCase))
                || (w.ProcessName.Equals("Notepad", StringComparison.OrdinalIgnoreCase) && Names(w.Title, name))
                || (w.ClassName == "CabinetWClass" && (Names(w.Title, name) || Names(w.Title, shortcutName))))).ToArray();
        return candidates.Length == 1 ? candidates[0] : null;
    }
    private static bool Names(string title, string name) => name.Length > 0 &&
        (title.Equals(name, StringComparison.OrdinalIgnoreCase) || title.TrimStart('*').StartsWith(name + " - ", StringComparison.OrdinalIgnoreCase));

    private static string ResolveShortcut(string path)
    {
        if (!Path.GetExtension(path).Equals(".lnk", StringComparison.OrdinalIgnoreCase)) return path;
        object? shell = null, shortcut = null;
        try
        {
            var type = Type.GetTypeFromProgID("WScript.Shell");
            if (type is null) return path;
            shell = Activator.CreateInstance(type);
            shortcut = ((dynamic)shell!).CreateShortcut(path);
            string target = ((dynamic)shortcut).TargetPath;
            return string.IsNullOrEmpty(target) ? path : target;
        }
        catch (Exception ex) when (ex is COMException or Microsoft.CSharp.RuntimeBinder.RuntimeBinderException) { return path; }
        finally
        {
            if (shortcut is not null && Marshal.IsComObject(shortcut)) Marshal.FinalReleaseComObject(shortcut);
            if (shell is not null && Marshal.IsComObject(shell)) Marshal.FinalReleaseComObject(shell);
        }
    }

    public (string? Error, string? Title) CloseLatest()
    {
        lock (_gate)
        {
            if (_ended) return ("denied", null);
            while (_stack.Count > 0)
            {
                var last = _stack[^1];
                if (last.Pending) return ("busy", null);
                // An ambiguous/reused launch is a barrier: never silently close an older window instead.
                if (last.Window is null) return ("untracked", Path.GetFileName(last.Path));
                if (!windows.IsCurrent(last.Window)) { _stack.RemoveAt(_stack.Count - 1); continue; }
                var error = windows.RequestClose(last.Window);
                return (error, last.Window.Title);
            }
            return ("empty", null);
        }
    }
    public void Dispose()
    {
        lock (_gate) { if (_ended) return; _ended = true; _stack.Clear(); }
        windows.Dispose();
    }
}

/// <summary>A message-pumped destroy hook prevents a recycled HWND from becoming a close target.</summary>
internal sealed class NativeLaunchWindows : ILaunchWindows
{
    private readonly Control _dispatcher;
    private readonly Dictionary<IntPtr, long> _generations = [];
    private readonly WinEventProc _callback;
    private volatile bool _disposed;
    public NativeLaunchWindows()
    {
        using var ready = new ManualResetEventSlim();
        Control? dispatcher = null;
        Exception? failure = null;
        _callback = (_, _, hwnd, obj, child, _, _) =>
        { if (hwnd != IntPtr.Zero && obj == 0 && child == 0 && _generations.ContainsKey(hwnd)) _generations[hwnd]++; };
        var thread = new Thread(() =>
        {
            IntPtr hook = IntPtr.Zero;
            try
            {
                dispatcher = new Control();
                _ = dispatcher.Handle;
                hook = SetWinEventHook(0x8001, 0x8001, IntPtr.Zero, _callback, 0, 0, 0);
                if (hook == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                ready.Set();
                Application.Run();
            }
            catch (Exception ex) { failure = ex; ready.Set(); }
            finally { if (hook != IntPtr.Zero) UnhookWinEvent(hook); dispatcher?.Dispose(); }
        }) { IsBackground = true, Name = "Deskhand window history" };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        ready.Wait();
        if (failure is not null) throw new InvalidOperationException("Window history unavailable", failure);
        _dispatcher = dispatcher!;
    }
    private T OnThread<T>(Func<T> action) => _disposed ? throw new ObjectDisposedException(nameof(NativeLaunchWindows))
        : (T)_dispatcher.Invoke(action);
    public IReadOnlyList<LaunchWindow> Snapshot() => OnThread<IReadOnlyList<LaunchWindow>>(() =>
    {
        var result = new List<LaunchWindow>();
        foreach (var window in TextWindowActivator.Snapshot())
        {
            if (window.ProcessId == Environment.ProcessId) continue;
            var identity = Identity(window.Handle, window.Title, window.ProcessName);
            if (identity is not null && identity.ClassName is not ("Progman" or "WorkerW" or "Shell_TrayWnd" or "Shell_SecondaryTrayWnd")) result.Add(identity);
        }
        return result;
    });
    private LaunchWindow? Identity(IntPtr handle, string title, string name)
    {
        if (!IsWindow(handle)) return null;
        GetWindowThreadProcessId(handle, out var pid);
        try
        {
            using var process = Process.GetProcessById((int)pid);
            var cls = new System.Text.StringBuilder(256);
            GetClassNameW(handle, cls, cls.Capacity);
            _generations.TryAdd(handle, 0);
            return new(handle, pid, process.StartTime.ToUniversalTime().Ticks, _generations.GetValueOrDefault(handle), cls.ToString(), title, name);
        }
        catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception) { return null; }
    }
    private bool Current(LaunchWindow target)
    {
        var now = Identity(target.Handle, "", "");
        return now is not null && now.ProcessId == target.ProcessId && now.ProcessStart == target.ProcessStart
            && now.Generation == target.Generation && now.ClassName == target.ClassName;
    }
    public bool IsCurrent(LaunchWindow target) => OnThread(() => Current(target));
    internal LaunchWindow? Inspect(IntPtr hwnd) => OnThread(() => Identity(hwnd, "Fixture window", "Fixture"));
    public string? RequestClose(LaunchWindow target) => OnThread<string?>(() =>
    {
        if (!Current(target)) return "missing";
        if (!IsWindowEnabled(target.Handle)) return "prompt";
        // WM_CLOSE asks the application to close normally, including its save prompts. Never kill a process.
        return PostMessageW(target.Handle, 0x0010, IntPtr.Zero, IntPtr.Zero) ? null : "denied";
    });
    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        try { _dispatcher.BeginInvoke((Action)Application.ExitThread); }
        catch (Exception ex) when (ex is InvalidOperationException or ObjectDisposedException) { }
    }
    private delegate void WinEventProc(IntPtr hook, uint ev, IntPtr hwnd, int obj, int child, uint thread, uint time);
    [DllImport("user32.dll", SetLastError = true)] private static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr module, WinEventProc callback, uint process, uint thread, uint flags);
    [DllImport("user32.dll")] private static extern bool UnhookWinEvent(IntPtr hook);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsWindowEnabled(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassNameW(IntPtr hwnd, System.Text.StringBuilder name, int count);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool PostMessageW(IntPtr hwnd, uint message, IntPtr wparam, IntPtr lparam);
}
