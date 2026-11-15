using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

namespace Deskhand.Windows.App;

/// <summary>Captures the primary monitor (including the mouse cursor) as scaled JPEG frames.
/// Batch 2 (100.030): the v100.008 multi-monitor selection API was never wired
/// up — zero callers anywhere in the snapshot — and is removed. Capture is
/// primary-only; Bounds stays primary-only for existing callers
/// (GpuVideoEncoder).</summary>
public interface IDesktopCapture : IDisposable
{
    int Width { get; }
    DesktopChange Capture();
    EncodedDesktop Encode(int maxWidth, long quality, int byteBudget);
}

public sealed class ScreenCapturer : IDesktopCapture
{
    private static readonly ImageCodecInfo Jpeg = ImageCodecInfo.GetImageEncoders().First(e => e.MimeType == "image/jpeg");
    private Bitmap? _full;


    public static Rectangle Bounds => System.Windows.Forms.Screen.PrimaryScreen!.Bounds;

    public Rectangle SelectedBounds => Bounds;

    public void Dispose()
    {
        _full?.Dispose();
        _composite?.Dispose();
    }

    private Bitmap? _composite;
    private readonly PixelChangeDetector _detector = new();
    private (int X, int Y, int Flags, IntPtr Handle) _cursor;
    public int Width => _full?.Width ?? 0;

    // Read every desktop pixel before cursor composition, so tiny text/caret changes are retained.
    public DesktopChange Capture()
    {
        var b = SelectedBounds;
        var resized = _full is null || _full.Width != b.Width || _full.Height != b.Height;
        if (resized)
        {
            _full?.Dispose(); _composite?.Dispose();
            _full = new Bitmap(b.Width, b.Height, PixelFormat.Format32bppRgb);
            _composite = new Bitmap(b.Width, b.Height, PixelFormat.Format32bppRgb);
        }
        using (var g = Graphics.FromImage(_full!)) g.CopyFromScreen(b.Location, Point.Empty, b.Size);
        var change = _detector.Compare(_full!);
        var info = new CURSORINFO { cbSize = Marshal.SizeOf<CURSORINFO>() };
        GetCursorInfo(ref info);
        var cursor = (info.pt.X, info.pt.Y, info.flags, info.hCursor);
        var cursorChanged = cursor != _cursor;
        _cursor = cursor;
        return new(change.Changed || cursorChanged, change.Substantial, resized);
    }

    // Encode only the newest captured screen, and only when a transport slot is available.
    public EncodedDesktop Encode(int maxWidth, long quality, int byteBudget)
    {
        if (_full is null) throw new InvalidOperationException("Capture first");
        using (var g = Graphics.FromImage(_composite!))
        {
            g.DrawImageUnscaled(_full, 0, 0);
            DrawCursor(g, SelectedBounds);
        }
        return EncodeBounded(_composite!, maxWidth, quality, byteBudget);
    }

    public static EncodedDesktop EncodeBounded(Bitmap source, int maxWidth, long quality, int byteBudget)
    {
        var width = Math.Min(source.Width, Math.Max(1, maxWidth));
        var budget = Math.Min(byteBudget, AdaptiveStream.MaxFrameBytes);
        Bitmap? scaled = null;
        try
        {
            while (true)
            {
                var height = Math.Max(1, (int)((long)source.Height * width / source.Width));
                if (width != source.Width && (scaled is null || scaled.Width != width))
                {
                    scaled?.Dispose();
                    scaled = new Bitmap(width, height, PixelFormat.Format32bppRgb);
                    using var g = Graphics.FromImage(scaled);
                    g.InterpolationMode = quality >= 90 ? InterpolationMode.HighQualityBicubic : InterpolationMode.Bilinear;
                    g.DrawImage(source, 0, 0, width, height);
                }
                using var ms = new MemoryStream(128 * 1024);
                using var parameters = new EncoderParameters(1);
                parameters.Param[0] = new EncoderParameter(Encoder.Quality, quality);
                (width == source.Width ? source : scaled!).Save(ms, Jpeg, parameters);
                if (ms.Length <= budget) return new(ms.ToArray(), width, height, quality);
                if (quality > 45) { quality = Math.Max(45, quality - 10); continue; }
                if (width == 1) throw new InvalidOperationException("Connection frame budget is smaller than a JPEG header");
                width = Math.Max(1, (int)(width * .75));
            }
        }
        finally { scaled?.Dispose(); }
    }

    private static void DrawCursor(Graphics g, Rectangle bounds)
    {
        var info = new CURSORINFO { cbSize = Marshal.SizeOf<CURSORINFO>() };
        if (!GetCursorInfo(ref info) || (info.flags & 1) == 0) return;
        if (!GetIconInfo(info.hCursor, out var icon)) return;
        try
        {
            var hdc = g.GetHdc();
            try
            {
                DrawIconEx(hdc, info.pt.X - bounds.X - icon.xHotspot, info.pt.Y - bounds.Y - icon.yHotspot, info.hCursor, 0, 0, 0, IntPtr.Zero, 3);
            }
            finally
            {
                g.ReleaseHdc(hdc);
            }
        }
        finally
        {
            if (icon.hbmMask != IntPtr.Zero) DeleteObject(icon.hbmMask);
            if (icon.hbmColor != IntPtr.Zero) DeleteObject(icon.hbmColor);
        }
    }

    [StructLayout(LayoutKind.Sequential)] private struct POINT { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] private struct CURSORINFO { public int cbSize, flags; public IntPtr hCursor; public POINT pt; }
    [StructLayout(LayoutKind.Sequential)] private struct ICONINFO { public bool fIcon; public int xHotspot, yHotspot; public IntPtr hbmMask, hbmColor; }

    [DllImport("user32.dll")] private static extern bool GetCursorInfo(ref CURSORINFO info);
    [DllImport("user32.dll")] private static extern bool GetIconInfo(IntPtr hIcon, out ICONINFO info);
    [DllImport("user32.dll")] private static extern bool DrawIconEx(IntPtr hdc, int x, int y, IntPtr hIcon, int cx, int cy, int step, IntPtr brush, int flags);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr obj);
}
