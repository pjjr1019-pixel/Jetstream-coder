using System.Drawing.Imaging;
using System.Runtime.InteropServices;

namespace Deskhand.Windows.App;

internal sealed record AssistantScreenshotResult(string? JpegBase64, int Width, int Height, string? Error, string? ObservationToken = null);
internal sealed record AssistantScreenObservation(TextWindowActivator.Window Window, byte[] ImageHash, int Width, int Height, DateTimeOffset CapturedAt, byte[]? ImageBytes = null);

internal sealed class AssistantScreenSession
{
    private readonly Dictionary<string, (AssistantScreenObservation Value, DateTimeOffset Expiry)> _observations = [];
    internal string Remember(TextWindowActivator.Window window, AssistantScreenshotResult screenshot)
    {
        _observations.Clear();
        var token = Guid.NewGuid().ToString("N");
        // Batch 2 (100.030): keep the JPEG bytes as well as the hash so the
        // pointer path can tolerate small pixel change (blinking caret,
        // clock) instead of requiring byte-identical frames. One live
        // observation per task, at most 1 MiB, so the retained copy is bounded.
        var bytes = Convert.FromBase64String(screenshot.JpegBase64!);
        var hash = System.Security.Cryptography.SHA256.HashData(bytes);
        _observations.Add(token, (new(window, hash, screenshot.Width, screenshot.Height, DateTimeOffset.UtcNow, bytes), DateTimeOffset.UtcNow.AddMinutes(2)));
        return token;
    }
    internal AssistantScreenObservation? Take(string token) =>
        _observations.Remove(token, out var item) && item.Expiry > DateTimeOffset.UtcNow ? item.Value : null;
    internal void Clear() => _observations.Clear();
}

/// <summary>Capture an individually verified window, never the whole desktop.</summary>
internal static class AssistantScreenshot
{
    private static readonly SemaphoreSlim CaptureGate = new(1, 1);

    // Batch 2: decision frames are downscaled to 640px wide (JPEG) by
    // default; pass a larger maxWidth only when the model must read small
    // text. Every call captures a fresh frame (CaptureGate + PrintWindow
    // per call); no frame is ever reused across verify steps.
    internal static async Task<AssistantScreenshotResult> CaptureAsync(TextWindowActivator.Window window, Func<bool>? authorized = null, int maxWidth = 640)
    {
        authorized ??= () => true;
        var before = await AssistantAccessibility.InspectAsync(window, authorized);
        if (before.Error is not null) return new(null, 0, 0, before.Error);
        if (!authorized()) return new(null, 0, 0, "authorization_revoked");
        if (!await CaptureGate.WaitAsync(0)) return new(null, 0, 0, "capture_busy");
        var worker = Task.Run(() =>
        {
            try { return authorized() ? Capture(window, authorized, maxWidth) : new AssistantScreenshotResult(null, 0, 0, "authorization_revoked"); }
            finally { CaptureGate.Release(); }
        });
        AssistantScreenshotResult captured;
        try { captured = await worker.WaitAsync(TimeSpan.FromSeconds(5)); }
        catch (TimeoutException) { return new(null, 0, 0, "capture_timeout"); }
        if (captured.Error is not null) return captured;
        var after = await AssistantAccessibility.InspectAsync(window, authorized);
        return after.Error is null ? captured : new(null, 0, 0, after.Error);
    }

    private static AssistantScreenshotResult Capture(TextWindowActivator.Window window, Func<bool> authorized, int maxWidth)
    {
        try
        {
            if (!TextWindowActivator.Snapshot().Any(w => w.Handle == window.Handle && w.ProcessId == window.ProcessId && w.ClassName == window.ClassName && w.Title == window.Title && w.ProcessStartTicks == window.ProcessStartTicks))
                return new(null, 0, 0, "stale_or_denied");
            if (!GetWindowRect(window.Handle, out var rect)) return new(null, 0, 0, "capture_unavailable");
            var width = rect.Right - rect.Left; var height = rect.Bottom - rect.Top;
            if (width <= 0 || height <= 0 || width > 3840 || height > 2160) return new(null, 0, 0, "unsupported_window_size");
            using var bitmap = new Bitmap(width, height);
            if (!authorized()) return new(null, 0, 0, "authorization_revoked");
            using (var graphics = Graphics.FromImage(bitmap))
            {
                var dc = graphics.GetHdc();
                try { if (!PrintWindow(window.Handle, dc, 2)) return new(null, 0, 0, "capture_unavailable"); }
                finally { graphics.ReleaseHdc(dc); }
            }
            if (!TextWindowActivator.Snapshot().Any(w => w.Handle == window.Handle && w.ProcessId == window.ProcessId && w.ClassName == window.ClassName && w.Title == window.Title && w.ProcessStartTicks == window.ProcessStartTicks))
                return new(null, 0, 0, "stale_or_denied");
            // Batch 2: downscale the decision frame to maxWidth (640 default),
            // preserving aspect; the result reports the downscaled size so
            // the pointer's fresh-capture comparison stays like-for-like.
            var targetWidth = Math.Max(1, Math.Min(bitmap.Width, maxWidth));
            Bitmap downscaled = bitmap;
            try
            {
                if (targetWidth != bitmap.Width)
                {
                    var scaledHeight = Math.Max(1, (int)((long)bitmap.Height * targetWidth / bitmap.Width));
                    var scaled = new Bitmap(targetWidth, scaledHeight);
                    using (var g = Graphics.FromImage(scaled))
                    {
                        g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                        g.DrawImage(bitmap, 0, 0, targetWidth, scaledHeight);
                    }
                    downscaled = scaled;
                }
                using var stream = new MemoryStream();
                downscaled.Save(stream, ImageFormat.Jpeg);
                if (stream.Length > 1024 * 1024) return new(null, 0, 0, "capture_too_large");
                return new(Convert.ToBase64String(stream.ToArray()), downscaled.Width, downscaled.Height, null);
            }
            finally { if (!ReferenceEquals(downscaled, bitmap)) downscaled.Dispose(); }
        }
        catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or ExternalException or OutOfMemoryException)
        { return new(null, 0, 0, "capture_unavailable"); }
    }

    [StructLayout(LayoutKind.Sequential)] private struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr handle, out RECT rect);
    [DllImport("user32.dll")] private static extern bool PrintWindow(IntPtr handle, IntPtr dc, uint flags);
}
