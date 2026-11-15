using System.Drawing;
using System.Runtime.InteropServices;
using System.Security.Cryptography;

namespace Deskhand.Windows.App;

internal interface IAssistantPointerNative
{
    string? Click(TextWindowActivator.Window window, double x, double y, Func<bool> authorized);
    string? RightClick(TextWindowActivator.Window window, double x, double y, Func<bool> authorized);
    string? DoubleClick(TextWindowActivator.Window window, double x, double y, Func<bool> authorized);
    string? Move(TextWindowActivator.Window window, double x, double y, Func<bool> authorized);
    string? Drag(TextWindowActivator.Window window, double x, double y, double toX, double toY, bool rightButton, Func<bool> authorized);
    string? Scroll(TextWindowActivator.Window window, double x, double y, int steps, Func<bool> authorized);
    // G5 (100.030): cursor read-back. The static entry points call this after
    // move/drag/scroll; the position feeds the pointer/drag/scroll evidence
    // kinds. LastDragStartCursor is the read-back taken at the drag start
    // (after the positioning move, before button-down); null when the last
    // drag never positioned or the read-back failed.
    bool TryGetCursorPosition(out int x, out int y);
    (int X, int Y)? LastDragStartCursor { get; }
}

internal static class AssistantPointer
{
    // G5 (100.030): cursor read-back tolerance for the pointer/drag evidence
    // kinds, in physical pixels on the observation's monitor scale. The
    // comparison itself happens in the evidence builder; this number is the
    // single recorded value both sides use.
    internal const int CursorTolerancePx = 2;

    internal static async Task<AssistantControlResult> GestureAsync(AssistantScreenObservation observation, double x, double y, double toX, double toY,
        bool drag, Func<bool> authorized, IAssistantPointerNative native, bool rightButton = false)
    {
        if (new[] { x, y, toX, toY }.Any(v => !double.IsFinite(v) || v < 0 || v > 1)) return new("rejected", "invalid_position");
        // Batch 2: never act on a frame older than 15s. The observation
        // carries the capture time; the fresh capture + hash check below
        // still guards against the window changing since then.
        if (DateTimeOffset.UtcNow - observation.CapturedAt > TimeSpan.FromSeconds(15)) return new("rejected", "stale_observation");
        var current = await AssistantScreenshot.CaptureAsync(observation.Window, authorized);
        if (current.Error is not null || current.JpegBase64 is null) return new("rejected", current.Error ?? "capture_unavailable");
        var hash = SHA256.HashData(Convert.FromBase64String(current.JpegBase64));
        if (current.Width != observation.Width || current.Height != observation.Height || !FrameMatches(observation, current, hash)) return new("rejected", "window_changed");
        if (!authorized()) return new("cancelled", "authorization_revoked");
        var error = drag ? native.Drag(observation.Window, x, y, toX, toY, rightButton, authorized) : native.Move(observation.Window, x, y, authorized);
        // G5 (100.030): read the cursor back after the move/drag. The
        // position feeds the pointer/drag evidence kinds (cursor_match,
        // end_match); the drag-start read-back comes from the native layer.
        // A failed read-back leaves Cursor null and the evidence downgrades.
        (int X, int Y)? cursor = null;
        if (native.TryGetCursorPosition(out var cursorX, out var cursorY)) cursor = (cursorX, cursorY);
        var dragStart = drag ? native.LastDragStartCursor : null;
        // Batch 2 (100.030, H6): a fully dispatched gesture is completed.
        // SendInput success proves the input reached the verified foreground
        // window; it does not prove what the app did with it afterwards.
        // A partial send stays uncertain.
        return error is null ? new("completed", null, cursor, dragStart) : error == "input_unconfirmed" ? new("uncertain", error, cursor, dragStart) : new("rejected", error, cursor, dragStart);
    }
    internal static async Task<AssistantControlResult> ClickAsync(AssistantScreenObservation observation, double x, double y,
        Func<bool> authorized, IAssistantPointerNative native)
        => await DispatchAsync(observation, x, y, 0, false, false, authorized, native);

