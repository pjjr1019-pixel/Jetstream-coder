using System.Text.Json;
using Deskhand.Contracts;

namespace Deskhand.Windows.App;

internal sealed record AssistantActionResult(Guid ActionId, string Status, string? Error, object? Data);
internal sealed record AssistantTaskStatus(Guid TaskId, Guid TurnId, string Assistant, string Model, bool Active, Guid[] RunningActions);

/// <summary>
/// Host-owned action authority. This first subset is not exposed to models until
/// the browser binding, approval UI and remaining action families are complete.
/// </summary>
internal sealed class AssistantActionEngine : IDisposable
{
    private readonly object _authority = new();
    private readonly JettJournal _journal;
    private readonly Func<DesktopBrowser> _browserFactory;
    private readonly Func<IAssistantWindowNative> _windowFactory;
    private readonly Func<IntPtr>? _foregroundForFixture;
    private readonly IAssistantPointerNative _pointer;
    private readonly Dictionary<Guid, (Guid Turn, DesktopBrowser Browser, long Generation)> _tasks = [];
    private readonly Dictionary<Guid, (string Assistant, string Model, string Account)> _routes = [];
    internal Func<string, string, bool>? RouteAuthorized { get; set; }
    internal bool AutomaticOrdinaryActions { get; set; }
    private readonly Dictionary<Guid, CancellationTokenSource> _taskCancellation = [];
    private readonly Dictionary<Guid, AssistantWindowSession> _windows = [];
    private readonly Dictionary<Guid, AssistantAccessibilitySession> _accessibility = [];
    private readonly Dictionary<Guid, AssistantScreenSession> _screens = [];
    private readonly Dictionary<Guid, Guid> _running = [];
    private readonly HashSet<Guid> _uncertainTasks = [];
    // Batch 2 (100.030, D10): per-task input budget state (see
    // ConsumeInputBudget). Created in Begin, removed in End, cleared in
    // Stop/Dispose alongside the other per-task session state.
    private readonly Dictionary<Guid, InputBudget> _inputBudgets = [];
    private static readonly HashSet<string> ObservationActions = new(StringComparer.Ordinal)
        { "window.list", "window.observe", "window.inspect", "window.screenshot", "window.wait_for", "desktop.list", "filesystem.roots", "filesystem.next", "filesystem.read", "filesystem.read_chunk" };
    private readonly SemaphoreSlim _serial = new(1, 1);
    internal AssistantApprovals Approvals { get; } = new();
    private bool _stopped = true, _disposed;
    private long _generation;
    private DateTimeOffset _expiry;

    internal AssistantActionEngine(JettJournal journal, Func<DesktopBrowser> browserFactory,
        Func<IAssistantWindowNative>? windowFactory = null, Func<IntPtr>? foregroundForFixture = null, IAssistantPointerNative? pointer = null)
    { _journal = journal; _browserFactory = browserFactory; _windowFactory = windowFactory ?? (() => new AssistantWindowNative()); _foregroundForFixture = foregroundForFixture; _pointer = pointer ?? new AssistantPointerNative(); }

