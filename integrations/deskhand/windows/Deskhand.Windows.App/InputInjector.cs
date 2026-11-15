using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace Deskhand.Windows.App;

/// <summary>
/// Injects mouse and keyboard input from validated remote events.
/// Windows blocks injection into elevated windows (UIPI) and the secure desktop; those are not controllable.
/// </summary>
public static class InputInjector
{
    private const uint Mouse = 0, Keyboard = 1;
    private const uint MoveF = 0x0001, LeftDown = 0x0002, LeftUp = 0x0004, RightDown = 0x0008, RightUp = 0x0010, WheelF = 0x0800, AbsoluteF = 0x8000;
    private const uint KeyUpF = 0x0002, UnicodeF = 0x0004;

    private static readonly Dictionary<string, ushort> Keys = new()
    {
        ["enter"] = 0x0D, ["backspace"] = 0x08, ["tab"] = 0x09, ["esc"] = 0x1B, ["delete"] = 0x2E,
        ["left"] = 0x25, ["up"] = 0x26, ["right"] = 0x27, ["down"] = 0x28,
        ["home"] = 0x24, ["end"] = 0x23, ["pageup"] = 0x21, ["pagedown"] = 0x22,
        // D3 (100.030): F1–F12 are single keys, so they live in Keys.
        ["F1"] = 0x70, ["F2"] = 0x71, ["F3"] = 0x72, ["F4"] = 0x73,
        ["F5"] = 0x74, ["F6"] = 0x75, ["F7"] = 0x76, ["F8"] = 0x77,
        ["F9"] = 0x78, ["F10"] = 0x79, ["F11"] = 0x7A, ["F12"] = 0x7B,
    };
    // AI shortcuts are separately approved and never change the phone's key protocol.
    // D3 (100.030): each chord names its modifier virtual keys (press order)
    // and its main key explicitly — the same named-allowlist pattern as
    // before, now uniform across ctrl+letter and multi-modifier chords.
    // Lockstep: Keys + AssistantShortcuts here, the protocol parser's
    // control.key set, and AssistantAccessibility's supported-key check
    // (InputInjector.IsSupportedKey) must contain exactly the same names.
    private const ushort Ctrl = 0x11, Shift = 0x10, Alt = 0x12;
    private static readonly Dictionary<string, (ushort[] Modifiers, ushort Key)> AssistantShortcuts = new()
    {
        ["ctrl+a"] = ([Ctrl], 0x41), ["ctrl+c"] = ([Ctrl], 0x43), ["ctrl+v"] = ([Ctrl], 0x56),
        ["ctrl+x"] = ([Ctrl], 0x58), ["ctrl+z"] = ([Ctrl], 0x5A), ["ctrl+y"] = ([Ctrl], 0x59),
        ["ctrl+s"] = ([Ctrl], 0x53), ["ctrl+f"] = ([Ctrl], 0x46),
        ["ctrl+shift+o"] = ([Ctrl, Shift], 0x4F),
        ["alt+tab"] = ([Alt], 0x09),
        ["shift+tab"] = ([Shift], 0x09),
        ["ctrl+shift+left"] = ([Ctrl, Shift], 0x25),
        ["ctrl+shift+right"] = ([Ctrl, Shift], 0x27),
        ["ctrl+home"] = ([Ctrl], 0x24),
        ["ctrl+end"] = ([Ctrl], 0x23),
    };

    // Batch 2 (100.030, B4): the foreground window the last injected input
    // actually landed in (handle, process id, title, time). The phone input
    // protocol (Msg.Input) carries no window target, so this is a record for
    // diagnosis, not a per-window gate — see RequireForegroundWindow below.
    internal static (IntPtr Handle, uint ProcessId, string Title, DateTimeOffset At)? LastInjectedForeground { get; private set; }