    internal static async Task<AssistantControlResult> DoubleClickAsync(AssistantScreenObservation observation, double x, double y,
        Func<bool> authorized, IAssistantPointerNative native)
        => await DispatchAsync(observation, x, y, 0, true, false, authorized, native);

    // G1 (100.030): same observation-token binding and approval pattern as
    // ClickAsync. The approval prompt text ("Right-click" + target) lives in
    // the engine (AssistantActionEngine) — this method takes the same
    // prompt/target parameters as ClickAsync and hardcodes no prompt.
    internal static async Task<AssistantControlResult> RightClickAsync(AssistantScreenObservation observation, double x, double y,
        Func<bool> authorized, IAssistantPointerNative native)
        => await DispatchAsync(observation, x, y, 0, false, true, authorized, native);

    internal static async Task<AssistantControlResult> ScrollAsync(AssistantScreenObservation observation, double x, double y, int steps,
        Func<bool> authorized, IAssistantPointerNative native)
        => steps is < -3 or > 3 or 0 ? new("rejected", "invalid_scroll") : await DispatchAsync(observation, x, y, steps, false, false, authorized, native);

    private static async Task<AssistantControlResult> DispatchAsync(AssistantScreenObservation observation, double x, double y, int steps, bool doubleClick, bool rightClick,
        Func<bool> authorized, IAssistantPointerNative native)
    {
        if (!double.IsFinite(x) || !double.IsFinite(y) || x < 0 || x > 1 || y < 0 || y > 1) return new("rejected", "invalid_position");
        // Batch 2: never act on a frame older than 15s. The observation
        // carries the capture time; the fresh capture + hash check below
        // still guards against the window changing since then.
        if (DateTimeOffset.UtcNow - observation.CapturedAt > TimeSpan.FromSeconds(15)) return new("rejected", "stale_observation");
        var current = await AssistantScreenshot.CaptureAsync(observation.Window, authorized);
        if (current.Error is not null || current.JpegBase64 is null) return new("rejected", current.Error ?? "capture_unavailable");
        var hash = SHA256.HashData(Convert.FromBase64String(current.JpegBase64));
        if (current.Width != observation.Width || current.Height != observation.Height || !FrameMatches(observation, current, hash))
            return new("rejected", "window_changed");
        if (!authorized()) return new("cancelled", "authorization_revoked");
        var error = steps != 0 ? native.Scroll(observation.Window, x, y, steps, authorized) : doubleClick
            ? native.DoubleClick(observation.Window, x, y, authorized) : rightClick
            ? native.RightClick(observation.Window, x, y, authorized) : native.Click(observation.Window, x, y, authorized);
        // G5 (100.030): read the cursor back after a scroll; the position
        // feeds the scroll evidence kind. Clicks take no read-back — their
        // evidence is the existing invoke kind (post-observe changed).
        (int X, int Y)? cursor = null;
        if (steps != 0 && native.TryGetCursorPosition(out var cursorX, out var cursorY)) cursor = (cursorX, cursorY);
        // Batch 2 (100.030, H6): a fully sent click/scroll is completed — the
        // input provably reached the verified foreground window. Only a
        // partial send stays uncertain. The app's reaction afterwards is
        // still not verified; a later screenshot is the way to confirm it.
        return error is null ? new("completed", null, cursor) : error == "input_unconfirmed" ? new("uncertain", error, cursor) : new("rejected", error, cursor);
    }

