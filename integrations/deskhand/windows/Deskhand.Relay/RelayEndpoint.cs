using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;

namespace Deskhand.Relay;

public sealed class RelayOptions
{
    public int MaxMessageBytes { get; init; } = 4 * 1024 * 1024;
    public int MaxConnectionsPerIp { get; init; } = 20;
    public int MaxAttemptsPerMinutePerIp { get; init; } = 120;
}

/// <summary>
/// Untrusted rendezvous relay. It pairs one host and one client per opaque room id and forwards binary
/// messages unchanged. It never sees keys or plaintext; all payloads are end-to-end encrypted by the peers.
/// Text messages are relay control events only ("peer-joined", "peer-left").
/// </summary>
public static partial class RelayEndpoint
{
    /// <summary>Per-start random token (32 lowercase hex chars) served by GET /healthz. Generated once per relay start (in MapRelay), in-memory only, never persisted, never logged. Read in-process by the re-mint echo check.</summary>
    public static string? HealthzToken { get; private set; }

    [GeneratedRegex("^[0-9a-f]{32}$")]
    private static partial Regex RoomPattern();

    private sealed class Peer(WebSocket socket)
    {
        public WebSocket Socket { get; } = socket;
        private readonly SemaphoreSlim _sendLock = new(1, 1);

        public async Task SendAsync(ReadOnlyMemory<byte> data, WebSocketMessageType type)
        {
            await _sendLock.WaitAsync();
            try
            {
                if (Socket.State == WebSocketState.Open)
                    await Socket.SendAsync(data, type, true, CancellationToken.None);
            }
            catch (Exception ex) when (ex is WebSocketException or ObjectDisposedException or OperationCanceledException)
            {
            }
            finally
            {
                _sendLock.Release();
            }
        }

        public Task NotifyAsync(string text) => SendAsync(Encoding.UTF8.GetBytes(text), WebSocketMessageType.Text);
    }

    private sealed class Room
    {
        public readonly object Gate = new();
        public Peer? Host;
        public Peer? Client;
        public Peer? Connector;
    }

    private sealed class IpState
    {
        public int Connections;
        public int Attempts;
        public DateTime WindowStart = DateTime.UtcNow;
    }

