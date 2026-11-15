using System.Runtime.InteropServices;
using System.Text;

namespace Deskhand.Windows.App;

internal interface IForegroundNative
{
    IntPtr Foreground();
    bool IsUsable(IntPtr hwnd);
    string? ClassName(IntPtr hwnd);
    bool Post(IntPtr hwnd, uint message, IntPtr parameter);
}
internal static class ForegroundWindows
{
    public static string? Act(string action) => Act(action, new Native());
    internal static string? Act(string action, IForegroundNative windows)
    {
        if (action is not ("close" or "minimize")) return "malformed";
        var hwnd = windows.Foreground();
        if (hwnd == IntPtr.Zero || !windows.IsUsable(hwnd)) return "empty";
        var name = windows.ClassName(hwnd);
        if (name is null) return "unknown";
        if (name is "Progman" or "WorkerW" or "Shell_TrayWnd" or "Shell_SecondaryTrayWnd") return "empty";
        if (windows.Foreground() != hwnd) return "changed";
        var message = action == "close" ? 0x0010u : 0x0112u; // WM_CLOSE or WM_SYSCOMMAND
        var parameter = action == "minimize" ? new IntPtr(0xF020) : IntPtr.Zero; // SC_MINIMIZE
        return windows.Post(hwnd, message, parameter) ? null : "denied";
    }
    private sealed class Native : IForegroundNative
    {
        public IntPtr Foreground() => GetForegroundWindow();
        public bool IsUsable(IntPtr hwnd) => hwnd != GetShellWindow() && hwnd != GetDesktopWindow() && IsWindow(hwnd) && IsWindowVisible(hwnd);
        public string? ClassName(IntPtr hwnd) { var name = new StringBuilder(256); return GetClassName(hwnd, name, name.Capacity) == 0 ? null : name.ToString(); }
        public bool Post(IntPtr hwnd, uint message, IntPtr parameter) => PostMessage(hwnd, message, parameter, IntPtr.Zero);
    }
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern IntPtr GetShellWindow();
    [DllImport("user32.dll")] private static extern IntPtr GetDesktopWindow();
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd, StringBuilder name, int count);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool PostMessage(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam);
}