    // Batch 2 (100.030): a byte-identical JPEG hash rejected clicks on any
    // animated window (blinking caret, clock, spinner). An exact hash match
    // still passes immediately; otherwise both frames are decoded and
    // compared at 64x36, tolerating up to 5% of sampled pixels differing by
    // more than 32 per channel. Anything else — including undecodable
    // frames — falls back to the strict rejection. Dimensions are checked
    // by the caller before this runs.
    private static bool FrameMatches(AssistantScreenObservation observation, AssistantScreenshotResult current, byte[] currentHash)
    {
        if (CryptographicOperations.FixedTimeEquals(currentHash, observation.ImageHash)) return true;
        if (observation.ImageBytes is null || current.JpegBase64 is null) return false;
        try
        {
            using var oldStream = new MemoryStream(observation.ImageBytes);
            using var newStream = new MemoryStream(Convert.FromBase64String(current.JpegBase64));
            using var oldImage = new Bitmap(oldStream);
            using var newImage = new Bitmap(newStream);
            if (oldImage.Width != newImage.Width || oldImage.Height != newImage.Height) return false;
            const int sampleWidth = 64, sampleHeight = 36;
            using var oldSmall = new Bitmap(sampleWidth, sampleHeight);
            using var newSmall = new Bitmap(sampleWidth, sampleHeight);
            using (var g = Graphics.FromImage(oldSmall)) g.DrawImage(oldImage, 0, 0, sampleWidth, sampleHeight);
            using (var g = Graphics.FromImage(newSmall)) g.DrawImage(newImage, 0, 0, sampleWidth, sampleHeight);
            var differing = 0;
            for (var y = 0; y < sampleHeight; y++)
                for (var x = 0; x < sampleWidth; x++)
                {
                    var a = oldSmall.GetPixel(x, y); var b = newSmall.GetPixel(x, y);
                    if (Math.Abs(a.R - b.R) > 32 || Math.Abs(a.G - b.G) > 32 || Math.Abs(a.B - b.B) > 32) differing++;
                }
            return differing * 20 <= sampleWidth * sampleHeight;
        }
        catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or ExternalException or OutOfMemoryException)
        { return false; }
    }
}

internal sealed class AssistantPointerNative : IAssistantPointerNative
{
    public string? Move(TextWindowActivator.Window window, double x, double y, Func<bool> authorized) => Gesture(window, x, y, x, y, false, false, authorized);
    public string? Drag(TextWindowActivator.Window window, double x, double y, double toX, double toY, bool rightButton, Func<bool> authorized) => Gesture(window, x, y, toX, toY, true, rightButton, authorized);

    // G5 (100.030): the drag-start cursor read-back, set inside Gesture after
    // the positioning move (drag only). Read via the interface so the static
    // entry points can attach it to the result.
    public (int X, int Y)? LastDragStartCursor => _lastDragStartCursor;
    private (int X, int Y)? _lastDragStartCursor;

    public bool TryGetCursorPosition(out int x, out int y)
    {
        if (GetCursorPos(out var point)) { x = point.X; y = point.Y; return true; }
        x = 0; y = 0; return false;
    }

