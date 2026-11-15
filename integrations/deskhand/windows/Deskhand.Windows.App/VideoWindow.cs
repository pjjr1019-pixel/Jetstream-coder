using System.Buffers.Binary;

namespace Deskhand.Windows.App;

// Separate acknowledgments prevent delayed video receipts from retiring JPEG frames.
public sealed class VideoWindow
{
    private readonly object _gate = new();
    private readonly Dictionary<(int Epoch, int Sequence), double> _pending = new();
    public int Count { get { lock (_gate) return _pending.Count; } }
    public bool Available => Count < 8;
    public bool Stalled(double now) { lock (_gate) return _pending.Values.Any(at => now - at > 2500); }
    public void Sent(int epoch, int sequence, double now)
    {
        lock (_gate)
        {
            if (_pending.Count >= 8) throw new InvalidOperationException("Video window full");
            _pending.Add((epoch, sequence), now);
        }
    }
    public void Ack(byte[] bytes)
    {
        if (bytes.Length != 8) return;
        lock (_gate) _pending.Remove((BinaryPrimitives.ReadInt32BigEndian(bytes), BinaryPrimitives.ReadInt32BigEndian(bytes.AsSpan(4))));
    }
}
