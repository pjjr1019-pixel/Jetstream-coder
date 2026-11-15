using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

namespace Deskhand.Windows.App;

public sealed class PixelChangeDetector
{
    private int[] _previous = [], _current = [];
    private Size _size;
    public DesktopChange Compare(Bitmap bitmap)
    {
        var resized = bitmap.Size != _size;
        if (resized)
        { _size = bitmap.Size; _previous = new int[bitmap.Width * bitmap.Height]; _current = new int[_previous.Length]; }
        var data = bitmap.LockBits(new Rectangle(Point.Empty, bitmap.Size), ImageLockMode.ReadOnly, PixelFormat.Format32bppRgb);
        try
        {
            for (var y = 0; y < bitmap.Height; y++)
                Marshal.Copy(data.Scan0 + y * data.Stride, _current, y * bitmap.Width, bitmap.Width);
        }
        finally { bitmap.UnlockBits(data); }
        var changes = 0;
        var threshold = Math.Max(256, _previous.Length / 100);
        if (!_current.AsSpan().SequenceEqual(_previous))
            for (var i = 0; i < _current.Length && changes < threshold; i++)
                if ((_current[i] & 0xffffff) != (_previous[i] & 0xffffff)) changes++;
        (_previous, _current) = (_current, _previous);
        return new(resized || changes > 0, resized || changes >= Math.Max(256, _previous.Length / 100), resized);
    }
}