    private string? Gesture(TextWindowActivator.Window window, double x, double y, double toX, double toY, bool drag, bool rightButton, Func<bool> authorized)
    {
        _lastDragStartCursor = null;
        if (new[] { x, y, toX, toY }.Any(v => !double.IsFinite(v) || v < 0 || v > 1)) return "invalid_position";
        if (!authorized()) return "authorization_revoked";
        if (GetForegroundWindow() != window.Handle) return "window_not_foreground";
        if (!GetWindowRect(window.Handle, out var rect)) return "window_bounds_unavailable";
        var width = rect.Right - rect.Left; var height = rect.Bottom - rect.Top;
        var left = GetSystemMetrics(76); var top = GetSystemMetrics(77); var virtualWidth = GetSystemMetrics(78); var virtualHeight = GetSystemMetrics(79);
        if (width <= 1 || height <= 1 || virtualWidth <= 1 || virtualHeight <= 1) return "window_changed";
        var positions = new List<(int X, int Y)>();
        var segments = drag ? 12 : 0;
        for (var i = 0; i <= segments; i++)
        {
            var fraction = segments == 0 ? 0 : (double)i / segments;
            var point = new POINT { X = rect.Left + (int)Math.Round((x + (toX - x) * fraction) * (width - 1)), Y = rect.Top + (int)Math.Round((y + (toY - y) * fraction) * (height - 1)) };
            var target = WindowFromPoint(point);
            GetWindowThreadProcessId(target, out var processId);
            if (target == IntPtr.Zero || GetAncestor(target, 2) != window.Handle || processId != window.ProcessId) return "target_obscured";
            if (point.X < left || point.Y < top || point.X >= left + virtualWidth || point.Y >= top + virtualHeight) return "position_offscreen";
            positions.Add(((int)Math.Round((point.X - left) * 65535.0 / (virtualWidth - 1)), (int)Math.Round((point.Y - top) * 65535.0 / (virtualHeight - 1))));
        }
        if (!authorized()) return "authorization_revoked";
        if (GetForegroundWindow() != window.Handle) return "window_not_foreground";
        if (!GetWindowRect(window.Handle, out var current)) return "window_bounds_unavailable";
        if (!current.Equals(rect)) return "window_changed";
        var start = new[] { Mouse(positions[0].X, positions[0].Y, 0x0001 | 0x8000 | 0x4000) };
        if (SendInput(1, start, Marshal.SizeOf<INPUT>()) != 1) return "input_unconfirmed";
        if (!drag) return null;
        // G5 (100.030): read the cursor back at the drag start — the drag
        // evidence kind needs both ends (start_match + end_match).
        if (GetCursorPos(out var startCursor)) _lastDragStartCursor = (startCursor.X, startCursor.Y);
        if (!authorized() || GetForegroundWindow() != window.Handle) return "input_unconfirmed";
        // Right-button drag is reserved (protocol exposes left only in v1):
        // the flags below mirror the left down/up (0x0002/0x0004) with the
        // right equivalents 0x0008/0x0010 — cross-checked against
        // InputInjector's RightDown/RightUp constants (same values).
        var downFlag = rightButton ? 0x0008u : 0x0002u;
        var upFlag = rightButton ? 0x0010u : 0x0004u;
        if (SendInput(1, [Mouse(0, 0, downFlag)], Marshal.SizeOf<INPUT>()) != 1) return "input_unconfirmed";
        var complete = true;
        try
        {
            // Let the destination process consume button-down and each motion
            // while checking STOP and foreground ownership between segments.
            foreach (var p in positions.Skip(1))
            {
                Thread.Sleep(12);
                if (!authorized() || GetForegroundWindow() != window.Handle || !GetWindowRect(window.Handle, out current) || !current.Equals(rect) ||
                    SendInput(1, [Mouse(p.X, p.Y, 0x0001 | 0x2000 | 0x8000 | 0x4000)], Marshal.SizeOf<INPUT>()) != 1)
                { complete = false; break; }
            }
        }
        finally
        {
            if (SendInput(1, [Mouse(0, 0, upFlag)], Marshal.SizeOf<INPUT>()) != 1) complete = false;
        }
        return complete ? null : "input_unconfirmed";
    }
    public string? Click(TextWindowActivator.Window window, double x, double y, Func<bool> authorized)
        => Dispatch(window, x, y, 0, false, false, authorized);

    // G1 (100.030): right down/up via SendInput. MOUSEEVENTF_RIGHTDOWN =
    // 0x0008, MOUSEEVENTF_RIGHTUP = 0x0010 — the mirror of the left flags
    // 0x0002/0x0004; cross-checked against InputInjector's RightDown/RightUp
    // constants (same values). No middle button in v1 (D2).
    public string? RightClick(TextWindowActivator.Window window, double x, double y, Func<bool> authorized)
        => Dispatch(window, x, y, 0, false, true, authorized);

    public string? DoubleClick(TextWindowActivator.Window window, double x, double y, Func<bool> authorized)
        => Dispatch(window, x, y, 0, true, false, authorized);

    public string? Scroll(TextWindowActivator.Window window, double x, double y, int steps, Func<bool> authorized)
        => steps is < -3 or > 3 or 0 ? "invalid_scroll" : Dispatch(window, x, y, steps, false, false, authorized);

