using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Deskhand.Windows.App;

public interface IAssistantChat
{
    Task<JsonElement> CallAsync(JsonElement command);
}

/// <summary>One owned Python child, shared by Windows and encrypted phone chat.</summary>
public sealed class AssistantClient : IAssistantChat, IDisposable
{
    private readonly ConcurrentDictionary<long, TaskCompletionSource<JsonElement>> _pending = new();
    private readonly SemaphoreSlim _write = new(1, 1);
    internal Action? PhoneControlWaitingForFixture { get; set; }
    internal Action? PhoneControlDeniedForFixture { get; set; }
    private readonly object _gate = new();
    private Process? _process;
    private long _next;
    private readonly string _data;
    private readonly bool _fixture;
    private readonly TimeSpan _requestTimeout;
    private JettJournal? _jettJournal;
    private Func<Guid, Guid, Guid, string, string, Task<AssistantActionResult>>? _hostAction;
    private Func<object>? _healthSnapshot;
    private Func<Guid, Guid, string, string, string, string, bool>? _beginTask;
    private Func<Guid, Guid, string, bool>? _endTask;
    private Func<Guid, Guid, bool>? _activeTask;
    private Action? _transportLost;
    private Func<JsonElement, Func<Func<bool>, bool>?, Task<JsonElement>>? _accessRequest;
    internal void AttachFullPcAccess(Func<JsonElement, Func<Func<bool>, bool>?, Task<JsonElement>> handler) => _accessRequest = handler;
    private string? _museStopEventName;
    private Process? _lossReported;
    private readonly System.Threading.Timer _poll;
    private int _polling;
    private string _lastHistory = "";
    private long? _lastSeq;
    private bool _warmStarted;
    private string _diagnostic = "";
    private int _disposed;
    public event Action<JsonElement>? HistoryChanged;
    public event Action<JsonElement>? RuntimeStreamChanged;

    internal void AttachJettJournal(JettJournal journal) => _jettJournal = journal;
    internal void AttachHostAction(Func<Guid, Guid, Guid, string, string, Task<AssistantActionResult>> action) => _hostAction = action;
    internal void AttachHealthSnapshot(Func<object> provider) => _healthSnapshot = provider;
    internal void AttachTaskLifecycle(Func<Guid, Guid, string, string, string, string, bool> begin, Func<Guid, Guid, string, bool> end, Func<Guid, Guid, bool> active) { _beginTask = begin; _endTask = end; _activeTask = active; }
    internal void AttachTransportLost(Action handler) => _transportLost = handler;
    internal void AttachMuseStopEvent(string name)
    {
        lock (_gate)
        {
            if (_process is { HasExited: false }) throw new InvalidOperationException("Attach Muse STOP before starting chat.");
            _museStopEventName = name;
        }
    }

    internal void AcknowledgeJettReplyOnWindows(Guid replyId) => _jettJournal?.AcknowledgeSurface(replyId, "windows");

    public AssistantClient(string data, bool fixture = false) : this(data, fixture, TimeSpan.FromSeconds(45)) { }

    internal AssistantClient(string data, bool fixture, TimeSpan requestTimeout)
    {
        if (requestTimeout <= TimeSpan.Zero || requestTimeout > TimeSpan.FromMinutes(5)) throw new ArgumentOutOfRangeException(nameof(requestTimeout));
        _data = data;
        _fixture = fixture;
        _requestTimeout = requestTimeout;
        _poll = new System.Threading.Timer(async _ => await Poll(), null, Timeout.Infinite, Timeout.Infinite);
    }

