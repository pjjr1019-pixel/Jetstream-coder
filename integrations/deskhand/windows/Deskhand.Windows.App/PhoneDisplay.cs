using System.Runtime.InteropServices;
using System.Text.Json;

namespace Deskhand.Windows.App;

// A single host owns the temporary mode for all its viewers.
internal sealed class PhoneDisplay
{
    private readonly object _sync = new();
    private readonly string _journal;
    private readonly Action<string> _notice;
    private readonly IDisplayApi _display;
    private static readonly JsonSerializerOptions JsonOptions = new() { IncludeFields = true };
    private readonly Dictionary<object, (bool Landscape, long Sequence)> _viewers = new();
    private long _sequence;
    private bool _enabled;
    private Recovery? _active;

    internal PhoneDisplay(bool enabled, Action<string> notice, IDisplayApi? display = null, string? journal = null)
    {
        _enabled = enabled;
        _notice = notice;
        _display = display ?? new WindowsDisplayApi();
        var dir = Environment.GetEnvironmentVariable("DESKHAND_CONFIG_DIR") ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Deskhand");
        _journal = journal ?? Path.Combine(dir, "display-recovery.json");
        Recover();
    }

    internal void Enter(object viewer, bool landscape) { lock (_sync) { _viewers[viewer] = (landscape, ++_sequence); if (_enabled) Apply(landscape); } }
    internal void Update(object viewer, bool landscape)
    {
        lock (_sync)
        {
            if (!_viewers.TryGetValue(viewer, out var previous) || previous.Landscape == landscape) return;
            _viewers[viewer] = (landscape, ++_sequence);
            if (_enabled) Apply(landscape);
        }
    }
    internal void Leave(object viewer)
    {
        lock (_sync)
        {
            if (!_viewers.Remove(viewer)) return;
            if (_viewers.Count == 0) Restore();
            else if (_enabled) Apply(_viewers.Values.MaxBy(v => v.Sequence).Landscape);
        }
    }
    internal void SetEnabled(bool enabled)
    {
        lock (_sync)
        {
            if (_enabled == enabled) return;
            _enabled = enabled;
            if (_viewers.Count > 0) { if (enabled) Apply(_viewers.Values.MaxBy(v => v.Sequence).Landscape); else Restore(); }
        }
    }

