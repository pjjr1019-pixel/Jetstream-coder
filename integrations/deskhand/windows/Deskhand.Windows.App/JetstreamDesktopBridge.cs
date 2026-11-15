using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace Deskhand.Windows.App;

// Local desktop controls run on the existing WinForms UI thread and reuse the
// host's authority checks. Pipe access is restricted to the current OS user.
internal sealed class JetstreamDesktopBridge : IDisposable
{
    private readonly CancellationTokenSource _stop = new();
    private readonly SynchronizationContext _ui;
    private readonly MainForm _form;
    private readonly HostEngine _engine;
    private readonly AppConfig _config;
    private TaskCompletionSource<PairDecision>? _pairDecision;
    private object? _pairRequest;
    internal JetstreamDesktopBridge(string pipeName, SynchronizationContext ui, MainForm form, HostEngine engine, AppConfig config)
    {
        _ui = ui; _form = form; _engine = engine; _config = config;
        engine.ApprovalHandler = RequestPairApproval;
        _ = Serve(pipeName);
    }
    private async Task<PairDecision> RequestPairApproval(PairRequestInfo info)
    {
        var decision = new TaskCompletionSource<PairDecision>(TaskCreationOptions.RunContinuationsAsynchronously);
        var id = Guid.NewGuid().ToString();
        await OnUi(() => { _pairDecision?.TrySetResult(PairDecision.Reject); _pairDecision = decision; _pairRequest = new { id, name = info.DeviceName, code = info.ConfirmCode }; return Task.FromResult<object>(true); });
        try { return await decision.Task.WaitAsync(TimeSpan.FromMinutes(2), _stop.Token); }
        catch (Exception error) when (error is TimeoutException or OperationCanceledException) { return PairDecision.Reject; }
        finally { await OnUi(() => { if (_pairDecision == decision) { _pairDecision = null; _pairRequest = null; } return Task.FromResult<object>(true); }); }
    }
    private Task<object> OnUi(Func<Task<object>> action)
    {
        var done = new TaskCompletionSource<object>(TaskCreationOptions.RunContinuationsAsynchronously);
        _ui.Post(async _ => { try { done.TrySetResult(await action()); } catch (Exception error) { done.TrySetException(error); } }, null);
        return done.Task;
    }
    private async Task<object> Execute(JsonElement request)
    {
        var operation = request.GetProperty("operation").GetString();
        switch (operation)
        {
            case "features": return _form.JetstreamFeatures();
            case "settings": return await _form.JetstreamSettings(request);
            case "assistant": return await _form.JetstreamAssistant(request.GetProperty("command"));
            case "status": return new { network = await _form.JetstreamNetworkStatus(), paused = _config.Paused, pairing = _pairRequest,
                phones = _config.Devices.Select(d => new { id = d.Id, name = d.Name, control = d.Control, connected = _engine.ActiveDevices.Contains(d.Name) }).ToArray(),
                approvals = _engine.AllApprovals(), relayError = _engine.RelayError, orientation = _config.PhoneFriendlyDisplay };
            case "pair": return await _form.JetstreamCreatePairing();
            case "pair-configured": return await _form.JetstreamCreatePairing(configuredRelay: true);
            case "login": return await _form.JetstreamLogin();
            case "cancel-pair": _engine.CancelPairing(); return true;
            case "pair-decision":
                if (_pairRequest is null || _pairDecision is null || JsonSerializer.SerializeToElement(_pairRequest).GetProperty("id").GetString() != request.GetProperty("id").GetString()) throw new InvalidOperationException("Pairing approval expired.");
                var decision = request.GetProperty("decision").GetString() switch { "control" => PairDecision.Control, "view" => PairDecision.ViewOnly, "reject" => PairDecision.Reject, _ => throw new InvalidOperationException("Invalid pairing decision.") };
                _pairDecision.TrySetResult(decision); return true;
            case "pause": _engine.SetPaused(request.GetProperty("enabled").GetBoolean()); return true;
            case "disconnect": _engine.DisconnectAll(); return true;
            case "orientation": _engine.SetPhoneFriendlyDisplay(request.GetProperty("enabled").GetBoolean()); return true;
            case "phone-control": case "remove-phone":
                var phone = _config.Devices.SingleOrDefault(d => d.Id == request.GetProperty("id").GetString()) ?? throw new InvalidOperationException("Phone is unavailable.");
                if (operation == "remove-phone") _engine.Revoke(phone); else _engine.SetControl(phone, request.GetProperty("enabled").GetBoolean()); return true;
            case "approval": return _engine.Approvals.Resolve(Guid.Parse(request.GetProperty("id").GetString()!), request.GetProperty("approved").GetBoolean());
            default: throw new InvalidOperationException("Unsupported desktop control.");
        }
    }
    private async Task Serve(string name)
    {
        while (!_stop.IsCancellationRequested)
        {
            NamedPipeServerStream? pipe = null;
            try
            {
                pipe = new NamedPipeServerStream(name, PipeDirection.InOut, NamedPipeServerStream.MaxAllowedServerInstances,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await pipe.WaitForConnectionAsync(_stop.Token);
                _ = Handle(pipe); // Service calls must not block status/approval connections.
                pipe = null;
            }
            catch (Exception error) when (error is IOException or OperationCanceledException) { }
            finally { if (pipe is not null) await pipe.DisposeAsync(); }
        }
    }
    private async Task Handle(NamedPipeServerStream pipe)
    {
        await using (pipe)
        {
            try
            {
                using var reader = new StreamReader(pipe, Encoding.UTF8, false, 4096, leaveOpen: true);
                using var writer = new StreamWriter(pipe, new UTF8Encoding(false), 4096, leaveOpen: true) { AutoFlush = true };
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token); timeout.CancelAfter(TimeSpan.FromSeconds(20));
                var line = await reader.ReadLineAsync(timeout.Token);
                if (line is null || line.Length > 8192) return;
                object response;
                try { using var json = JsonDocument.Parse(line); var command = json.RootElement.Clone(); response = new { ok = true, result = await OnUi(() => Execute(command)) }; }
                catch (Exception error) { response = new { ok = false, error = error.Message }; }
                await writer.WriteLineAsync(JsonSerializer.Serialize(response));
            }
            catch (Exception error) when (error is IOException or OperationCanceledException) { }
        }
    }
    public void Dispose() { _pairDecision?.TrySetResult(PairDecision.Reject); _stop.Cancel(); }
}