    private static string? Dispatch(TextWindowActivator.Window window, double x, double y, int steps, bool doubleClick, bool rightClick, Func<bool> authorized)
    {
        if (!authorized() || GetForegroundWindow() != window.Handle || !GetWindowRect(window.Handle, out var rect)) return "window_changed_or_not_foreground";
        var width = rect.Right - rect.Left; var height = rect.Bottom - rect.Top;
        if (width <= 1 || height <= 1) return "window_changed";
        var point = new POINT { X = rect.Left + (int)Math.Round(x * (width - 1)), Y = rect.Top + (int)Math.Round(y * (height - 1)) };
        var target = WindowFromPoint(point);
        GetWindowThreadProcessId(target, out var processId);
        if (target == IntPtr.Zero || GetAncestor(target, 2) != window.Handle || processId != window.ProcessId) return "target_obscured";
        var left = GetSystemMetrics(76); var top = GetSystemMetrics(77); var virtualWidth = GetSystemMetrics(78); var virtualHeight = GetSystemMetrics(79);
        if (virtualWidth <= 1 || virtualHeight <= 1 || point.X < left || point.Y < top || point.X >= left + virtualWidth || point.Y >= top + virtualHeight) return "position_offscreen";
        var nx = (int)Math.Round((point.X - left) * 65535.0 / (virtualWidth - 1));
        var ny = (int)Math.Round((point.Y - top) * 65535.0 / (virtualHeight - 1));
        if (!authorized() || GetForegroundWindow() != window.Handle || WindowFromPoint(point) != target) return "window_changed_or_not_foreground";
        var inputs = steps == 0
            ? doubleClick
                ? new[] { Mouse(nx, ny, 0x0001 | 0x8000 | 0x4000), Mouse(0, 0, 0x0002), Mouse(0, 0, 0x0004), Mouse(0, 0, 0x0002), Mouse(0, 0, 0x0004) }
                : rightClick
                    ? new[] { Mouse(nx, ny, 0x0001 | 0x8000 | 0x4000), Mouse(0, 0, 0x0008), Mouse(0, 0, 0x0010) }
                    : new[] { Mouse(nx, ny, 0x0001 | 0x8000 | 0x4000), Mouse(0, 0, 0x0002), Mouse(0, 0, 0x0004) }
            : new[] { Mouse(nx, ny, 0x0001 | 0x8000 | 0x4000), Mouse(0, 0, 0x0800, unchecked((uint)(steps * 120))) };
        var sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<INPUT>());
        if (sent == inputs.Length) return null;
        // A partial send can include button-down without button-up. Release it
        // even when the requested click outcome cannot be established — with
        // the matching button's up flag (right-up for a right-click).
        if (steps == 0 && sent >= 2) SendInput(1, [Mouse(0, 0, rightClick ? 0x0010u : 0x0004u)], Marshal.SizeOf<INPUT>());
        return "input_unconfirmed";
    }

    private static INPUT Mouse(int x, int y, uint flags, uint data = 0) => new() { type = 0, u = new InputUnion { mi = new MOUSEINPUT { dx = x, dy = y, dwFlags = flags, mouseData = data } } };
    [StructLayout(LayoutKind.Sequential)] private struct POINT { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] private struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] private struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr extra; }
    // Batch 2 (100.030): the keyboard/hardware input structs are removed —
    // only the mouse arm of the union was ever constructed here (keyboard
    // input lives in InputInjector, which has its own copies).
    [StructLayout(LayoutKind.Explicit)] private struct InputUnion { [FieldOffset(0)] public MOUSEINPUT mi; }
    [StructLayout(LayoutKind.Sequential)] private struct INPUT { public uint type; public InputUnion u; }
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr handle, out RECT rect);
    [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(POINT point);
    [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr handle, uint flags);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);
    [DllImport("user32.dll")] private static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll", SetLastError = true)] private static extern uint SendInput(uint count, INPUT[] inputs, int size);
    // G5 (100.030): new P/Invoke — GetCursorPos exists nowhere in app/*.cs
    // (verified 2026-11-09). Signature matches the one already used by the
    // specs project (AssistantAccessibilitySpecs.cs).
    [DllImport("user32.dll")] private static extern bool GetCursorPos(out POINT point);
}