    private Process Start()
    {
        lock (_gate)
        {
            ObjectDisposedException.ThrowIf(_disposed != 0, this);
            if (_process is { } existing)
            {
                if (!existing.HasExited) return existing;
                ReportTransportLoss(existing);
                FailPending(new IOException("Deskhand chat disconnected; delivery may be unconfirmed."));
                existing.Dispose();
                _process = null;
                _warmStarted = false;
            }
            var script = Environment.GetEnvironmentVariable("DESKHAND_ASSISTANT_SCRIPT") ??
                Path.Combine(AppContext.BaseDirectory, "assistant_service", "deskhand_service.py");
            if (!File.Exists(script)) throw new FileNotFoundException("Deskhand chat service is missing", script);
            var bundledPython = Path.Combine(AppContext.BaseDirectory, "python", "python.exe");
            var python = Environment.GetEnvironmentVariable("DESKHAND_PYTHON") ?? (File.Exists(bundledPython) ? bundledPython : "python");
            string? packages = null;
            // Windows venv redirectors can close redirected handles while their
            // interpreter child remains alive. Use the venv's own base runtime
            // with its existing libraries instead; never install dependencies.
            if (Path.IsPathFullyQualified(python))
            {
                var venv = Directory.GetParent(Path.GetDirectoryName(python)!)?.FullName;
                var cfg = venv is null ? null : Path.Combine(venv, "pyvenv.cfg");
                if (cfg is not null && File.Exists(cfg))
                {
                    var home = File.ReadLines(cfg).FirstOrDefault(x => x.StartsWith("home = "))?[7..].Trim();
                    if (home is not null && File.Exists(Path.Combine(home, "python.exe")))
                    {
                        python = Path.Combine(home, "python.exe");
                        packages = Path.Combine(venv!, "Lib", "site-packages");
                    }
                }
            }
            var info = new ProcessStartInfo(python)
            {
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true,
                RedirectStandardOutput = true, RedirectStandardError = true,
                StandardInputEncoding = new System.Text.UTF8Encoding(false),
                StandardOutputEncoding = System.Text.Encoding.UTF8
            };
            if (packages is not null)
                info.Environment["PYTHONPATH"] = packages + (info.Environment.TryGetValue("PYTHONPATH", out var oldPath) && !string.IsNullOrEmpty(oldPath) ? Path.PathSeparator + oldPath : "");
            if (string.Equals(python, bundledPython, StringComparison.OrdinalIgnoreCase))
            {
                info.Environment["PYTHONNOUSERSITE"] = "1";
                info.Environment.Remove("PYTHONPATH");
                info.Environment.Remove("PYTHONHOME");
            }
            info.Environment["PYTHONDONTWRITEBYTECODE"] = "1";
            if (Environment.GetCommandLineArgs().Contains("--jetstream"))
            {
                // Resolve code from this bundle/check-out; do not execute the
                // standalone Deskhand source folder as an implicit fallback.
                info.Environment["DESKHAND_TASK_AGENT_DIR"] = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "TaskAgent"));
                for (var ancestor = new DirectoryInfo(AppContext.BaseDirectory); ancestor is not null; ancestor = ancestor.Parent)
                {
                    var candidate = Path.Combine(ancestor.FullName, "TaskAgent");
                    if (!File.Exists(Path.Combine(candidate, "laptop_agent_v2.py"))) continue;
                    info.Environment["DESKHAND_TASK_AGENT_DIR"] = candidate;
                    break;
                }
            }
            if (_museStopEventName is not null) info.Environment["DESKHAND_HOST_STOP_EVENT"] = _museStopEventName;
            info.ArgumentList.Add("-u"); info.ArgumentList.Add(script);
            info.ArgumentList.Add("--data"); info.ArgumentList.Add(_data);
            if (_fixture) info.ArgumentList.Add("--fixture");
            var process = Process.Start(info) ?? throw new InvalidOperationException("Could not start Deskhand chat");
            _process = process;
            _ = Task.Run(async () =>
            {
                try
                {
                    while (await process.StandardOutput.ReadLineAsync() is { } line)
                    {
                        // Batch 2 (100.030): one malformed stdout line used to
                        // kill the whole chat transport (a missing "ok" threw
                        // KeyNotFoundException, a stray print threw
                        // JsonException, and both retired the child). A bad
                        // line is now skipped and noted in _diagnostic; only a
                        // failure to read the stream retires the transport.
                        JsonElement value;
                        try
                        {
                            using var doc = JsonDocument.Parse(line);
                            value = doc.RootElement.Clone();
                        }
                        catch (JsonException) { _diagnostic = "Skipped a malformed Deskhand chat line."; continue; }
                        try
                        {
                            if (value.TryGetProperty("host_call", out _))
                            {
                                _ = HandleHostCallAsync(process, value);
                                continue;
                            }
                            if (!value.TryGetProperty("rpc", out var id) || !id.TryGetInt64(out var number)) continue;
                            if (!_pending.TryRemove(number, out var waiting)) continue;
                            if (value.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True && value.TryGetProperty("result", out var result)) waiting.TrySetResult(result.Clone());
                            else if (value.TryGetProperty("ok", out var notOk) && notOk.ValueKind == JsonValueKind.False)
                            {
                                var errorMessage = "Deskhand chat error.";
                                if (value.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.String && error.GetString() is { } message) errorMessage = message;
                                waiting.TrySetException(new InvalidOperationException(errorMessage));
                            }
                            // A line with no usable ok/result is ignored: the
                            // waiting request keeps its timeout instead of
                            // taking the whole transport down with it.
                        }
                        catch (Exception ex) when (ex is JsonException or InvalidOperationException or KeyNotFoundException)
                        { _diagnostic = "Skipped a malformed Deskhand chat line: " + ex.GetType().Name; }
                    }
                }
                catch (Exception ex) { FailPendingFor(process, ex); }
                finally { FailPendingFor(process, new IOException("Deskhand chat disconnected; delivery may be unconfirmed. " + _diagnostic)); }
            });
            // Drain library diagnostics without mixing them with JSON or chat text.
            _ = Task.Run(async () => { try { while (await process.StandardError.ReadLineAsync() is { } line) { _diagnostic = line.Length <= 500 ? line : line[..500]; } } catch (IOException) { } });
            _poll.Change(1000, 1000);
            if (!_warmStarted)
            {
                // Batch 2: warm local models once per service connection so the
                // first real turn is not a cold load. Best-effort: failures are
                // ignored and the warm-up never blocks the caller.
                _warmStarted = true;
                _ = WarmStartAsync();
            }
            return process;
        }
    }

    private async Task HandleHostCallAsync(Process process, JsonElement call)
    {
        string? requestId = null;
        object result = new { status = "rejected", error = "invalid_request" };
        try
        {
            lock (_gate) if (!ReferenceEquals(_process, process) || process.HasExited) return;
            if (call.ValueKind != JsonValueKind.Object || call.EnumerateObject().Count() != 3 ||
                !call.TryGetProperty("host_call", out var id) || id.ValueKind != JsonValueKind.String ||
                !Guid.TryParseExact(id.GetString(), "D", out var parsedId) || !IsUuidV4(parsedId, id.GetString()!) ||
                !call.TryGetProperty("operation", out var operation) || operation.ValueKind != JsonValueKind.String ||
                !call.TryGetProperty("payload", out var payload) || payload.ValueKind != JsonValueKind.Object) return;
            requestId = id.GetString();
            if (operation.GetString() == "action.execute" && _hostAction is not null && payload.EnumerateObject().Count() == 5 &&
                TryId(payload, "task_id", out var task) && TryId(payload, "turn_id", out var turn) && TryId(payload, "action_id", out var action) &&
                payload.TryGetProperty("name", out var name) && name.ValueKind == JsonValueKind.String &&
                payload.TryGetProperty("arguments", out var args) && args.ValueKind == JsonValueKind.Object)
            {
                result = await _hostAction(task, turn, action, name.GetString()!, args.GetRawText());
            }
            else if (operation.GetString() == "task.check" && _activeTask is not null && payload.EnumerateObject().Count() == 2 &&
                TryId(payload, "task_id", out var checkedTask) && TryId(payload, "turn_id", out var checkedTurn))
            {
                result = new { active = _activeTask(checkedTask, checkedTurn) };
            }
            else if (operation.GetString() == "task.end" && _endTask is not null && payload.EnumerateObject().Count() == 3 &&
                TryId(payload, "task_id", out var finishedTask) && TryId(payload, "turn_id", out var finishedTurn) &&
                payload.TryGetProperty("outcome", out var outcome) && outcome.ValueKind == JsonValueKind.String &&
                outcome.GetString() is "completed" or "failed" or "cancelled" or "timeout_unknown")
            {
                result = new { ended = _endTask(finishedTask, finishedTurn, outcome.GetString()!) };
            }
            // 100.016 (Worker B) — `health.snapshot` (STEP0 §0.2): the
            // service's health() op reads the 9 C#-owned rows the engine
            // published on the 2s tick. Strict shape like the task.*
            // handlers: empty object payload, provider attached by the
            // engine's Chat setter. No provider (a client without an
            // engine) falls through to the rejected default, which the
            // service reads as "C# rows UNKNOWN". The provider only
            // copies an in-memory dict under a lock — it never probes,
            // never throws across the channel, never touches the UI
            // thread (HandleHostCallAsync's catch still answers
            // "uncertain" if it ever did).
            else if (operation.GetString() == "health.snapshot" && _healthSnapshot is not null && payload.EnumerateObject().Count() == 0)
            {
                result = _healthSnapshot();
            }
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or ArgumentException or IOException or ObjectDisposedException)
        { result = new { status = "uncertain", error = "host_unavailable" }; }
        catch (Exception)
        {
            // Batch 2 (100.030): any other exception from a host action (e.g.
            // a null dereference inside the engine) used to escape this
            // fire-and-forget handler with no host_result ever written, so
            // Python waited out its full action timeout and the journal action
            // stayed stuck at running. Always answer, even on exception.
            result = new { status = "uncertain", error = "host_unavailable" };
        }
        if (requestId is null || process.HasExited) return;
        var response = JsonSerializer.Serialize(new { op = "host_result", host_call = requestId, result }, new JsonSerializerOptions(JsonSerializerDefaults.Web));
        try
        {
            await _write.WaitAsync();
            try { if (!process.HasExited) { await process.StandardInput.WriteLineAsync(response); await process.StandardInput.FlushAsync(); } }
            finally { _write.Release(); }
        }
        catch (Exception ex) when (ex is IOException or InvalidOperationException or ObjectDisposedException) { }
    }

    private void EndAdmittedTask(Guid task, Guid turn, string outcome) => _endTask?.Invoke(task, turn, outcome);

    private static bool TryId(JsonElement value, string name, out Guid id)
    {
        id = Guid.Empty;
        return value.TryGetProperty(name, out var field) && field.ValueKind == JsonValueKind.String &&
            Guid.TryParseExact(field.GetString(), "D", out id) && IsUuidV4(id, field.GetString()!);
    }

    private void FailPending(Exception error)
    {
        foreach (var pair in _pending)
            if (_pending.TryRemove(pair.Key, out var waiting)) waiting.TrySetException(error);
    }

    private void FailPendingFor(Process process, Exception error)
    {
        lock (_gate) if (ReferenceEquals(_process, process))
        {
            ReportTransportLoss(process);
            FailPending(error);
            // A closed or malformed output channel cannot safely carry replies.
            // Retire the owned child so the next request starts a fresh one.
            if (_disposed == 0)
                try { if (!process.HasExited) { process.Kill(entireProcessTree: true); process.WaitForExit(1000); } }
                catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception) { }
        }
    }

    // The caller holds _gate. Revoke authority before any new child starts.
    private void ReportTransportLoss(Process process)
    {
        if (_disposed != 0 || ReferenceEquals(_lossReported, process)) return;
        _lossReported = process;
        _transportLost?.Invoke();
    }

    public Task<JsonElement> CallAsync(object command) => CallAsync(JsonSerializer.SerializeToElement(command));

    public Task<JsonElement> CallAsync(JsonElement command) => CallAsync(command, null);

    internal Task<JsonElement> CallFromPhoneAsync(JsonElement command, Func<Func<bool>, bool> admissionGuard) => CallAsync(command, admissionGuard);

    private async Task<JsonElement> CallAsync(JsonElement command, Func<Func<bool>, bool>? admissionGuard)
    {
        if (command.TryGetProperty("op", out var accessOp) && accessOp.GetString() is "access_status" or "access_set")
            return _accessRequest is null ? throw new InvalidOperationException("Full PC access is unavailable.") : await _accessRequest(command, admissionGuard);
        var process = Start();
        var id = Interlocked.Increment(ref _next);
        var message = JsonNode.Parse(command.GetRawText())!.AsObject();
        // Only the Windows host may assign an agent task ID; ignore caller input.
        message.Remove("agent_task_id");
        Guid? taskId = null;
        Guid? taskTurn = null;
        if (command.TryGetProperty("op", out var submitOp) && submitOp.GetString() == "submit" &&
            command.TryGetProperty("agent", out var agent) && agent.ValueKind == JsonValueKind.True)
        {
            // Batch 2 (100.030): the rethrow-only catch around this admission
            // block is removed; admission failures propagate directly.
            var agentAssistant = RequiredString(command, "assistant");
            if (agentAssistant is not ("local" or "muse" or "codex") || _beginTask is null || _endTask is null ||
                !TryId(command, "id", out var turn))
                throw new InvalidOperationException("Agent mode is unavailable for this route.");
            var task = Guid.NewGuid();
            bool Admit()
            {
                return _beginTask(task, turn, agentAssistant, RequiredString(command, "model"), RequiredString(command, "account"), RequiredString(command, "effort"));
            }
            if (!(admissionGuard is null ? Admit() : admissionGuard(Admit)))
            {
                // A reconnect can resend a turn already admitted and
                // recorded. Return that exact existing result; never
                // create another task or replay uncertain work.
                var existing = await CallAsync(new { op = "get", id = turn.ToString("D") });
                if (existing.ValueKind == JsonValueKind.Object &&
                    existing.TryGetProperty("agent_task_id", out var priorTask) && priorTask.ValueKind == JsonValueKind.String &&
                    new[] { "text", "assistant", "model", "account", "effort" }.All(key =>
                        existing.TryGetProperty(key, out var prior) && prior.ValueKind == JsonValueKind.String &&
                        prior.GetString() == RequiredString(command, key)) &&
                    (admissionGuard is null || admissionGuard(() => true)))
                    return existing;
                throw new InvalidOperationException("The computer task could not be admitted. Check the local grant and phone connection; an existing turn is never replayed.");
            }
            taskId = task; taskTurn = turn;
            message["agent_task_id"] = task.ToString("D");
        }
        message["rpc"] = id;
        var waiting = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = waiting;
        try
        {
            if (admissionGuard is not null && command.TryGetProperty("op", out var waitingOp) &&
                waitingOp.ValueKind == JsonValueKind.String && waitingOp.GetString() is "stop_chat" or "restart_chat" or "resume_chat_local")
                PhoneControlWaitingForFixture?.Invoke();
            await _write.WaitAsync();
            try
            {
                var payload = message.ToJsonString();
                if (admissionGuard is not null && command.TryGetProperty("op", out var controlOp) &&
                    controlOp.ValueKind == JsonValueKind.String && controlOp.GetString() is "stop_chat" or "restart_chat" or "resume_chat_local")
                {
                    // Linearize acceptance under the session lock only after
                    // this request reaches the head of the serial writer.
                    // The immutable payload is admitted here; the pipe write
                    // remains asynchronous and holds no host monitor.
                    string? admitted = null;
                    if (!admissionGuard(() => { admitted = payload; return true; }))
                    {
                        PhoneControlDeniedForFixture?.Invoke();
                        throw new InvalidOperationException("Phone session is no longer active.");
                    }
                    payload = admitted!;
                }
                await process.StandardInput.WriteLineAsync(payload);
                await process.StandardInput.FlushAsync();
            }
            finally { _write.Release(); }
            var result = await waiting.Task.WaitAsync(_requestTimeout);
            return command.TryGetProperty("op", out var readOperation)
                && readOperation.ValueKind == JsonValueKind.String && readOperation.GetString() == "history"
                ? AddJettReplies(result) : result;
        }
        catch (InvalidOperationException)
        {
            if (taskId is { } task && taskTurn is { } turn) EndAdmittedTask(task, turn, "failed");
            throw;
        }
        catch (Exception ex) when (ex is IOException or TimeoutException or OperationCanceledException or ObjectDisposedException)
        {
            if (taskId is { } task && taskTurn is { } turn) EndAdmittedTask(task, turn, "failed");
            throw;
        }
        finally { _pending.TryRemove(id, out _); }
    }



    private async Task WarmStartAsync()
    {
        try { await CallAsync(new { op = "warm_start" }); }
        catch (Exception) { /* Best-effort only; a cold first turn is the old behaviour. */ }
    }

    private async Task Poll()
    {
        if (Interlocked.Exchange(ref _polling, 1) != 0) return;
        try
        {
            // Batch 2: poll the runtime event feed first, keeping the `since`
            // cursor so each poll only returns new events. The feed is
            // best-effort: an older service that does not know the op falls
            // back to history-only polling below, and the loop never fails.
            // History rows carry `partial` / `partial_seq` for dispatching
            // turns, so history is still fetched every poll: the stream
            // carries event summaries, not partial text.
            try
            {
                var stream = await CallAsync(new { op = "runtime_stream", since = _lastSeq, limit = 100 });
                if (stream.TryGetProperty("latest_seq", out var latest) && latest.TryGetInt64(out var seq)) _lastSeq = seq;
                RuntimeStreamChanged?.Invoke(stream.Clone());
                if (stream.TryGetProperty("resynced", out var resynced) && resynced.ValueKind == JsonValueKind.True)
                {
                    // The window slid past the cursor: re-fetch history once
                    // so the panel cannot miss turns the stream skipped.
                    var resyncHistory = await CallAsync(new { op = "history" });
                    var resyncDecorated = AddJettReplies(resyncHistory);
                    var resyncText = resyncDecorated.GetRawText();
                    if (resyncText != _lastHistory) { _lastHistory = resyncText; HistoryChanged?.Invoke(resyncDecorated); }
                    return;
                }
            }
            catch (Exception) { /* Older service / transient failure: history fallback below. */ }
            var history = await CallAsync(new { op = "history" });
            var decorated = AddJettReplies(history);
            var text = decorated.GetRawText();
            if (text != _lastHistory) { _lastHistory = text; HistoryChanged?.Invoke(decorated); }
        }
        catch (Exception) { /* Explicit requests report service errors in each bar. */ }
        finally { Interlocked.Exchange(ref _polling, 0); }
    }

    private JsonElement AddJettReplies(JsonElement history)
    {
        var node = JsonNode.Parse(history.GetRawText())?.AsObject() ?? throw new JsonException("History response must be an object.");
        node["jettReplies"] = JsonSerializer.SerializeToNode(_jettJournal?.ReadJettReplies() ?? []);
        node["jettActions"] = JsonSerializer.SerializeToNode(_jettJournal?.ReadJettActions() ?? []);
        node["assistantActions"] = JsonSerializer.SerializeToNode(_jettJournal?.ReadAssistantHistory() ?? []);
        using var document = JsonDocument.Parse(node.ToJsonString());
        return document.RootElement.Clone();
    }

    private static string RequiredString(JsonElement command, string name) =>
        command.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()! : throw new InvalidOperationException($"Missing assistant route field '{name}'.");

    private static bool IsUuidV4(Guid id, string value) =>
        id != Guid.Empty && id.ToString("D") == value && value.Length == 36 && value[14] == '4'
        && value[19] is '8' or '9' or 'a' or 'b';

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0) return;
        _poll.Dispose();
        lock (_gate)
        {
            if (_process is { } process)
            {
                if (!process.HasExited)
                {
                    try { process.StandardInput.Close(); }
                    catch (IOException error) { _diagnostic = "Could not request graceful assistant shutdown: " + error.Message; }

                    if (!process.WaitForExit(TimeSpan.FromSeconds(45)))
                    {
                        _diagnostic = "Assistant shutdown timed out; terminating its process tree.";
                        try
                        {
                            process.Kill(true);
                            process.WaitForExit(TimeSpan.FromSeconds(5));
                        }
                        catch (InvalidOperationException) { }
                    }
                }
                process.Dispose();
            }
        }
        FailPending(new ObjectDisposedException(nameof(AssistantClient)));
    }
}
