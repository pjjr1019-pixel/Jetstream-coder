using System.Net.WebSockets;
using System.Text;

namespace Deskhand.Windows.App;

/// <summary>Outbound-only WebSocket to the relay for one room, with automatic reconnect.</summary>
public sealed class RelayLink : IDisposable
{
    private readonly string _url;
    private readonly CancellationTokenSource _cts = new();
    private readonly SemaphoreSlim _sendLock = new(1, 1);
    private ClientWebSocket? _socket;

    public RelayLink(string baseUrl, string room, string role = "host")
    {
        _url = $"{baseUrl.TrimEnd('/')}/ws/{room}?role={role}";
    }

    public bool Connected => _socket?.State == WebSocketState.Open;
    // B2-A B-6: the last connection failure, if any. Null while connected
    // or before the first failure. The host surfaces this in the relay
    // status line; previously every failure was swallowed silently and
    // the only symptom was RelayConnected == false.
    public string? LastError { get; private set; }
    public event Action? PeerJoined;
    public event Action? PeerLeft;
    public event Action<byte[]>? Message;
    public event Action? ConnectionChanged;

    public void Start() => _ = Task.Run(RunAsync);

    private async Task RunAsync()
    {
        var delay = TimeSpan.FromSeconds(1);
        while (!_cts.IsCancellationRequested)
        {
            using var socket = new ClientWebSocket();
            socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(20);
            try
            {
                using var connectCts = CancellationTokenSource.CreateLinkedTokenSource(_cts.Token);
                connectCts.CancelAfter(TimeSpan.FromSeconds(15));
                await socket.ConnectAsync(new Uri(_url), connectCts.Token);
                _socket = socket;
                LastError = null;
                delay = TimeSpan.FromSeconds(1);
                ConnectionChanged?.Invoke();
                await ReceiveLoop(socket);
            }
            catch (Exception ex) when (ex is WebSocketException or OperationCanceledException or HttpRequestException or UriFormatException)
            {
                // B2-A B-6: previously this block was empty, so a bad relay
                // URL retried forever with no trace anywhere.
                RecordRelayError(ex);
            }
            catch (Exception ex) when (ex is not OutOfMemoryException and not StackOverflowException and not AccessViolationException)
            {
                // A callback failure must not leave an apparently connected socket
                // whose receive loop has stopped processing video and controls.
                RecordRelayError(ex);
                socket.Abort();
            }
            _socket = null;
            ConnectionChanged?.Invoke();
            PeerLeft?.Invoke();
            try
            {
                await Task.Delay(delay, _cts.Token);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            delay = TimeSpan.FromSeconds(Math.Min(30, delay.TotalSeconds * 2));
        }
    }

    private async Task ReceiveLoop(ClientWebSocket socket)
    {
        var buffer = new byte[64 * 1024];
        using var ms = new MemoryStream();
        while (socket.State == WebSocketState.Open && !_cts.IsCancellationRequested)
        {
            ms.SetLength(0);
            ValueWebSocketReceiveResult result;
            do
            {
                result = await socket.ReceiveAsync(buffer.AsMemory(), _cts.Token);
                if (result.MessageType == WebSocketMessageType.Close) return;
                ms.Write(buffer, 0, result.Count);
                if (ms.Length > 8 * 1024 * 1024) return;
            } while (!result.EndOfMessage);

            if (result.MessageType == WebSocketMessageType.Text)
            {
                switch (Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length))
                {
                    case "peer-joined": PeerJoined?.Invoke(); break;
                    case "peer-left": PeerLeft?.Invoke(); break;
                }
            }
            else
            {
                Message?.Invoke(ms.ToArray());
            }
        }
    }

    public async Task<bool> SendAsync(byte[] data)
    {
        var socket = _socket;
        if (socket is null || socket.State != WebSocketState.Open) return false;
        await _sendLock.WaitAsync(_cts.Token);
        try
        {
            await socket.SendAsync(data, WebSocketMessageType.Binary, true, _cts.Token);
            return true;
        }
        catch (Exception ex) when (ex is WebSocketException or OperationCanceledException or ObjectDisposedException)
        {
            return false;
        }
        finally
        {
            _sendLock.Release();
        }
    }

    // B2-A B-6: records the failure (once per distinct message, so a
    // retrying loop does not spam the debug log) and leaves it readable
    // via LastError until the next successful connect clears it. A
    // dispose-driven cancel is not an error and is not recorded.
    private void RecordRelayError(Exception ex)
    {
        if (_cts.IsCancellationRequested) return;
        var message = ex.GetType().Name + ": " + ex.Message;
        if (LastError != message)
        {
            LastError = message;
            System.Diagnostics.Debug.WriteLine($"Deskhand relay connection failed: {message}");
        }
    }

    public void Dispose()
    {
        _cts.Cancel();
        try { _socket?.Abort(); } catch (ObjectDisposedException) { }
    }
}
