using System.Buffers.Binary;
using Deskhand.Core;

namespace Deskhand.Windows.App;

public sealed partial class HostEngine
{
    private sealed partial class Listener
    {
        private volatile bool _videoFailed;
        private VideoWindow? _videoWindow;
        private static int _videoEpoch;

        private async Task HybridStreamLoop(CancellationToken ct, StreamFlight flight, SecureSession session)
        {
            GpuVideoEncoder? encoder = null;
            var window = new VideoWindow();
            _videoWindow = window;
            var fallback = false;
            try
            {
                using var capture = _engine._captureFactory();
                var policy = new AdaptiveStream();
                var lastCheck = double.NegativeInfinity;
                var lastSharp = double.NegativeInfinity;
                var sharpInterval = 2000.0;
                var lastRemote = 0.0;
                var epoch = 0;
                var sequence = 0;
                var startedAt = 0.0;
                var waitingKey = true;
                var sentVideo = false;
                var sourceSize = System.Drawing.Size.Empty;
                var log = Environment.GetEnvironmentVariable("DESKHAND_FPS_LOG");
                while (!ct.IsCancellationRequested)
                {
                    var now = StreamFlight.Now;
                    if (_videoFailed || window.Stalled(now))
                        throw new IOException("Video unavailable; returning to JPEG streaming");
                    if (now - lastCheck >= 50)
                    {
                        var change = capture.Capture();
                        var remote = Volatile.Read(ref _remoteMotionAt);
                        policy.Observe(change, now, remote > lastRemote);
                        lastRemote = remote;
                        lastCheck = now;
                        if (change.Resized)
                        {
                            sourceSize = ScreenCapturer.Bounds.Size;
                            encoder?.Dispose(); encoder = null;
                            waitingKey = true;
                            lastSharp = double.NegativeInfinity;
                            await SendInfoAsync();
                        }
                    }
                    // A mode change can invalidate the old capture process. Handle the
                    // new dimensions above before deciding whether video has failed.
                    if (encoder?.Failed == true) throw new IOException("Video encoder stopped");
                    if (policy.Motion(now))
                    {
                        // Do not build a video queue behind a large sharp frame.
                        if (flight.Count != 0)
                        {
                            // Batch 2 (100.030, B1): a JPEG frame whose ACK never
                            // arrives must not block video forever — retire
                            // frames stalled over 2.5 s (same threshold as
                            // VideoWindow), then proceed once the flight drains.
                            flight.RetireStalled(now);
                            if (flight.Count != 0) { await Task.Delay(10, ct); continue; }
                        }
                        if (encoder is null)
                        {
                            encoder = new GpuVideoEncoder(sourceSize.Width, sourceSize.Height);
                            startedAt = now; waitingKey = true; sentVideo = false;
                        }
                        // Driver and encoder initialization can take several seconds
                        // after another GPU workload releases the device.
                        if (!sentVideo && now - startedAt > 10000) throw new IOException("Video startup timed out");
                        if (!sentVideo && policy.Due(now))
                        {
                            // Batch 2 (100.030, B2): NVENC startup can take
                            // seconds, during which the phone previously saw a
                            // frozen frame. Bridge the gap with tiered JPEG
                            // motion frames (same encoding the JPEG loop uses)
                            // until the first video frame is actually sent.
                            // Best-effort: a failed bridge frame must not kill
                            // video startup.
                            try
                            {
                                var bridgeProfile = policy.Profile(now, capture.Width);
                                var bridge = capture.Encode(bridgeProfile.Width, bridgeProfile.Quality,
                                    AdaptiveStream.ByteBudget(false, flight.BytesPerSecond));
                                if (await SendFrame(session, flight, bridge, ct, false, Volatile.Read(ref _remoteMotionAt), policy.MotionAge(now)))
                                    policy.Sent(StreamFlight.Now, false);
                            }
                            catch (Exception ex) when (ex is IOException or InvalidOperationException or System.Runtime.InteropServices.ExternalException)
                            {
                                // Bridge failure is non-fatal; video startup continues.
                            }
                        }
                        if (window.Available && encoder.Take() is { } video)
                        {
                            if (now - video.At > 250) { waitingKey = true; continue; }
                            if (video.Restart) waitingKey = true;
                            if (waitingKey && !video.Key) continue;
                            if (waitingKey) { epoch = Interlocked.Increment(ref _videoEpoch); waitingKey = false; }
                            var packet = new byte[16 + video.Bytes.Length];
                            BinaryPrimitives.WriteInt32BigEndian(packet, epoch);
                            BinaryPrimitives.WriteInt32BigEndian(packet.AsSpan(4), ++sequence);
                            BinaryPrimitives.WriteInt32BigEndian(packet.AsSpan(8), encoder.Width);
                            BinaryPrimitives.WriteInt32BigEndian(packet.AsSpan(12), encoder.Height);
                            video.Bytes.CopyTo(packet, 16);
                            await _sealLock.WaitAsync(ct);
                            try
                            {
                                if (!ReferenceEquals(_session, session) || _engine.Config.Paused) break;
                                window.Sent(epoch, sequence, now);
                                await _link.SendAsync(session.Seal(Msg.VideoFrame, packet)).WaitAsync(ct);
                            }
                            finally { _sealLock.Release(); }
                            sentVideo = true;
                            if (log is not null) File.AppendAllText(log, $"{DateTime.UtcNow:O} hybrid=video bytes={video.Bytes.Length} dimensions={encoder.Width}x{encoder.Height} in_flight={window.Count}\n");
                        }
                    }
                    else
                    {
                        // Keep the encoder warm across brief pauses while scrolling.
                        // Delay large still frames until motion has genuinely stopped;
                        // otherwise a slow mobile link blocks the next video burst.
                        if (sentVideo && policy.MotionAge(now) < 1500)
                        {
                            await Task.Delay(10, ct);
                            continue;
                        }
                        if (encoder is not null)
                        {
                            encoder.Dispose(); encoder = null;
                            lastSharp = double.NegativeInfinity;
                        }
                        if (window.Count == 0 && flight.Count == 0 && policy.Due(now) && now - lastSharp >= sharpInterval)
                        {
                            // Native resolution and q95, bounded only by the protocol frame limit.
                            var frame = capture.Encode(capture.Width, 95, AdaptiveStream.MaxFrameBytes);
                            var newer = capture.Capture();
                            var remote = Volatile.Read(ref _remoteMotionAt);
                            policy.Observe(newer, StreamFlight.Now, remote > lastRemote);
                            lastRemote = remote;
                            if (policy.Motion(StreamFlight.Now)) continue;
                            if (await SendFrame(session, flight, frame, ct, true, remote, policy.MotionAge(StreamFlight.Now)))
                            {
                                lastSharp = StreamFlight.Now;
                                sharpInterval = Math.Max(2000, flight.BytesPerSecond > 0 ? frame.Bytes.Length * 4000.0 / flight.BytesPerSecond : 5000);
                                policy.Sent(lastSharp, true);
                                policy.Observe(newer, lastSharp);
                            }
                        }
                    }
                    await Task.Delay(5, ct);
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception ex) when (ex is IOException or InvalidOperationException or System.ComponentModel.Win32Exception or System.Net.WebSockets.WebSocketException)
            {
                fallback = !ct.IsCancellationRequested;
                if (fallback)
                {
                    var log = Environment.GetEnvironmentVariable("DESKHAND_FPS_LOG");
                    if (log is not null) { try { File.AppendAllText(log, $"{DateTime.UtcNow:O} hybrid=fallback reason={ex.GetType().Name}: {ex.Message}\n"); } catch (IOException) { } }
                    _engine.RaiseNotice("Video unavailable; using the compatible image stream.");
                }
            }
            finally { encoder?.Dispose(); if (ReferenceEquals(_videoWindow, window)) _videoWindow = null; }
            if (fallback) await StreamLoop(ct, flight, session);
        }
    }
}