    // Called only by a local Windows control; no remote or model entry point.
    internal void GrantLocally(DateTimeOffset expires)
    {
        lock (_authority)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (expires <= DateTimeOffset.UtcNow || (expires != DateTimeOffset.MaxValue && expires > DateTimeOffset.UtcNow.AddMinutes(30).AddSeconds(2))) throw new ArgumentOutOfRangeException(nameof(expires));
            // A fresh grant must not leave old tasks active or resurrectable.
            Stop();
            _generation++; _expiry = expires; _stopped = false;
        }
    }

    internal void Stop()
    {
        lock (_authority)
        {
            _stopped = true; _generation++;
            foreach (var cancellation in _taskCancellation.Values) cancellation.Cancel();
            _taskCancellation.Clear();
            Approvals.CancelAll();
            // Batch 2 (100.030, G3): a STOP latch must not leave a key
            // held down. Best-effort and never throws (see
            // InputInjector.ReleaseAllHeldKeys).
            InputInjector.ReleaseAllHeldKeys();
            foreach (var task in _tasks)
            {
                task.Value.Browser.End();
                // STOP must still revoke in memory if the journal is unavailable.
                try { _journal.FinishAssistantTask(task.Key, "cancelled"); }
                catch (Exception ex) when (ex is InvalidOperationException or IOException) { }
            }
            _tasks.Clear();
            _routes.Clear();
            
            _uncertainTasks.Clear();
            _inputBudgets.Clear();
            _windows.Clear();
            foreach (var session in _accessibility.Values) session.Clear();
            _accessibility.Clear();
            foreach (var session in _screens.Values) session.Clear();
            _screens.Clear();
        }
    }

    internal bool End(Guid taskId, Guid turnId, string outcome)
    {
        lock (_authority)
        {
            if (!_tasks.TryGetValue(taskId, out var task) || task.Turn != turnId) return false;
            if (!_journal.FinishAssistantTask(taskId, outcome)) return false;
            _tasks.Remove(taskId); _routes.Remove(taskId);  _windows.Remove(taskId); _uncertainTasks.Remove(taskId); _inputBudgets.Remove(taskId);
            if (_accessibility.Remove(taskId, out var accessibility)) accessibility.Clear();
            if (_screens.Remove(taskId, out var screen)) screen.Clear();
            if (_taskCancellation.Remove(taskId, out var cancellation)) cancellation.Cancel();
            Approvals.CancelTask(taskId); task.Browser.End();
            // Batch 2 (100.030, G3): task end releases any key this task
            // left held (unpaired key_down). Best-effort, never throws.
            InputInjector.ReleaseAllHeldKeys();
            return true;
        }
    }

    // Task authority is owned by this host.
    // It is independent of the local-model grant and fails closed in memory
    // even if persistent history is unavailable.


    internal bool Active(Guid taskId, Guid turnId)
    {
        lock (_authority)
            return _tasks.TryGetValue(taskId, out var task) && task.Turn == turnId && TaskAuthorized(taskId, task.Generation);
    }

    // Informational snapshot only. Execution independently rechecks authority
    // under the same host lock, including after the approval wait.




    internal Guid[] RunningActions()
    {
        lock (_authority) return _running.Keys.ToArray();
    }

    internal bool HasActiveGrant()
    {
        lock (_authority) return Authorized(_generation);
    }

    internal AssistantTaskStatus[] Tasks()
    {
        lock (_authority)
            return _tasks.Select(item => new AssistantTaskStatus(item.Key, item.Value.Turn, _routes[item.Key].Assistant,
                _routes[item.Key].Model, TaskAuthorized(item.Key, item.Value.Generation), _running.Where(action => action.Value == item.Key).Select(action => action.Key).ToArray())).ToArray();
    }

    internal bool Begin(Guid taskId, Guid turnId, string assistant, string model, string account, string effort)
    {
        lock (_authority)
        {
            if (assistant is not ("local" or "muse" or "codex") || (assistant != "local" && RouteAuthorized is null) || RouteAuthorized?.Invoke(assistant, account) == false || !Authorized(_generation) || _tasks.ContainsKey(taskId)) return false;
            var browser = _browserFactory();
            AssistantWindowSession windows;
            try
            {
                windows = new AssistantWindowSession(_windowFactory());
                if (!_journal.ReserveAssistantTask(taskId, turnId, assistant, model, account, effort)) { browser.End(); return false; }
            }
            catch { browser.End(); throw; }
            _tasks.Add(taskId, (turnId, browser, _generation));
            _routes.Add(taskId, (assistant, model, account));
            _taskCancellation.Add(taskId, new CancellationTokenSource());
            _windows.Add(taskId, windows);
            _accessibility.Add(taskId, new AssistantAccessibilitySession());
            _screens.Add(taskId, new AssistantScreenSession());
            _inputBudgets.Add(taskId, new InputBudget());
            return true;
        }
    }

    // The caller owns an already restricted disposable-folder browser. This
    // grant cannot add permissions to the underlying local grant or be renewed.


    internal Task<AssistantActionResult> ExecuteAsync(Guid taskId, Guid turnId, Guid actionId, string name, string argumentsJson) =>
        ExecuteBoundAsync(taskId, turnId, actionId, name, argumentsJson);


    private async Task<AssistantActionResult> ExecuteBoundAsync(Guid taskId, Guid turnId, Guid actionId, string name, string argumentsJson)
    {
        // Batch 2 (100.030): the global lock is held for mutations only.
        // Observations are read-only for other tasks (per-task session state
        // is touched only by that task's own actions, which the Python
        // adapter issues one at a time), so an approval wait or a 30 s
        // command in one task no longer blocks window.list / filesystem.read
        // in every other task. Mutations still serialize globally.
        var isObservation = ObservationActions.Contains(name);
        if (!isObservation) await _serial.WaitAsync();
        try
        {
            lock (_authority)
                if (_uncertainTasks.Contains(taskId) && !ObservationActions.Contains(name))
                    return new(actionId, "rejected", "previous_action_uncertain", null);
            AssistantActionResult result;
            try { result = await ExecuteBoundCoreAsync(taskId, turnId, actionId, name, argumentsJson); }
            catch
            {
                // Batch 2 (100.030, G3): any error releases held keys.
                InputInjector.ReleaseAllHeldKeys();
                lock (_authority) if (_tasks.ContainsKey(taskId)) _uncertainTasks.Add(taskId);
                throw;
            }
            if (result.Status == "uncertain")
                lock (_authority) if (_tasks.ContainsKey(taskId)) _uncertainTasks.Add(taskId);
            return result;
        }
        finally { if (!isObservation) _serial.Release(); }
    }

    private async Task<AssistantActionResult> ExecuteBoundCoreAsync(Guid taskId, Guid turnId, Guid actionId, string name, string argumentsJson)
    {
        try
        {
            (Guid Turn, DesktopBrowser Browser, long Generation) task;
            lock (_authority)
            {
                if (!_tasks.TryGetValue(taskId, out task) || task.Turn != turnId || !TaskAuthorized(taskId, task.Generation))
                    return new(actionId, "rejected", "authorization_required", null);
                if (name is not ("desktop.list" or "desktop.open" or "filesystem.roots" or "filesystem.next" or "filesystem.read" or "filesystem.read_chunk" or "filesystem.create" or "filesystem.mkdir" or "filesystem.rmdir" or "filesystem.launch" or "filesystem.delete" or "filesystem.replace" or "filesystem.copy" or "filesystem.move" or "filesystem.move_folder" or "command.run" or "window.list" or "window.observe" or "window.activate" or "window.close" or "window.inspect" or "window.screenshot" or "window.wait_for" or "screen.click" or "screen.double_click" or "screen.move" or "screen.drag" or "screen.scroll" or "screen.right_click" or "control.invoke" or "control.type" or "control.key" or "control.key_down" or "control.key_up")) return new(actionId, "rejected", "unsupported_action", null);
                if (!_journal.ReserveAssistantAction(taskId, actionId, name, name is "filesystem.create" or "filesystem.replace" or "command.run" or "control.type" ? "{\"content\":\"[redacted]\"}" : argumentsJson)) return new(actionId, "rejected", "duplicate_action", null);
                if (!_journal.TransitionAssistantAction(actionId, "reserved", "running")) return new(actionId, "rejected", "journal_failure", null);
                _running.Add(actionId, taskId);
            }
            try
            {
                using var json = JsonDocument.Parse(argumentsJson, new JsonDocumentOptions { MaxDepth = 3 });
                var args = json.RootElement;
                if (args.ValueKind != JsonValueKind.Object) throw new JsonException();
                var props = args.EnumerateObject().ToArray();
                bool Allowed()
                {
                    lock (_authority)
                        return TaskAuthorized(taskId, task.Generation) && _tasks.TryGetValue(taskId, out var current) &&
                            current.Turn == turnId && current.Generation == task.Generation && ReferenceEquals(current.Browser, task.Browser);
                }
                object result;
                string? error;
                var uncertain = false;
                if (name == "window.list")
                {
                    if (props.Length != 0) throw new JsonException();
                    // Inventory shares the protected-window inspection used by
                    // observations; do not disclose credential-window titles.
                    var snapshots = TextWindowActivator.Snapshot().Where(AssistantWindowPolicy.Eligible).Take(50).ToArray();
                    var safe = new List<TextWindowActivator.Window>();
                    var deadline = System.Diagnostics.Stopwatch.StartNew();
                    foreach (var window in snapshots)
                    {
                        if (!Allowed() || deadline.Elapsed > TimeSpan.FromSeconds(8)) break;
                        if ((await AssistantAccessibility.InspectAsync(window, Allowed)).Error is null) safe.Add(window);
                    }
                    result = AssistantWindows.Summarize(safe); error = Allowed() ? null : "authorization_revoked";
                }
                else if (name == "window.observe")
                {
                    if (props.Length != 0) throw new JsonException();
                    AssistantWindowSession? session; AssistantObservedWindow[] candidates;
                    lock (_authority)
                    {
                        if (_accessibility.TryGetValue(taskId, out var oldControls)) oldControls.Clear();
                        if (_screens.TryGetValue(taskId, out var oldScreen)) oldScreen.Clear();
                        candidates = Allowed() && _windows.TryGetValue(taskId, out session) ? session.Observe() : [];
                        _windows.TryGetValue(taskId, out session);
                    }
                    var safe = new List<AssistantObservedWindow>();
                    var deadline = System.Diagnostics.Stopwatch.StartNew();
                    foreach (var candidate in candidates)
                    {
                        if (!Allowed() || session is null) break;
                        if (deadline.Elapsed > TimeSpan.FromSeconds(8)) { session.Forget(candidate.Token); continue; }
                        var window = session.Peek(candidate.Token);
                        if (window is not null && (await AssistantAccessibility.InspectAsync(window, Allowed)).Error is null) safe.Add(candidate);
                        else session.Forget(candidate.Token);
                    }
                    result = safe.ToArray(); error = Allowed() && session is not null ? null : "authorization_revoked";
                }
                else if (name == "window.activate")
                {
                    if (props.Length != 1 || props[0].Name != "window_token" || props[0].Value.ValueKind != JsonValueKind.String) throw new JsonException();
                    TextWindowActivator.Window? window; AssistantWindowSession? session;
                    lock (_authority)
                        window = _windows.TryGetValue(taskId, out session) ? session.Take(props[0].Value.GetString()!, Allowed) : null;
                    if (window is null || session is null) error = "stale_or_denied";
                    else
                    {
                        var inspection = await AssistantAccessibility.InspectAsync(window, Allowed);
                        if (inspection.Error is not null) error = inspection.Error;
                        else lock (_authority) error = session.Activate(window, Allowed);
                    }
                    result = new { error };
                }
                else if (name == "window.close")
                {
                    if (props.Length != 2 || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 2 ||
                        !args.TryGetProperty("window_token", out var token) || token.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("purpose", out var purpose) || purpose.ValueKind != JsonValueKind.String ||
                        string.IsNullOrWhiteSpace(purpose.GetString()) || purpose.GetString()!.Length > 500 || purpose.GetString()!.Any(char.IsControl)) throw new JsonException();
                    TextWindowActivator.Window? window; AssistantWindowSession? session; Task<bool>? decision = null;
                    lock (_authority)
                    {
                        window = _windows.TryGetValue(taskId, out session) ? session.Take(token.GetString()!, Allowed) : null;
                        if (window is not null)
                            decision = AskActionAsync(new(Guid.NewGuid(), taskId, actionId, "Close window", window.ProcessName + " — " + window.Title, purpose.GetString()!, DateTimeOffset.UtcNow.AddMinutes(2)), CancellationToken.None);
                    }
                    if (window is null || session is null || decision is null) { result = new { error = "stale_or_denied" }; error = "stale_or_denied"; }
                    else if (!await decision || !Allowed())
                    {
                        var cancelled = _journal.TransitionAssistantAction(actionId, "running", "cancelled", "approval_denied_or_expired");
                        return cancelled ? new(actionId, "cancelled", "approval_denied_or_expired", null) : new(actionId, "uncertain", "journal_failure", null);
                    }
                    else
                    {
                        var inspection = await AssistantAccessibility.InspectAsync(window, Allowed);
                        if (inspection.Error is not null) error = inspection.Error;
                        else lock (_authority) error = session.Close(window, Allowed);
                        result = new { error };
                        // Batch 2 (100.030, H6): a close is completed only when
                        // the window is verified gone (see AssistantWindowSession.
                        // Close). A close that leaves the window open stays
                        // uncertain via "close_unconfirmed" instead of poisoning
                        // the task for a close that provably happened.
                        uncertain = error == "close_unconfirmed";
                    }
                }
                else if (name is "window.inspect" or "window.screenshot")
                {
                    if (props.Length != 1 || props[0].Name != "window_token" || props[0].Value.ValueKind != JsonValueKind.String) throw new JsonException();
                    TextWindowActivator.Window? window;
                    lock (_authority)
                    {
                        if (name == "window.screenshot" && _screens.TryGetValue(taskId, out var previousScreen)) previousScreen.Clear();
                        window = _windows.TryGetValue(taskId, out var session) ? session.Take(props[0].Value.GetString()!, Allowed) : null;
                    }
                    if (window is null) { result = new { error = "stale_or_denied" }; error = "stale_or_denied"; }
                    else
                    {
                        if (name == "window.inspect")
                        {
                            AssistantAccessibilitySession? accessible;
                            lock (_authority) _accessibility.TryGetValue(taskId, out accessible);
                            var inspection = await AssistantAccessibility.InspectAsync(window, Allowed, accessible);
                            result = inspection; error = inspection.Error;
                        }
                        else
                        {
                            var screenshot = await AssistantScreenshot.CaptureAsync(window, Allowed);
                            lock (_authority)
                                if (screenshot.Error is null && screenshot.JpegBase64 is not null && Allowed() && _screens.TryGetValue(taskId, out var screen))
                                    screenshot = screenshot with { ObservationToken = screen.Remember(window, screenshot) };
                            result = screenshot; error = screenshot.Error;
                        }
                    }
                }
                else if (name == "window.wait_for")
                {
                    // Batch 2 (100.030, G9): read-only host-side poll, bounded
                    // 30 s. The AI must not busy-poll in chat; the host polls.
                    // Exactly one selector: window_token (<=64) / text (<=500)
                    // / control_name (<=200). Evidence kind: read — the payload
                    // (what was found) is the evidence. Timeout is failed
                    // with the timeout stated, not uncertain. No approval
                    // prompt: a wait observes, it never acts.
                    var hasWindowToken = args.TryGetProperty("window_token", out var waitToken);
                    var hasText = args.TryGetProperty("text", out var waitText);
                    var hasControlName = args.TryGetProperty("control_name", out var waitControlName);
                    var selectorCount = (hasWindowToken ? 1 : 0) + (hasText ? 1 : 0) + (hasControlName ? 1 : 0);
                    if (props.Length != 2 || selectorCount != 1 || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 2 ||
                        !args.TryGetProperty("timeout_secs", out var timeoutValue) || timeoutValue.ValueKind != JsonValueKind.Number || !timeoutValue.TryGetInt32(out var timeoutSecs) || timeoutSecs is < 1 or > 30 ||
                        (hasWindowToken && (waitToken.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(waitToken.GetString()) || waitToken.GetString()!.Length > 64)) ||
                        (hasText && (waitText.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(waitText.GetString()) || waitText.GetString()!.Length > 500 || waitText.GetString()!.Any(char.IsControl))) ||
                        (hasControlName && (waitControlName.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(waitControlName.GetString()) || waitControlName.GetString()!.Length > 200 || waitControlName.GetString()!.Any(char.IsControl)))) throw new JsonException();
                    AssistantWindowSession? waitSession;
                    lock (_authority) _windows.TryGetValue(taskId, out waitSession);
                    object? found = null;
                    var waitDeadline = DateTimeOffset.UtcNow.AddSeconds(timeoutSecs);
                    // Poll rounds, 250 ms apart. window_token is a Peek (never
                    // consumes the observation); text matches a window title;
                    // control_name inspects eligible windows without touching
                    // the task's accessibility session (no session argument,
                    // as in window.list). Each control round is bounded to 2 s
                    // so one slow provider cannot eat the whole timeout.
                    while (found is null && Allowed() && DateTimeOffset.UtcNow < waitDeadline)
                    {
                        if (hasWindowToken)
                        {
                            TextWindowActivator.Window? window;
                            lock (_authority) window = waitSession is null ? null : waitSession.Peek(waitToken.GetString()!);
                            if (window is not null) found = new { found = true, windowToken = waitToken.GetString(), processName = window.ProcessName, title = window.Title };
                        }
                        else if (hasText)
                        {
                            var text = waitText.GetString()!;
                            var match = TextWindowActivator.Snapshot().Where(AssistantWindowPolicy.Eligible)
                                .FirstOrDefault(w => w.Title.Contains(text, StringComparison.OrdinalIgnoreCase));
                            if (match is not null) found = new { found = true, text, processName = match.ProcessName, title = match.Title };
                        }
                        else
                        {
                            var controlName = waitControlName.GetString()!;
                            var roundDeadline = DateTimeOffset.UtcNow.AddSeconds(2);
                            foreach (var window in TextWindowActivator.Snapshot().Where(AssistantWindowPolicy.Eligible).Take(50))
                            {
                                if (!Allowed() || DateTimeOffset.UtcNow >= roundDeadline) break;
                                var inspection = await AssistantAccessibility.InspectAsync(window, Allowed);
                                if (inspection.Error is null && inspection.Items.Any(item => string.Equals(item.Name, controlName, StringComparison.OrdinalIgnoreCase)))
                                { found = new { found = true, controlName, processName = window.ProcessName, title = window.Title }; break; }
                            }
                        }
                        if (found is null && Allowed() && DateTimeOffset.UtcNow < waitDeadline) await Task.Delay(250);
                    }
                    if (found is null)
                    {
                        error = Allowed() ? "wait_for_timeout" : "authorization_revoked";
                        result = new { error, timeoutSecs };
                    }
                    else { result = found; error = null; }
                }
                else if (name is "screen.click" or "screen.double_click" or "screen.move" or "screen.drag" or "screen.scroll" or "screen.right_click")
                {
                    var scrolling = name == "screen.scroll";
                    var dragging = name == "screen.drag";
                    var moving = name == "screen.move";
                    var expected = dragging ? 6 : scrolling ? 5 : 4;
                    var steps = 0;
                    double toX = 0, toY = 0;
                    if (props.Length != expected || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != expected ||
                        !args.TryGetProperty("screenshot_token", out var token) || token.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("x", out var xValue) || xValue.ValueKind != JsonValueKind.Number || !xValue.TryGetDouble(out var x) || !double.IsFinite(x) || x < 0 || x > 1 ||
                        !args.TryGetProperty("y", out var yValue) || yValue.ValueKind != JsonValueKind.Number || !yValue.TryGetDouble(out var y) || !double.IsFinite(y) || y < 0 || y > 1 ||
                        !args.TryGetProperty("purpose", out var purpose) || purpose.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(purpose.GetString()) ||
                        purpose.GetString()!.Length > 500 || purpose.GetString()!.Any(char.IsControl) ||
                        (dragging && (!args.TryGetProperty("to_x", out var toXValue) || toXValue.ValueKind != JsonValueKind.Number || !toXValue.TryGetDouble(out toX) || !double.IsFinite(toX) || toX < 0 || toX > 1 ||
                            !args.TryGetProperty("to_y", out var toYValue) || toYValue.ValueKind != JsonValueKind.Number || !toYValue.TryGetDouble(out toY) || !double.IsFinite(toY) || toY < 0 || toY > 1)) ||
                        (scrolling && (!args.TryGetProperty("steps", out var stepValue) || stepValue.ValueKind != JsonValueKind.Number || !stepValue.TryGetInt32(out steps) || steps is < -3 or > 3 or 0))) throw new JsonException();
                    AssistantScreenObservation? observation; Task<bool>? decision = null; string? budgetError = null;
                    lock (_authority)
                    {
                        observation = Allowed() && _screens.TryGetValue(taskId, out var session) ? session.Take(token.GetString()!) : null;
                        if (observation is not null)
                        {
                            // Batch 2 (100.030, D10): the budget is checked
                            // after the token is taken (only an op that is
                            // actually going to execute consumes budget) and
                            // before the approval prompt, so a capped op
                            // never consumes a phone approval. A capped op
                            // spends the observation token; the AI re-observes
                            // (window.screenshot is free and uncounted).
                            budgetError = ConsumeInputBudget(taskId, name);
                            if (budgetError is null)
                            {
                                var target = $"{observation.Window.ProcessName} — {observation.Window.Title}\nPosition: {x:P1} from left, {y:P1} from top";
                                if (scrolling) target += $"\nScroll: {(steps > 0 ? "up" : "down")} {Math.Abs(steps)} step(s)";
                                if (dragging) target += $"\nDestination: {toX:P1} from left, {toY:P1} from top";
                                decision = AskActionAsync(new(Guid.NewGuid(), taskId, actionId, scrolling ? "Scroll window" : dragging ? "Drag in window" : moving ? "Move pointer" : name == "screen.double_click" ? "Double-click window" : name == "screen.right_click" ? "Right-click window" : "Click window", target, purpose.GetString()!, DateTimeOffset.UtcNow.AddMinutes(2)), CancellationToken.None);
                            }
                        }
                    }
                    if (budgetError is not null)
                    {
                        // The journal has no "blocked" transition (running ->
                        // completed/failed/uncertain/cancelled only), so the
                        // capped op is journalled as failed with the same
                        // error while the returned result carries "blocked".
                        var recordedBudget = _journal.TransitionAssistantAction(actionId, "running", "failed", budgetError);
                        return recordedBudget ? new(actionId, "blocked", budgetError, null) : new(actionId, "uncertain", "journal_failure", null);
                    }
                    if (observation is null || decision is null) { result = new { error = "stale_or_denied" }; error = "stale_or_denied"; }
                    else if (!await decision || !Allowed())
                    {
                        var cancelled = _journal.TransitionAssistantAction(actionId, "running", "cancelled", "approval_denied_or_expired");
                        return cancelled ? new(actionId, "cancelled", "approval_denied_or_expired", null) : new(actionId, "uncertain", "journal_failure", null);
                    }
                    else
                    {
                        var click = scrolling ? await AssistantPointer.ScrollAsync(observation, x, y, steps, Allowed, _pointer) : dragging || moving ? await AssistantPointer.GestureAsync(observation, x, y, dragging ? toX : x, dragging ? toY : y, dragging, Allowed, _pointer) : name == "screen.double_click" ? await AssistantPointer.DoubleClickAsync(observation, x, y, Allowed, _pointer) : name == "screen.right_click" ? await AssistantPointer.RightClickAsync(observation, x, y, Allowed, _pointer) : await AssistantPointer.ClickAsync(observation, x, y, Allowed, _pointer);
                        var clickStatus = click.Status == "rejected" ? "failed" : click.Status;
                        var saved = _journal.TransitionAssistantAction(actionId, "running", clickStatus, click.Error, JsonSerializer.Serialize(new { click.Error }));
                        return saved ? new(actionId, clickStatus, click.Error, click) : new(actionId, "uncertain", "journal_failure", null);
                    }
                }
                else if (name is "control.invoke" or "control.type" or "control.key")
                {
                    var expected = name is "control.type" or "control.key" ? 3 : 2;
                    if (props.Length != expected || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != expected ||
                        !args.TryGetProperty("control_token", out var control) || control.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("purpose", out var purpose) || purpose.ValueKind != JsonValueKind.String ||
                        string.IsNullOrWhiteSpace(purpose.GetString()) || purpose.GetString()!.Length > 500 || purpose.GetString()!.Any(char.IsControl)) throw new JsonException();
                    string? input = null;
                    if (name == "control.type")
                    {
                        if (!args.TryGetProperty("text", out var text) || text.ValueKind != JsonValueKind.String || text.GetString()!.Length > 512 ||
                            text.GetString()!.Any(c => char.IsControl(c) && c is not ('\r' or '\n' or '\t'))) throw new JsonException();
                        input = text.GetString();
                    }
                    else if (name == "control.key")
                    {
                        if (!args.TryGetProperty("key", out var key) || key.ValueKind != JsonValueKind.String || !InputInjector.IsSupportedKey(key.GetString()!)) throw new JsonException();
                        input = key.GetString();
                    }
                    AssistantAccessibleCandidate? candidate;
                    Task<bool>? decision = null; string? budgetError = null;
                    lock (_authority)
                    {
                        candidate = Allowed() && _accessibility.TryGetValue(taskId, out var session) ? session.Take(control.GetString()!) : null;
                        if (candidate is not null)
                        {
                            // Batch 2 (100.030, D10): only control.key is a
                            // key op for the budget; control.type (text set)
                            // and control.invoke are not pointer/key ops.
                            // Checked after the token is taken and before the
                            // approval prompt, as in the screen.* branch.
                            if (name == "control.key") budgetError = ConsumeInputBudget(taskId, name);
                            if (budgetError is null)
                            {
                                var target = $"{candidate.Window.ProcessName} — {candidate.Window.Title}\nControl: {(string.IsNullOrEmpty(candidate.Name) ? candidate.Role : candidate.Name)}";
                                if (input is not null) target += name == "control.key" ? "\n\nKey: " + input : "\n\nReplacement text:\n" + input;
                                var operation = name == "control.type" ? "Set control text" : name == "control.key" ? "Press key" : "Invoke control";
                                decision = AskActionAsync(new(Guid.NewGuid(), taskId, actionId, operation, target, purpose.GetString()!, DateTimeOffset.UtcNow.AddMinutes(2)), CancellationToken.None);
                            }
                        }
                    }
                    if (budgetError is not null)
                    {
                        var recordedBudget = _journal.TransitionAssistantAction(actionId, "running", "failed", budgetError);
                        return recordedBudget ? new(actionId, "blocked", budgetError, null) : new(actionId, "uncertain", "journal_failure", null);
                    }
                    if (candidate is null || decision is null) { result = new { error = "stale_or_denied" }; error = "stale_or_denied"; }
                    else if (!await decision || !Allowed())
                    {
                        var cancelled = _journal.TransitionAssistantAction(actionId, "running", "cancelled", "approval_denied_or_expired");
                        return cancelled ? new(actionId, "cancelled", "approval_denied_or_expired", null) : new(actionId, "uncertain", "journal_failure", null);
                    }
                    else
                    {
                        var action = await AssistantAccessibility.PerformAsync(candidate, name == "control.type" ? "type" : name == "control.key" ? "key" : "invoke", input, Allowed, _foregroundForFixture);
                        var actionStatus = action.Status == "rejected" ? "failed" : action.Status;
                        var saved = _journal.TransitionAssistantAction(actionId, "running", actionStatus, action.Error, JsonSerializer.Serialize(new { action.Error }));
                        return saved ? new(actionId, actionStatus, action.Error, action) : new(actionId, "uncertain", "journal_failure", null);
                    }
                }
                else if (name is "control.key_down" or "control.key_up")
                {
                    // Batch 2 (100.030, G3): a hold acts on the focused window,
                    // like the screen ops — so it takes a screenshot_token,
                    // NOT a control_token. One key, from the same allowlist
                    // as control.key (InputInjector.IsSupportedKey).
                    if (props.Length != 3 || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 3 ||
                        !args.TryGetProperty("screenshot_token", out var token) || token.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("purpose", out var purpose) || purpose.ValueKind != JsonValueKind.String ||
                        string.IsNullOrWhiteSpace(purpose.GetString()) || purpose.GetString()!.Length > 500 || purpose.GetString()!.Any(char.IsControl) ||
                        !args.TryGetProperty("key", out var keyValue) || keyValue.ValueKind != JsonValueKind.String || !InputInjector.IsSupportedKey(keyValue.GetString()!)) throw new JsonException();
                    var key = keyValue.GetString()!;
                    AssistantScreenObservation? observation; Task<bool>? decision = null; string? budgetError = null;
                    lock (_authority)
                    {
                        observation = Allowed() && _screens.TryGetValue(taskId, out var session) ? session.Take(token.GetString()!) : null;
                        if (observation is not null)
                        {
                            // Batch 2 (100.030, D10): a hold/release is a key
                            // op; checked after the token is taken and before
                            // the approval prompt, as in the screen.* branch.
                            budgetError = ConsumeInputBudget(taskId, name);
                            if (budgetError is null)
                            {
                                var target = $"{observation.Window.ProcessName} — {observation.Window.Title}\n\nKey: {key}";
                                var operation = name == "control.key_down" ? "Hold key" : "Release key";
                                decision = AskActionAsync(new(Guid.NewGuid(), taskId, actionId, operation, target, purpose.GetString()!, DateTimeOffset.UtcNow.AddMinutes(2)), CancellationToken.None);
                            }
                        }
                    }
                    if (budgetError is not null)
                    {
                        var recordedBudget = _journal.TransitionAssistantAction(actionId, "running", "failed", budgetError);
                        return recordedBudget ? new(actionId, "blocked", budgetError, null) : new(actionId, "uncertain", "journal_failure", null);
                    }
                    if (observation is null || decision is null) { result = new { error = "stale_or_denied" }; error = "stale_or_denied"; }
                    else if (!await decision || !Allowed())
                    {
                        var cancelled = _journal.TransitionAssistantAction(actionId, "running", "cancelled", "approval_denied_or_expired");
                        return cancelled ? new(actionId, "cancelled", "approval_denied_or_expired", null) : new(actionId, "uncertain", "journal_failure", null);
                    }
                    else
                    {
                        // Batch 2 (100.030, G3): reconciled at the 100.030
                        // FIX pass — the assumed per-key async methods
                        // (observation, key, authorized) do not exist. AssistantAccessibility.PerformAsync takes
                        // an AssistantAccessibleCandidate (a control_token
                        // binding), which a screenshot_token hold does not
                        // have, so PerformAsync cannot be called from this
                        // observation-bound branch. The closest correct call
                        // is the dispatch PerformAsync itself delegates to
                        // for key_down/key_up: InputInjector.KeyDown/KeyUp
                        // (internal static void (string), same namespace;
                        // key already validated by IsSupportedKey above).
                        // GAP (flagged, not invented here): unlike the
                        // PerformAsync control path, this skips its
                        // focus/foreground re-verification — the key goes to
                        // whatever window holds focus at dispatch, gated
                        // only by Allowed() and the observation. The 15 s
                        // staleness bound below mirrors the screen ops; an
                        // observation-taking PerformAsync overload (or a key
                        // method on AssistantPointer) is still needed for a
                        // fully verified hold.
                        AssistantControlResult action;
                        if (!Allowed()) action = new("cancelled", "authorization_revoked");
                        else if (DateTimeOffset.UtcNow - observation.CapturedAt > TimeSpan.FromSeconds(15)) action = new("rejected", "stale_observation");
                        else
                        {
                            try
                            {
                                if (name == "control.key_down") InputInjector.KeyDown(key);
                                else InputInjector.KeyUp(key);
                                action = new("completed", null);
                            }
                            catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception)
                            {
                                // As in PerformAsync: a KeyDown that threw
                                // after a partial send leaves the outcome
                                // unconfirmed (its own partial-send release
                                // freed whatever went down); KeyUp never
                                // throws. Never a silent hold.
                                action = new("uncertain", "outcome_unconfirmed");
                            }
                        }
                        var actionStatus = action.Status == "rejected" ? "failed" : action.Status;
                        var saved = _journal.TransitionAssistantAction(actionId, "running", actionStatus, action.Error, JsonSerializer.Serialize(new { action.Error }));
                        return saved ? new(actionId, actionStatus, action.Error, action) : new(actionId, "uncertain", "journal_failure", null);
                    }
                }
                else if (name == "filesystem.roots")
                {
                    if (props.Length != 0) throw new JsonException();
                    var listed = task.Browser.List(new DesktopListRequest(actionId, null, ThisPc: true), Allowed);
                    result = listed; error = listed.Error;
                }
                else if (name == "desktop.list")
                {
                    if (props.Length != 1 || props[0].Name != "folder_token" || props[0].Value.ValueKind is not (JsonValueKind.Null or JsonValueKind.String)) throw new JsonException();
                    var listed = task.Browser.List(new DesktopListRequest(actionId, props[0].Value.GetString(), Paged: true), Allowed);
                    result = listed; error = listed.Error;
                }
                else if (name == "filesystem.next")
                {
                    if (props.Length != 2 || !args.TryGetProperty("folder_token", out var folder) || folder.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("cursor", out var cursor) || cursor.ValueKind != JsonValueKind.String ||
                        props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 2) throw new JsonException();
                    var listed = task.Browser.List(new DesktopListRequest(actionId, folder.GetString(), Cursor: cursor.GetString(), Paged: true), Allowed);
                    result = listed; error = listed.Error;
                }
                else if (name == "filesystem.read")
                {
                    if (props.Length != 1 || props[0].Name != "item_token" || props[0].Value.ValueKind != JsonValueKind.String) throw new JsonException();
                    var read = task.Browser.ReadAssistantText(props[0].Value.GetString()!, Allowed);
                    result = read; error = read.Error;
                }
                else if (name == "filesystem.read_chunk")
                {
                    if (props.Length != 2 || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 2 ||
                        !args.TryGetProperty("item_token", out var item) || item.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("offset", out var position) || position.ValueKind != JsonValueKind.Number || !position.TryGetInt64(out var offset) || offset < 0) throw new JsonException();
                    var read = task.Browser.ReadAssistantTextChunk(item.GetString()!, offset, Allowed);
                    result = read; error = read.Error;
                }
                else if (name == "filesystem.create")
                {
                    if (props.Length != 3 || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 3 ||
                        !args.TryGetProperty("folder_token", out var parent) || parent.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("name", out var fileName) || fileName.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("content", out var content) || content.ValueKind != JsonValueKind.String) throw new JsonException();
                    var created = task.Browser.CreateAssistantText(parent.GetString()!, fileName.GetString()!, content.GetString()!, Allowed);
                    result = created; error = created.Error; uncertain = created.Uncertain;
                }
                else if (name == "filesystem.mkdir")
                {
                    if (props.Length != 2 || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 2 ||
                        !args.TryGetProperty("folder_token", out var parent) || parent.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("name", out var folderName) || folderName.ValueKind != JsonValueKind.String) throw new JsonException();
                    var created = task.Browser.CreateAssistantFolder(parent.GetString()!, folderName.GetString()!, Allowed);
                    result = created; error = created.Error; uncertain = created.Uncertain;
                }
                else if (name is "filesystem.launch" or "filesystem.delete" or "filesystem.rmdir")
                {
                    if (props.Length != 2 || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 2 ||
                        !args.TryGetProperty("item_token", out var item) || item.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("purpose", out var purpose) || purpose.ValueKind != JsonValueKind.String ||
                        string.IsNullOrWhiteSpace(purpose.GetString()) || purpose.GetString()!.Length > 500 || purpose.GetString()!.Any(char.IsControl)) throw new JsonException();
                    var target = name == "filesystem.rmdir" ? task.Browser.DescribeAssistantFolderForRemoval(item.GetString()!, Allowed) : task.Browser.DescribeAssistantItem(item.GetString()!, Allowed);
                    // Shortcuts and scripts can conceal a command line. They stay
                    // unavailable until the command-specific approval flow exists.
                    var extension = target.Path is null ? "" : Path.GetExtension(target.Path).ToLowerInvariant();
                    if (name == "filesystem.launch" && target.Error is null && extension is (".lnk" or ".url" or ".bat" or ".cmd" or ".ps1" or ".com"))
                        target = (null, "command_approval_unavailable");
                    if (target.Error is not null)
                    {
                        result = new { error = target.Error }; error = target.Error;
                    }
                    else
                    {
                        Task<bool> decision;
                        lock (_authority)
                        {
                            if (!Allowed())
                            {
                                _journal.TransitionAssistantAction(actionId, "running", "cancelled", "authorization_revoked");
                                return new(actionId, "cancelled", "authorization_revoked", null);
                            }
                            var operation = name == "filesystem.rmdir" ? "Permanently delete empty folder" : name == "filesystem.delete" ? "Permanently delete file" : "Open file or program";
                            decision = AskActionAsync(new(Guid.NewGuid(), taskId, actionId, operation, target.Path!, purpose.GetString()!, DateTimeOffset.UtcNow.AddMinutes(2)), CancellationToken.None);
                        }
                        var approved = await decision;
                        if (!approved || !Allowed())
                        {
                            var recordedDenial = _journal.TransitionAssistantAction(actionId, "running", "cancelled", approved ? "authorization_revoked" : "approval_denied_or_expired");
                            return recordedDenial ? new(actionId, "cancelled", approved ? "authorization_revoked" : "approval_denied_or_expired", null) : new(actionId, "uncertain", "journal_failure", null);
                        }
                        if (name is "filesystem.delete" or "filesystem.rmdir")
                        {
                            var deleted = name == "filesystem.rmdir" ? task.Browser.RemoveAssistantEmptyFolder(item.GetString()!, Allowed) : task.Browser.DeleteAssistantFile(item.GetString()!, Allowed);
                            result = deleted; error = deleted.Error; uncertain = deleted.Uncertain || (deleted.Error is null && !Allowed());
                        }
                        else
                        {
                            using var launchWindows = new NativeLaunchWindows();
                            var beforeLaunch = launchWindows.Snapshot();
                            var opened = task.Browser.Open(new DesktopOpenRequest(actionId, item.GetString()!, true, OpenFolder: true), Allowed);
                            result = opened; error = opened.Error; uncertain = error is null || !Allowed();
                            if (error is null && Allowed())
                            {
                                var evidence = await AssistantLaunchVerification.ObserveAsync(target.Path!, beforeLaunch, launchWindows, Allowed);
                                if (evidence is not null && Allowed())
                                {
                                    result = new { error = (string?)null, processId = evidence.ProcessId,
                                        processName = evidence.ProcessName, windowTitle = evidence.Title,
                                        windowHandle = evidence.Handle.ToInt64() };
                                    uncertain = false;
                                }
                            }
                        }
                    }
                }
                else if (name == "command.run")
                {
                    if (props.Length != 2 || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 2 ||
                        !args.TryGetProperty("command", out var command) || command.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("purpose", out var purpose) || purpose.ValueKind != JsonValueKind.String ||
                        string.IsNullOrWhiteSpace(command.GetString()) || command.GetString()!.Length > 4000 || command.GetString()!.Any(char.IsControl) ||
                        string.IsNullOrWhiteSpace(purpose.GetString()) || purpose.GetString()!.Length > 500 || purpose.GetString()!.Any(char.IsControl)) throw new JsonException();
                    Task<bool> decision; CancellationToken token;
                    lock (_authority)
                    {
                        if (!Allowed() || !_taskCancellation.TryGetValue(taskId, out var source))
                        {
                            _journal.TransitionAssistantAction(actionId, "running", "cancelled", "authorization_revoked");
                            return new(actionId, "cancelled", "authorization_revoked", null);
                        }
                        token = source.Token;
                        decision = AskActionAsync(new(Guid.NewGuid(), taskId, actionId, "Run command", command.GetString()!, purpose.GetString()!, DateTimeOffset.UtcNow.AddMinutes(2)), token);
                    }
                    if (!await decision || !Allowed())
                    {
                        var recordedDenial = _journal.TransitionAssistantAction(actionId, "running", "cancelled", "approval_denied_or_expired");
                        return recordedDenial ? new(actionId, "cancelled", "approval_denied_or_expired", null) : new(actionId, "uncertain", "journal_failure", null);
                    }
                    Task<AssistantCommandResult> runningCommand;
                    lock (_authority)
                    {
                        // Admission and Process.Start share the STOP authority lock;
                        // a command cannot begin after STOP takes effect.
                        runningCommand = AssistantCommands.RunAsync(command.GetString()!, Allowed, token);
                    }
                    var run = await runningCommand;
                    var saved = _journal.TransitionAssistantAction(actionId, "running", run.Status, run.Error, JsonSerializer.Serialize(new { run.ExitCode, run.Error }));
                    return saved ? new(actionId, run.Status, run.Error, run) : new(actionId, "uncertain", "journal_failure", null);
                }
                else if (name == "filesystem.replace")
                {
                    if (props.Length != 3 || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 3 ||
                        !args.TryGetProperty("item_token", out var item) || item.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("purpose", out var purpose) || purpose.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("content", out var content) || content.ValueKind != JsonValueKind.String ||
                        string.IsNullOrWhiteSpace(purpose.GetString()) || purpose.GetString()!.Length > 500 || purpose.GetString()!.Any(char.IsControl) ||
                        System.Text.Encoding.UTF8.GetByteCount(content.GetString()!) > 4096 || content.GetString()!.Any(c => char.IsControl(c) && c is not ('\r' or '\n' or '\t'))) throw new JsonException();
                    var target = task.Browser.DescribeAssistantItem(item.GetString()!, Allowed);
                    if (target.Error is not null) { result = new { error = target.Error }; error = target.Error; }
                    else
                    {
                        Task<bool> decision;
                        lock (_authority)
                        {
                            if (!Allowed())
                            {
                                _journal.TransitionAssistantAction(actionId, "running", "cancelled", "authorization_revoked");
                                return new(actionId, "cancelled", "authorization_revoked", null);
                            }
                            decision = AskActionAsync(new(Guid.NewGuid(), taskId, actionId, "Replace text file contents", target.Path! + "\n\nReplacement text:\n" + content.GetString(), purpose.GetString()!, DateTimeOffset.UtcNow.AddMinutes(2)), CancellationToken.None);
                        }
                        if (!await decision || !Allowed())
                        {
                            var cancelled = _journal.TransitionAssistantAction(actionId, "running", "cancelled", "approval_denied_or_expired");
                            return cancelled ? new(actionId, "cancelled", "approval_denied_or_expired", null) : new(actionId, "uncertain", "journal_failure", null);
                        }
                        var edited = task.Browser.ReplaceAssistantText(item.GetString()!, content.GetString()!, Allowed);
                        result = edited; error = edited.Error; uncertain = edited.Uncertain || (edited.Error is null && !Allowed());
                    }
                }
                else if (name is "filesystem.copy" or "filesystem.move" or "filesystem.move_folder")
                {
                    if (props.Length != 4 || props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != 4 ||
                        !args.TryGetProperty("item_token", out var item) || item.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("folder_token", out var folder) || folder.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("name", out var fileName) || fileName.ValueKind != JsonValueKind.String ||
                        !args.TryGetProperty("purpose", out var purpose) || purpose.ValueKind != JsonValueKind.String ||
                        string.IsNullOrWhiteSpace(fileName.GetString()) || fileName.GetString()!.Length > 200 || fileName.GetString()!.Any(char.IsControl) ||
                        string.IsNullOrWhiteSpace(purpose.GetString()) || purpose.GetString()!.Length > 500 || purpose.GetString()!.Any(char.IsControl)) throw new JsonException();
                    var moveFolder = name == "filesystem.move_folder";
                    var source = moveFolder ? task.Browser.DescribeAssistantFolderForRemoval(item.GetString()!, Allowed) : task.Browser.DescribeAssistantItem(item.GetString()!, Allowed);
                    var destination = task.Browser.DescribeAssistantItem(folder.GetString()!, Allowed);
                    error = source.Error ?? destination.Error;
                    if (error is not null) result = new { error };
                    else
                    {
                        Task<bool> decision;
                        lock (_authority)
                        {
                            if (!Allowed())
                            {
                                _journal.TransitionAssistantAction(actionId, "running", "cancelled", "authorization_revoked");
                                return new(actionId, "cancelled", "authorization_revoked", null);
                            }
                            decision = AskActionAsync(new(Guid.NewGuid(), taskId, actionId, name == "filesystem.copy" ? "Copy file" : moveFolder ? "Move folder and its contents" : "Move file", source.Path! + "\n\nDestination folder: " + destination.Path! + "\nNew name: " + fileName.GetString(), purpose.GetString()!, DateTimeOffset.UtcNow.AddMinutes(2)), CancellationToken.None);
                        }
                        if (!await decision || !Allowed())
                        {
                            var cancelled = _journal.TransitionAssistantAction(actionId, "running", "cancelled", "approval_denied_or_expired");
                            return cancelled ? new(actionId, "cancelled", "approval_denied_or_expired", null) : new(actionId, "uncertain", "journal_failure", null);
                        }
                        if (name == "filesystem.copy")
                        {
                            var copied = task.Browser.CopyAssistantFile(item.GetString()!, folder.GetString()!, fileName.GetString()!, Allowed);
                            result = copied; error = copied.Error; uncertain = copied.Uncertain || (copied.Error is null && !Allowed());
                        }
                        else
                        {
                            var moved = task.Browser.MoveAssistantFile(item.GetString()!, folder.GetString()!, fileName.GetString()!, Allowed, moveFolder);
                            result = moved; error = moved.Error; uncertain = moved.Uncertain || (moved.Error is null && !Allowed());
                        }
                    }
                }
                else if (name == "desktop.open")
                {
                    // Batch 2 (100.030, H8): desktop.open used to work only by
                    // falling through to this catch-all else, so any future
                    // allowlisted verb without a branch would have been
                    // misrouted to folder-open. It now has its own branch and
                    // reports the real outcome: OpenAssistantFolder returns a
                    // null error only after the pinned identity was verified
                    // and the shell accepted the open, so that is completed;
                    // a shell refusal is failed. (Unlike filesystem.launch
                    // there is no window-evidence step for a folder open.)
                    if (props.Length != 1 || props[0].Name != "item_token" || props[0].Value.ValueKind != JsonValueKind.String) throw new JsonException();
                    var opened = task.Browser.OpenAssistantFolder(new DesktopOpenRequest(actionId, props[0].Value.GetString()!, true, OpenFolder: true), Allowed);
                    result = opened; error = opened.Error;
                }
                else
                {
                    // Defensive: the allowlist above rejects unknown verbs, so
                    // this is unreachable in production. Never fall through to
                    // a folder open for an unhandled verb again.
                    result = new { error = "unsupported_action" }; error = "unsupported_action";
                }
                // A STOP concurrent with a shell call can leave its outcome unknown.
                // Shell dispatch alone does not prove a window opened.
                var stillAllowed = Allowed();
                if (!stillAllowed && name is not ("desktop.open" or "filesystem.create" or "filesystem.mkdir" or "filesystem.rmdir" or "filesystem.launch" or "filesystem.delete" or "filesystem.replace" or "filesystem.copy" or "filesystem.move" or "filesystem.move_folder" or "window.activate" or "window.close"))
                {
                    var cancelled = _journal.TransitionAssistantAction(actionId, "running", "cancelled", "authorization_revoked");
                    return cancelled ? new(actionId, "cancelled", "authorization_revoked", null) : new(actionId, "uncertain", "journal_failure", null);
                }
                // Batch 2 (100.030, H6/H8): desktop.open no longer reports
                // uncertain on success — a null error from OpenAssistantFolder
                // is completed (see the branch above). window.close is
                // completed only when verified gone. Both still report
                // uncertain, not completed, when authority was revoked during
                // the action, matching the filesystem verbs.
                var status = uncertain || ((name is "filesystem.create" or "filesystem.mkdir" or "filesystem.rmdir" or "filesystem.delete" or "filesystem.replace" or "filesystem.copy" or "filesystem.move" or "filesystem.move_folder" or "window.activate" or "window.close" or "desktop.open") && error is null && !stillAllowed) ? "uncertain" :
                    !stillAllowed ? "cancelled" : error is null ? "completed" : "failed";
                // Do not persist file contents in the action journal.
                var historyError = error;
                if (name == "filesystem.copy" && result is AssistantFileCopy { Uncertain: true, Destination: { } partialPath })
                {
                    historyError = (error ?? "copy_unconfirmed") + " — Possible partial copy: " + partialPath;
                    if (historyError.Length > 1000) historyError = historyError[..997] + "...";
                }
                var recorded = _journal.TransitionAssistantAction(actionId, "running", status, historyError,
                    name is "filesystem.read" or "filesystem.read_chunk" or "window.inspect" or "window.screenshot" ? JsonSerializer.Serialize(new { error }) : JsonSerializer.Serialize(result));
                return recorded ? new(actionId, status, error, result) : new(actionId, "uncertain", "journal_failure", null);
            }
            catch (JsonException)
            {
                // Batch 2 (100.030, H7): every `throw new JsonException()` in
                // the verb chain is argument-shape validation (wrong property
                // count/name, out-of-range number, over-long purpose, bad
                // token) or malformed arguments JSON — nothing happened, so
                // there is nothing unconfirmed. Report failed/invalid_arguments
                // (the journal allows running -> failed) instead of uncertain,
                // which used to poison the whole task via _uncertainTasks.
                // Batch 2 (100.030, G3): any error releases any key a
                // previous action left held. Best-effort, never throws.
                InputInjector.ReleaseAllHeldKeys();
                _journal.TransitionAssistantAction(actionId, "running", "failed", "invalid_arguments");
                return new(actionId, "failed", "invalid_arguments", null);
            }
            catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or IOException or UnauthorizedAccessException)
            {
                // Batch 2 (100.030, G3): any error releases held keys.
                InputInjector.ReleaseAllHeldKeys();
                _journal.TransitionAssistantAction(actionId, "running", "uncertain", "execution_unconfirmed");
                return new(actionId, "uncertain", "execution_unconfirmed", null);
            }
            catch (Exception)
            {
                // Batch 2 (100.030): an unexpected exception (e.g. a null
                // dereference after a partial effect) must still produce a
                // journal transition and a reply, so the Python side gets a
                // host_result instead of waiting out its action timeout with
                // the journal action stuck at running. The effect may have
                // partially happened, so this stays uncertain, not failed.
                // Batch 2 (100.030, G3): any error releases held keys.
                InputInjector.ReleaseAllHeldKeys();
                _journal.TransitionAssistantAction(actionId, "running", "uncertain", "execution_unconfirmed");
                return new(actionId, "uncertain", "execution_unconfirmed", null);
            }
        }
        finally { lock (_authority) _running.Remove(actionId); }
    }

    // Batch 2 (100.030, D10): per-task input budget. At most 40 pointer/key
    // ops per task; pointer moves/scrolls at most ~10 per second (rolling
    // 1 s window; the spec's "~10" is read as exactly 10 in any rolling
    // second). Counted ops are the six screen.* ops plus control.key,
    // control.key_down and control.key_up — control.type (text set) and
    // control.invoke are not pointer/key ops. Call with _authority held:
    // the check and the consume are one atomic step, so a burst cannot
    // slip past the cap between the check and the dispatch. Returns null
    // when the op fits, or "input_budget_exceeded" when it does not. A
    // capped op is never delayed (no silent slowdown) and never consumes
    // a phone approval — call sites check before AskActionAsync. The
    // journal has no "blocked" transition (JettJournal allows running ->
    // completed/failed/uncertain/cancelled only), so call sites journal
    // the capped op as failed with the same error while the returned
    // result carries status "blocked". Once the count is hit, every
    // further pointer/key op in the task returns the same blocked result,
    // so the task's input stops; reads and other ops are unaffected.
    private sealed class InputBudget
    {
        internal int Ops;
        internal readonly Queue<DateTimeOffset> RecentMoves = new();
    }

    private string? ConsumeInputBudget(Guid taskId, string name)
    {
        if (!_inputBudgets.TryGetValue(taskId, out var budget)) return "input_budget_exceeded";
        var now = DateTimeOffset.UtcNow;
        if (name is "screen.move" or "screen.scroll")
        {
            while (budget.RecentMoves.Count > 0 && now - budget.RecentMoves.Peek() > TimeSpan.FromSeconds(1)) budget.RecentMoves.Dequeue();
            if (budget.RecentMoves.Count >= 10) return "input_budget_exceeded";
        }
        if (budget.Ops >= 40) return "input_budget_exceeded";
        budget.Ops++;
        if (name is "screen.move" or "screen.scroll") budget.RecentMoves.Enqueue(now);
        return null;
    }

    private bool Authorized(long generation) => !_disposed && !_stopped && generation == _generation && DateTimeOffset.UtcNow < _expiry;
    private bool TaskAuthorized(Guid taskId, long generation) => Authorized(generation) &&
        _routes.TryGetValue(taskId, out var route) && RouteAuthorized?.Invoke(route.Assistant, route.Account) != false;

    private async Task<bool> AskActionAsync(AssistantApproval request, CancellationToken cancellation)
    {
        // Generic clicks, keys and control invocations can delete or overwrite
        // data. Without reliable intent evidence they retain exact-action review.
        var ordinary = request.Operation is "Scroll window" or "Move pointer" or "Copy file";
        if (request.Operation == "Open file or program")
            ordinary = Directory.Exists(request.Target) || Path.GetExtension(request.Target).ToLowerInvariant() is ".txt" or ".md" or ".pdf" or ".png" or ".jpg" or ".jpeg";
        var harmlessCommand = request.Operation == "Run command" && request.Target is "ver" or "whoami" or "hostname" or "echo DESKHAND_COMMAND_FIXTURE";
        if (AutomaticOrdinaryActions && (ordinary || harmlessCommand)) return true;
        // Batch 2 (100.030, G3): the single approval funnel for every
        // branch, so a denied or expired approval releases any key a
        // previous action left held. Best-effort, never throws. A wait
        // aborted by STOP / task end (OperationCanceledException) releases
        // too, then rethrows so the caller reports it as before.
        bool approved;
        try { approved = await Approvals.AskAsync(request, cancellation); }
        catch (OperationCanceledException) { InputInjector.ReleaseAllHeldKeys(); throw; }
        if (!approved) InputInjector.ReleaseAllHeldKeys();
        return approved;
    }

    public void Dispose()
    {
        lock (_authority)
        {
            if (_disposed) return;
            _disposed = true; _stopped = true; _generation++;
            foreach (var cancellation in _taskCancellation.Values) cancellation.Cancel();
            _taskCancellation.Clear();
            Approvals.CancelAll();
            // Batch 2 (100.030, G3): teardown must not leave a key held
            // down. Best-effort, never throws.
            InputInjector.ReleaseAllHeldKeys();
            foreach (var task in _tasks.Values) task.Browser.End();
            _tasks.Clear();
            _routes.Clear();
            _windows.Clear();
            _inputBudgets.Clear();
            foreach (var session in _accessibility.Values) session.Clear();
            _accessibility.Clear();
            foreach (var session in _screens.Values) session.Clear();
            _screens.Clear();
        }
    }
}
