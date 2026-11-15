using System.Diagnostics;

namespace Deskhand.Windows.App;

public readonly record struct DesktopChange(bool Changed, bool Substantial, bool Resized = false);
public readonly record struct EncodedDesktop(byte[] Bytes, int Width, int Height, long Quality);

// Independent of capture/transport: callers supply monotonic milliseconds, enabling deterministic tests.
public sealed class AdaptiveStream
{
    public const int MaxFrameBytes = 3584 * 1024;
    // Approximately 60 FPS; keep quality tiers and congestion thresholds independent of pacing.
    public const int MotionFrameMilliseconds = 17;
    private static readonly (int Width, long Quality)[] Tiers = [(1920, 82), (1600, 78), (1280, 75), (1024, 70), (800, 60)];
    private double _lastMotion, _lastSent = double.NegativeInfinity;
    private bool _dirty = true, _refined;
    private int _good;
    public int Tier { get; private set; }
    public double MotionAge(double now) => now - _lastMotion;
    public bool Motion(double now) => now - _lastMotion < 500;
    public int PollMilliseconds(double now) => Motion(now) ? MotionFrameMilliseconds : 100;
    public void Observe(DesktopChange change, double now, bool remoteMotion = false)
    {
        if (change.Resized) { Tier = 0; _good = 0; _dirty = true; _refined = false; }
        if (change.Changed) _dirty = true;
        if (change.Substantial || change.Resized || remoteMotion)
        { _lastMotion = now; _refined = false; _dirty = true; }
    }
    public bool Due(double now) => Motion(now) ? _dirty && now - _lastSent >= MotionFrameMilliseconds :
        (!_refined || _dirty) && now - _lastSent >= 200;
    public (int Width, long Quality) Profile(double now, int nativeWidth) => Motion(now) ? Tiers[Tier] : (nativeWidth, 95);
    public void Sent(double now, bool refinement)
    { _lastSent = now; _dirty = false; if (refinement) _refined = true; }
    // Called only for frames actually transmitted during motion; idle polling is never an adaptation sample.
    public void Sample(double ackMs, double encodeMs, int bytes, double bytesPerSecond)
    {
        var cost = Math.Max(encodeMs, Math.Max(ackMs / 2, bytesPerSecond > 0 ? bytes * 1000 / bytesPerSecond : 0));
        if (cost > 83 && Tier < Tiers.Length - 1) { Tier++; _good = 0; }
        else if (cost < 42 && ++_good >= 120) { Tier = Math.Max(0, Tier - 1); _good = 0; }
        else if (cost >= 42) _good = 0;
    }
    public static int ByteBudget(bool refinement, double bytesPerSecond) =>
        refinement && bytesPerSecond > 0 ? (int)Math.Clamp(bytesPerSecond * .5, 1, MaxFrameBytes) : MaxFrameBytes;
}

// Existing ordered, unnumbered ACKs retire the oldest frame. No transport/protocol change.
public sealed class StreamFlight
{
    private readonly object _gate = new();
    private readonly Queue<(double At, int Bytes)> _sent = new();
    private double _lastAck;
    public double AckMilliseconds { get; private set; }
    public double BytesPerSecond { get; private set; }
    public bool Available { get { lock (_gate) return _sent.Count < 2; } }
    public int Count { get { lock (_gate) return _sent.Count; } }
    // Batch 2 (100.030, B1): the JPEG flight had no timeout — a lost ACK
    // stalled hybrid video forever (HybridStream waited on flight.Count)
    // and starved the JPEG loop (flight.Available false forever). Same
    // 2.5 s stall threshold VideoWindow uses for video frames.
    public bool Stalled(double now) { lock (_gate) return _sent.Any(frame => now - frame.At > 2500); }
    public void RetireStalled(double now) { lock (_gate) { while (_sent.TryPeek(out var frame) && now - frame.At > 2500) _sent.Dequeue(); } }
    public void ResetEstimate() { lock (_gate) { BytesPerSecond = 0; AckMilliseconds = 0; _lastAck = 0; } }
    public void Sent(double now, int bytes)
    { lock (_gate) { if (_sent.Count >= 2) throw new InvalidOperationException("Frame window full"); _sent.Enqueue((now, bytes)); } }
    public void Ack(double now)
    {
        lock (_gate)
        {
            if (!_sent.TryDequeue(out var frame)) return;
            var delay = Math.Max(1, now - frame.At);
            AckMilliseconds = AckMilliseconds == 0 ? delay : .8 * AckMilliseconds + .2 * delay;
            // Never include idle time before this frame was sent; cap burst ACK estimates by its latency.
            var elapsed = Math.Max(delay / 2, _lastAck > frame.At ? now - _lastAck : delay);
            var rate = frame.Bytes * 1000 / Math.Max(1, elapsed);
            BytesPerSecond = BytesPerSecond == 0 ? rate : .8 * BytesPerSecond + .2 * rate;
            _lastAck = now;
        }
    }
    public static double Now => Stopwatch.GetTimestamp() * 1000.0 / Stopwatch.Frequency;
}