    private void Apply(bool landscape)
    {
        try
        {
            if (_active is null && File.Exists(_journal)) { _notice("Phone-friendly display is waiting for recovery."); return; }
            var device = _active?.Device ?? _display.PrimaryDevice;
            if (device is null || !_display.Read(device, -1, 0, out var current)) { _notice("Phone-friendly display is unavailable."); return; }
            if (_active is { } active && !SameMode(current, active.Applied)) { _notice("Display was changed manually; phone-friendly display is paused."); return; }
            var original = _active?.Original ?? current;
            // Batch 2 (100.030, B5): only skip the switch when the current
            // mode is already the phone-friendly one. (The pause check above
            // already returns when the current mode differs from Applied, so
            // this is defence in depth, not a behaviour change on its own.)
            if (_active is { } currentActive && SameMode(current, currentActive.Applied) && (current.Width > current.Height) == landscape) return;
            var candidates = new List<DisplayMode>();
            for (var i = 0; _display.Read(device, i, 4, out var mode); i++)
                if ((landscape ? mode.Width > mode.Height : mode.Width < mode.Height) && mode.Bits == original.Bits && Math.Min(mode.Width, mode.Height) >= 600) candidates.Add(mode);
            var targetAspect = landscape ? 16.0 / 9.0 : 9.0 / 16.0;
            // Treat near-matching aspect ratios alike so a huge exact 16:9 mode
            // does not win over a much more readable phone-sized mode.
            var selected = candidates.OrderBy(m => Math.Abs((double)m.Width / m.Height / targetAspect - 1) <= 0.05 ? 0 : 1)
                .ThenBy(m => Math.Abs((long)m.Width - (landscape ? 1600 : 900)) + Math.Abs((long)m.Height - (landscape ? 900 : 1600)))
                .ThenBy(m => Math.Abs((double)m.Width / m.Height - targetAspect)).ThenBy(m => Math.Abs((long)m.Frequency - original.Frequency))
                .Select(m => { m.X = original.X; m.Y = original.Y; return m; })
                .FirstOrDefault(m => _display.Change(device, m, 2) == 0);
            if (selected.Width == 0) { _notice("The requested phone-friendly display mode is not supported by this display."); return; }
            var recovery = new Recovery(device, original, selected, _active?.Applied);
            Directory.CreateDirectory(Path.GetDirectoryName(_journal)!);
            var temp = _journal + ".tmp";
            File.WriteAllText(temp, JsonSerializer.Serialize(recovery, JsonOptions));
            File.Move(temp, _journal, true);
            if (_display.Change(device, selected, 0) != 0)
            {
                if (_active is null) File.Delete(_journal);
                else
                {
                    File.WriteAllText(temp, JsonSerializer.Serialize(_active, JsonOptions));
                    File.Move(temp, _journal, true);
                }
                _notice("Could not switch to phone-friendly display mode.");
                return;
            }
            _active = recovery;
            if (recovery.PreviousApplied is not null)
            {
                var settled = recovery with { PreviousApplied = null };
                File.WriteAllText(temp, JsonSerializer.Serialize(settled, JsonOptions));
                File.Move(temp, _journal, true);
                _active = settled;
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.ComponentModel.Win32Exception)
        { _notice("Phone-friendly display could not be applied: " + ex.Message); }
    }

    private void Recover()
    {
        try
        {
            if (!File.Exists(_journal)) return;
            _active = JsonSerializer.Deserialize<Recovery>(File.ReadAllText(_journal), JsonOptions);
            if (_active is null) { _notice("Display recovery information could not be read."); return; }
            Restore();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        { _notice("Display recovery failed: " + ex.Message); }
    }

    private void Restore()
    {
        if (_active is not { } saved) return;
        try
        {
            if (!_display.Read(saved.Device, -1, 0, out var current)) { _notice("Display unavailable. Deskhand will retry display recovery on startup."); return; }
            // Batch 2 (100.030, B5): the old code deleted the recovery journal
            // without restoring whenever the current mode matched neither the
            // applied nor the previous mode (i.e. the user changed the display
            // manually while phone-friendly mode was active) — the original
            // mode was then unrecoverable. Restore first, and only delete the
            // journal (and clear _active) once the display is back on the
            // original mode, or is already there. A failed restore keeps the
            // journal so startup recovery can retry.
            if (SameMode(current, saved.Original)) { File.Delete(_journal); _active = null; return; }
            if (_display.Change(saved.Device, saved.Original, 0) != 0)
            { _notice("Could not restore the display. Deskhand will retry on startup."); return; }
            File.Delete(_journal);
            _active = null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        { _notice("Display recovery failed: " + ex.Message); }
    }

    private static bool SameMode(DisplayMode a, DisplayMode b) => a.Width == b.Width && a.Height == b.Height && a.Orientation == b.Orientation && a.Frequency == b.Frequency && a.Bits == b.Bits && a.X == b.X && a.Y == b.Y;
    private sealed record Recovery(string Device, DisplayMode Original, DisplayMode Applied, DisplayMode? PreviousApplied = null);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    internal struct DisplayMode
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        public ushort SpecVersion, DriverVersion, Size, DriverExtra;
        public uint Fields;
        public int X, Y;
        public uint Orientation, FixedOutput;
        public short Color, Duplex, YResolution, TTOption, Collate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string FormName;
        public ushort LogPixels;
        public uint Bits, Width, Height, DisplayFlags, Frequency, ICMMethod, ICMIntent, MediaType, DitherType, Reserved1, Reserved2, PanningWidth, PanningHeight;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool EnumDisplaySettingsEx(string device, int mode, ref DisplayMode settings, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int ChangeDisplaySettingsEx(string device, ref DisplayMode settings, IntPtr hwnd, uint flags, IntPtr param);
    internal interface IDisplayApi
    {
        string? PrimaryDevice { get; }
        bool Read(string device, int index, uint flags, out DisplayMode mode);
        int Change(string device, DisplayMode mode, uint flags);
    }

    internal sealed class WindowsDisplayApi : IDisplayApi
    {
        public string? PrimaryDevice => Screen.PrimaryScreen?.DeviceName;
        public bool Read(string device, int index, uint flags, out DisplayMode mode)
        {
            mode = new DisplayMode { Size = (ushort)Marshal.SizeOf<DisplayMode>() };
            return EnumDisplaySettingsEx(device, index, ref mode, flags);
        }
        public int Change(string device, DisplayMode mode, uint flags)
        {
            mode.Fields = 0x20 | 0x80 | 0x40000 | 0x80000 | 0x100000 | 0x400000;
            return ChangeDisplaySettingsEx(device, ref mode, IntPtr.Zero, flags, IntPtr.Zero);
        }
    }
}
