using System.Diagnostics;

namespace Deskhand.Windows.App;

public sealed class GpuVideoEncoder : IDisposable
{
    public const int FramesPerSecond = 45;
    public sealed record Frame(byte[] Bytes, bool Key, bool Restart, double At);
    public static string Executable => Environment.GetEnvironmentVariable("DESKHAND_FFMPEG") ??
        Path.Combine(AppContext.BaseDirectory, "ffmpeg", "win-x64", "ffmpeg.exe");
    public static bool Available => File.Exists(Executable);
    private readonly Process _process;
    private readonly object _gate = new();
    private readonly Queue<Frame> _frames = new();
    private bool _needKey = true, _disposed;
    public int Width { get; }
    public int Height { get; }
    public bool Failed { get; private set; }
    public GpuVideoEncoder(int width, int height, bool syntheticFixture = false)
    {
        var scale = Math.Min(1.0, 1280.0 / Math.Max(width, height));
        Width = Math.Max(2, (int)(width * scale) / 2 * 2);
        Height = Math.Max(2, (int)(height * scale) / 2 * 2);
        _process = new Process { StartInfo = new ProcessStartInfo(Executable)
        { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true } };
        // Match ScreenCapturer's already-rotated Windows desktop coordinates. Desktop
        // duplication returns the physical scanout orientation on rotated displays.
        var bounds = ScreenCapturer.Bounds;
        string[] input = syntheticFixture
            ? ["-re", "-f", "lavfi", "-i", $"testsrc2=size={width}x{height}:rate={FramesPerSecond}"]
            : System.Windows.Forms.Screen.AllScreens.Length == 1
            // Let gdigrab use its own DPI-aware desktop bounds on a single
            // display. Explicit physical pixel bounds can exceed the logical
            // desktop reported to the ffmpeg process after a rotation.
            ? ["-f", "gdigrab", "-framerate", FramesPerSecond.ToString(), "-i", "desktop"]
            : ["-f", "gdigrab", "-framerate", FramesPerSecond.ToString(), "-offset_x", bounds.X.ToString(), "-offset_y", bounds.Y.ToString(),
               "-video_size", $"{width}x{height}", "-i", "desktop"];
        string[] args = ["-hide_banner", "-loglevel", "error", ..input,
            "-vf", $"scale={Width}:{Height},setsar=1,format=nv12", "-c:v", "h264_nvenc",
            "-tune", "ull", "-preset", "p4", "-spatial-aq", "1", "-aq-strength", "6",
            "-rc-lookahead", "0", "-zerolatency", "1", "-bf", "0", "-g", "22", "-b:v", "900k", "-maxrate", "900k",
            "-bufsize", "150k", "-bsf:v", "h264_metadata=aud=insert", "-flush_packets", "1", "-f", "h264", "pipe:1"];
        foreach (var arg in args) _process.StartInfo.ArgumentList.Add(arg);
        _process.Start();
        // Drain stderr without retaining screen data or unbounded diagnostic output.
        _ = Task.Run(async () => { try { while (await _process.StandardError.ReadLineAsync() is { } line) { var log = Environment.GetEnvironmentVariable("DESKHAND_FPS_LOG"); if (log is not null) { try { File.AppendAllText(log, $"{DateTime.UtcNow:O} encoder-error {line[..Math.Min(line.Length, 500)]}\n"); } catch (IOException) { } } } } catch (ObjectDisposedException) { } });
        _ = Task.Run(() =>
        {
            try
            {
                H264AccessUnits.Read(_process.StandardOutput.BaseStream, (bytes, key) =>
                {
                    lock (_gate)
                    {
                        if (_disposed) return;
                        if (_frames.Count >= 4) { _frames.Clear(); _needKey = true; }
                        if (_needKey && !key) return;
                        _frames.Enqueue(new(bytes, key, _needKey, StreamFlight.Now));
                        _needKey = false;
                    }
                });
            }
            catch (Exception ex) when (ex is IOException or ObjectDisposedException or InvalidDataException) { }
            finally { Failed = true; }
        });
    }
    public Frame? Take()
    {
        lock (_gate) return _frames.TryDequeue(out var frame) ? frame : null;
    }
    public void Dispose()
    {
        lock (_gate) { if (_disposed) return; _disposed = true; _frames.Clear(); }
        try { if (!_process.HasExited) _process.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
        _process.Dispose();
    }
}