    public static void MapRelay(this WebApplication app, RelayOptions? options = null)
    {
        options ??= new RelayOptions();
        var rooms = new ConcurrentDictionary<string, Room>();
        var ips = new ConcurrentDictionary<IPAddress, IpState>();

        app.UseWebSockets(new WebSocketOptions { KeepAliveInterval = TimeSpan.FromSeconds(20) });
        app.MapGet("/health", () => "ok");
        HealthzToken = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        app.MapGet("/healthz", () => HealthzToken);

        app.Map("/ws/{room}", async (HttpContext ctx, string room) =>
        {
            if (!ctx.WebSockets.IsWebSocketRequest || !RoomPattern().IsMatch(room))
            {
                ctx.Response.StatusCode = StatusCodes.Status400BadRequest;
                return;
            }

            var ip = ctx.Connection.RemoteIpAddress ?? IPAddress.Loopback;
            var state = ips.GetOrAdd(ip, _ => new IpState());
            lock (state)
            {
                if (DateTime.UtcNow - state.WindowStart > TimeSpan.FromMinutes(1))
                {
                    state.WindowStart = DateTime.UtcNow;
                    state.Attempts = 0;
                }
                if (++state.Attempts > options.MaxAttemptsPerMinutePerIp || state.Connections >= options.MaxConnectionsPerIp)
                {
                    ctx.Response.StatusCode = StatusCodes.Status429TooManyRequests;
                    return;
                }
                state.Connections++;
            }

            try
            {
                var role = ctx.Request.Query["role"].ToString();
                if (role is not ("host" or "client" or "connector"))
                {
                    ctx.Response.StatusCode = StatusCodes.Status400BadRequest;
                    return;
                }
                var isHost = role == "host";
                var isConnector = role == "connector";
                var isPeer = !isHost;

                var r = isHost ? rooms.GetOrAdd(room, _ => new Room()) : rooms.GetValueOrDefault(room);
                if (r is null || (isPeer && r.Host is null))
                {
                    ctx.Response.StatusCode = StatusCodes.Status404NotFound;
                    return;
                }

                using var socket = await ctx.WebSockets.AcceptWebSocketAsync();
                var me = new Peer(socket);
                Peer? replaced;
                Peer? other;
                var rejected = false;
                lock (r.Gate)
                {
                    if (isHost)
                    {
                        replaced = r.Host;
                        r.Host = me;
                        other = r.Client ?? r.Connector;
                    }
                    else if (isConnector)
                    {
                        rejected = r.Client is not null || r.Connector is not null;
                        replaced = null;
                        other = r.Host;
                        if (!rejected) r.Connector = me;
                    }
                    else
                    {
                        rejected = r.Connector is not null;
                        replaced = rejected ? null : r.Client;
                        other = r.Host;
                        if (!rejected) r.Client = me;
                    }
                }
                if (rejected)
                {
                    await CloseQuietly(me.Socket, WebSocketCloseStatus.PolicyViolation, "peer role already occupied");
                    return;
                }
                if (replaced is not null) await CloseQuietly(replaced.Socket, WebSocketCloseStatus.PolicyViolation, "replaced");
                if (other is not null)
                {
                    await other.NotifyAsync("peer-joined");
                    await me.NotifyAsync("peer-joined");
                }

                try
                {
                    using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(ctx.RequestAborted, app.Lifetime.ApplicationStopping);
                    await Pump(me, r, role, options.MaxMessageBytes, cancellation.Token);
                }
                finally
                {
                    Peer? peer;
                    lock (r.Gate)
                    {
                        if (isHost && ReferenceEquals(r.Host, me)) r.Host = null;
                        if (!isHost && ReferenceEquals(r.Client, me)) r.Client = null;
                        if (isConnector && ReferenceEquals(r.Connector, me)) r.Connector = null;
                        peer = isHost ? r.Client ?? r.Connector : r.Host;
                        if (r.Host is null && r.Client is null && r.Connector is null) rooms.TryRemove(room, out _);
                    }
                    if (peer is not null) await peer.NotifyAsync("peer-left");
                }
            }
            finally
            {
                lock (state) state.Connections--;
            }
        });

        _ = Task.Run(async () =>
        {
            while (true)
            {
                await Task.Delay(TimeSpan.FromMinutes(5));
                foreach (var (key, s) in ips)
                {
                    lock (s)
                    {
                        if (s.Connections == 0 && DateTime.UtcNow - s.WindowStart > TimeSpan.FromMinutes(5)) ips.TryRemove(key, out _);
                    }
                }
            }
        });
    }

    private static async Task Pump(Peer me, Room room, string role, int maxBytes, CancellationToken cancellation)
    {
        var buffer = new byte[64 * 1024];
        using var message = new MemoryStream();
        while (me.Socket.State == WebSocketState.Open)
        {
            message.SetLength(0);
            ValueWebSocketReceiveResult result;
            do
            {
                try
                {
                    result = await me.Socket.ReceiveAsync(buffer.AsMemory(), cancellation);
                }
                catch (Exception ex) when (ex is WebSocketException or OperationCanceledException)
                {
                    return;
                }
                if (result.MessageType == WebSocketMessageType.Close) return;
                message.Write(buffer, 0, result.Count);
                var roomLimit = role == "connector" || role == "host" && room.Connector is not null
                    ? Math.Min(maxBytes, 64 * 1024 + 26) : maxBytes;
                if (message.Length > roomLimit)
                {
                    await CloseQuietly(me.Socket, WebSocketCloseStatus.MessageTooBig, "too big");
                    return;
                }
            } while (!result.EndOfMessage);

            if (result.MessageType != WebSocketMessageType.Binary) continue;
            Peer? peer;
            lock (room.Gate) peer = role == "host" ? room.Client ?? room.Connector : room.Host;
            if (peer is not null) await peer.SendAsync(message.ToArray(), WebSocketMessageType.Binary);
        }
    }

    private static async Task CloseQuietly(WebSocket socket, WebSocketCloseStatus status, string reason)
    {
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            await socket.CloseOutputAsync(status, reason, cts.Token);
        }
        catch (Exception ex) when (ex is WebSocketException or ObjectDisposedException or OperationCanceledException)
        {
        }
    }
}