    public static void Handle(ReadOnlySpan<byte> json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json.ToArray());
            var root = doc.RootElement;
            RecordForegroundWindow();
            RequireForegroundWindow();
            switch (root.GetProperty("t").GetString())
            {
                case "move": MoveTo(Num(root, "x"), Num(root, "y")); break;
                case "down": Button(root, true); break;
                case "up": Button(root, false); break;
                case "scroll": Wheel((int)Math.Clamp(Num(root, "dy"), -1200, 1200)); break;
                case "text": Text(root.GetProperty("s").GetString() ?? ""); break;
                case "key":
                    if (Keys.TryGetValue(root.GetProperty("k").GetString() ?? "", out var vk)) Key(vk);
                    break;
            }
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException or FormatException)
        {
        }
    }

    private static void RecordForegroundWindow()
    {
        var handle = GetForegroundWindow();
        if (handle == IntPtr.Zero) { LastInjectedForeground = null; return; }
        GetWindowThreadProcessId(handle, out var processId);
        var title = new StringBuilder(512);
        GetWindowTextW(handle, title, title.Capacity);
        LastInjectedForeground = (handle, processId, title.ToString(), DateTimeOffset.UtcNow);
    }

    private static void RequireForegroundWindow()
    {
        // With no foreground window there is nothing to receive the input;
        // refusing beats injecting blind. This does not verify the input
        // lands in the window the phone displayed — Msg.Input carries no
        // window target, so if the foreground changed between the displayed
        // frame and the tap, input still lands in the wrong app. A real gate
        // needs a window token in the phone input protocol (out of scope).
        if (GetForegroundWindow() == IntPtr.Zero)
            throw new System.ComponentModel.Win32Exception(5, "Remote input refused: no foreground window to receive it.");
    }

    private static double Num(JsonElement e, string name) => e.GetProperty(name).GetDouble();

    private static void MoveTo(double x, double y)
    {
        var nx = (int)Math.Round(Math.Clamp(x, 0, 1) * 65535);
        var ny = (int)Math.Round(Math.Clamp(y, 0, 1) * 65535);
        try
        {
            // Batch 2 (100.030, B3): MOUSEEVENTF_ABSOLUTE coordinates are
            // fractions of the *virtual desktop* (all monitors, origin
            // possibly negative), but the phone sends fractions of the
            // captured primary-monitor frame. Anchor the fractions to the
            // primary monitor's bounds (the same conversion the assistant
            // pointer path already does) instead of mapping them onto the
            // whole virtual desktop. Single-monitor PCs are unaffected:
            // the primary bounds are the virtual desktop there. A missing
            // primary screen (headless session) falls back to the old mapping.
            var bounds = ScreenCapturer.Bounds;
            var left = GetSystemMetrics(76); var top = GetSystemMetrics(77);
            var virtualWidth = GetSystemMetrics(78); var virtualHeight = GetSystemMetrics(79);
            if (bounds.Width > 1 && bounds.Height > 1 && virtualWidth > 1 && virtualHeight > 1)
            {
                var px = bounds.X + Math.Clamp(x, 0, 1) * (bounds.Width - 1);
                var py = bounds.Y + Math.Clamp(y, 0, 1) * (bounds.Height - 1);
                nx = (int)Math.Clamp(Math.Round((px - left) * 65535.0 / (virtualWidth - 1)), 0, 65535);
                ny = (int)Math.Clamp(Math.Round((py - top) * 65535.0 / (virtualHeight - 1)), 0, 65535);
            }
        }
        catch (Exception ex) when (ex is NullReferenceException or InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            // Fall back to the whole-desktop mapping above.
        }
        Send(MouseInput(nx, ny, 0, MoveF | AbsoluteF));
    }

    private static void Button(JsonElement root, bool down)
    {
        var right = root.TryGetProperty("b", out var b) && b.GetString() == "r";
        Send(MouseInput(0, 0, 0, right ? (down ? RightDown : RightUp) : (down ? LeftDown : LeftUp)));
    }

    private static void Wheel(int delta) => Send(MouseInput(0, 0, unchecked((uint)delta), WheelF));

    private static void Text(string s)
    {
        // D8 (100.030): over-long text is REFUSED, never silently truncated —
        // the 256-char cut was a silent-drop safety bug. 256 chars accepted,
        // 257 refused; the caller chunks. Thrown as Win32Exception(87 /
        // ERROR_INVALID_PARAMETER) deliberately: HostEngine's Msg.Input
        // handler catches exactly Win32Exception and raises a visible
        // notice, so the refusal reaches the phone. ArgumentException would
        // escape that handler uncaught, and InvalidOperationException would
        // be swallowed silently by both catch filters (the bug, again).
        if (s.Length > 256)
            throw new System.ComponentModel.Win32Exception(87, $"Remote text refused: {s.Length} characters exceeds the 256-character limit; chunk it into smaller calls.");
        foreach (var c in s)
        {
            Send(KeyInput(0, c, UnicodeF));
            Send(KeyInput(0, c, UnicodeF | KeyUpF));
        }
    }

    private static void Key(ushort vk)
    {
        Send(KeyInput(vk, 0, 0));
        Send(KeyInput(vk, 0, KeyUpF));
    }

    internal static bool IsSupportedKey(string key) => Keys.ContainsKey(key) || AssistantShortcuts.ContainsKey(key);

    // G3/D4 (100.030): keys held down via KeyDown, released by KeyUp or by
    // ReleaseAllHeldKeys. A hold may not exceed 30s (D4): a timer sweeps
    // expired holds every 5s, and every key entry point sweeps lazily first.
    // The timer is created on the first KeyDown and lives for the process.
    private const int MaxHoldSeconds = 30;
    private static readonly object HeldGate = new();
    private static readonly List<(ushort Vk, DateTimeOffset Since)> HeldKeys = [];
    private static System.Threading.Timer? _holdTimer;

    private static bool TryResolveChord(string key, out ushort[] modifiers, out ushort vk)
    {
        if (AssistantShortcuts.TryGetValue(key, out var chord)) { modifiers = chord.Modifiers; vk = chord.Key; return true; }
        if (Keys.TryGetValue(key, out vk)) { modifiers = []; vk = 0; return true; }
        modifiers = []; vk = 0; return false;
    }

    internal static void PressSupportedKey(string key)
    {
        if (!TryResolveChord(key, out var modifiers, out var vk)) throw new ArgumentException("Unsupported key", nameof(key));
        ReleaseExpiredHolds();
        // A chord press must not release a modifier the caller is holding via
        // KeyDown: up-events for held keys are skipped, so the hold survives
        // the press (the redundant down for an already-down key is harmless).
        ushort[] heldNow;
        lock (HeldGate)
        {
            heldNow = new ushort[HeldKeys.Count];
            for (var i = 0; i < HeldKeys.Count; i++) heldNow[i] = HeldKeys[i].Vk;
        }
        var inputs = new List<INPUT>();
        foreach (var m in modifiers) inputs.Add(KeyInput(m, 0, 0));
        inputs.Add(KeyInput(vk, 0, 0));
        if (Array.IndexOf(heldNow, vk) < 0) inputs.Add(KeyInput(vk, 0, KeyUpF));
        for (var i = modifiers.Length - 1; i >= 0; i--)
            if (Array.IndexOf(heldNow, modifiers[i]) < 0) inputs.Add(KeyInput(modifiers[i], 0, KeyUpF));
        var array = inputs.ToArray();
        var sent = SendInput((uint)array.Length, array, Marshal.SizeOf<INPUT>());
        if (sent == array.Length) return;
        // Release any keys that may have been pressed in a partially submitted
        // batch. Never release a key the caller is holding via KeyDown.
        // The down events are the first (modifiers + main key) inputs.
        var downs = modifiers.Length + 1;
        for (var i = 0; i < sent && i < downs; i++)
        {
            var pressed = i < modifiers.Length ? modifiers[i] : vk;
            if (Array.IndexOf(heldNow, pressed) < 0) SendInput(1, [KeyInput(pressed, 0, KeyUpF)], Marshal.SizeOf<INPUT>());
        }
        throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "Windows did not confirm the complete key input.");
    }

    // G3 (100.030): press-and-hold start. The matching KeyUp (same key) must
    // arrive in the same task; the host releases all held keys on task end,
    // STOP latch, approval expiry, and any error (ReleaseAllHeldKeys, called
    // by the engine). A failed batch never leaves a key held: whatever went
    // down in a partial send is released before throwing.
    internal static void KeyDown(string key)
    {
        if (!TryResolveChord(key, out var modifiers, out var vk)) throw new ArgumentException("Unsupported key", nameof(key));
        ReleaseExpiredHolds();
        var inputs = new INPUT[modifiers.Length + 1];
        for (var i = 0; i < modifiers.Length; i++) inputs[i] = KeyInput(modifiers[i], 0, 0);
        inputs[modifiers.Length] = KeyInput(vk, 0, 0);
        var sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<INPUT>());
        if (sent != inputs.Length)
        {
            for (var i = 0; i < sent && i < inputs.Length; i++)
                SendInput(1, [KeyInput(i < modifiers.Length ? modifiers[i] : vk, 0, KeyUpF)], Marshal.SizeOf<INPUT>());
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "Windows did not confirm the complete key-down input.");
        }
        lock (HeldGate)
        {
            var now = DateTimeOffset.UtcNow;
            for (var i = 0; i < modifiers.Length; i++) TrackHeld(modifiers[i], now);
            TrackHeld(vk, now);
            _holdTimer ??= new System.Threading.Timer(_ => ReleaseExpiredHolds(), null, TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(5));
        }
    }

    // Call with HeldGate held.
    private static void TrackHeld(ushort vk, DateTimeOffset now)
    {
        HeldKeys.RemoveAll(h => h.Vk == vk);
        HeldKeys.Add((vk, now));
    }

    // G3 (100.030): press-and-hold end. Best effort and never throws: every
    // up event is sent individually and only a confirmed up clears that key
    // from the held set, so a failed release stays tracked and is retried by
    // the hold timer / ReleaseAllHeldKeys instead of silently leaking.
    internal static void KeyUp(string key)
    {
        if (!TryResolveChord(key, out var modifiers, out var vk)) throw new ArgumentException("Unsupported key", nameof(key));
        ReleaseExpiredHolds();
        var released = new List<ushort>();
        if (SendInput(1, [KeyInput(vk, 0, KeyUpF)], Marshal.SizeOf<INPUT>()) == 1) released.Add(vk);
        for (var i = modifiers.Length - 1; i >= 0; i--)
            if (SendInput(1, [KeyInput(modifiers[i], 0, KeyUpF)], Marshal.SizeOf<INPUT>()) == 1) released.Add(modifiers[i]);
        if (released.Count == 0) return;
        lock (HeldGate)
        {
            foreach (var up in released) HeldKeys.RemoveAll(h => h.Vk == up);
        }
    }

    // G3 mandatory release: releases every key held via KeyDown (reverse
    // press order). Best effort and never throws — the engine calls this on
    // task end, STOP latch, approval expiry, and any error. Only a confirmed
    // up clears a key from the held set, so a failed release stays tracked
    // for the next sweep instead of silently leaking.
    internal static void ReleaseAllHeldKeys()
    {
        ushort[] held;
        lock (HeldGate)
        {
            held = new ushort[HeldKeys.Count];
            for (var i = 0; i < HeldKeys.Count; i++) held[HeldKeys.Count - 1 - i] = HeldKeys[i].Vk;
        }
        var released = new List<ushort>();
        foreach (var vk in held)
            if (SendInput(1, [KeyInput(vk, 0, KeyUpF)], Marshal.SizeOf<INPUT>()) == 1) released.Add(vk);
        if (released.Count == 0) return;
        lock (HeldGate)
        {
            foreach (var up in released) HeldKeys.RemoveAll(h => h.Vk == up);
        }
    }

    // D4: releases any hold older than MaxHoldSeconds. Called by the hold
    // timer and lazily at every key entry point.
    private static void ReleaseExpiredHolds()
    {
        ushort[] expired;
        var cutoff = DateTimeOffset.UtcNow.AddSeconds(-MaxHoldSeconds);
        lock (HeldGate)
        {
            var count = 0;
            foreach (var h in HeldKeys) if (h.Since <= cutoff) count++;
            expired = new ushort[count];
            var j = 0;
            for (var i = HeldKeys.Count - 1; i >= 0; i--)
                if (HeldKeys[i].Since <= cutoff) expired[j++] = HeldKeys[i].Vk;
        }
        if (expired.Length == 0) return;
        var released = new List<ushort>();
        foreach (var vk in expired)
            if (SendInput(1, [KeyInput(vk, 0, KeyUpF)], Marshal.SizeOf<INPUT>()) == 1) released.Add(vk);
        if (released.Count == 0) return;
        lock (HeldGate)
        {
            foreach (var up in released) HeldKeys.RemoveAll(h => h.Vk == up);
        }
    }

    private static INPUT MouseInput(int dx, int dy, uint data, uint flags) =>
        new() { type = Mouse, u = new InputUnion { mi = new MOUSEINPUT { dx = dx, dy = dy, mouseData = data, dwFlags = flags } } };

    private static INPUT KeyInput(ushort vk, ushort scan, uint flags) =>
        new() { type = Keyboard, u = new InputUnion { ki = new KEYBDINPUT { wVk = vk, wScan = scan, dwFlags = flags } } };

    private static void Send(INPUT input)
    {
        if (SendInput(1, [input], Marshal.SizeOf<INPUT>()) != 1)
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(),
                "Windows rejected remote input; check the interactive desktop and privilege boundary.");
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }

    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT { public uint type; public InputUnion u; }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint n, INPUT[] inputs, int size);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowTextW(IntPtr handle, StringBuilder text, int max);
    [DllImport("user32.dll")] private static extern int GetSystemMetrics(int index);
}
