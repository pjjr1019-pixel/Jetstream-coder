using System.Text.Json;

namespace Deskhand.Windows.App;

public sealed record FullPcAccessPreference
{
    public string Assistant { get; init; } = "";
    public string Account { get; init; } = "";
    public bool Enabled { get; init; }
    public long Generation { get; init; }
    public string ApprovalPolicy { get; init; } = "confirm_destructive";
}

public sealed partial class HostEngine
{
    private volatile FullPcAccessPreference _fullAccess = new();
    private long _accessChange;
    private readonly object _accessGate = new();
    private bool _fullAccessInitialized;
    private volatile string _accessState = "Off";
    private string? _accessError;

    internal void InitializeFullPcAccess()
    {
        _fullAccessInitialized = true;
        _accessChange = Config.FullPcAccess.Generation;
        // The local grant is independent of provider availability. The service
        // still validates the exact catalog route before accepting a task.
        _assistantActions.RouteAuthorized = (assistant, account) =>
            !Config.Paused && _fullAccess.Enabled && _accessState == "On" &&
            SupportedAccessRoute(assistant, account);
        _assistantActions.AutomaticOrdinaryActions = true;
        _assistantActions.Stop();
        // Reset the stop signal early when the saved grant is on, so the
        // vendor bridge does not trip its own STOP at startup. The service
        // starts on the first Chat call (the ChatPanel Load event), which
        // fires before main.Shown runs RestoreFullPcAccess, and the
        // bridge's safety monitor fires emergency_stop within 0.2s of
        // seeing the event set. Resetting here (from the saved grant,
        // before any Chat call) removes that race at its root. This does
        // not grant actions: RouteAuthorized still requires the in-memory
        // grant set by SetFullPcAccess at Shown, and if that validation
        // fails the signal is set again there.
        // STOP latch removed (Phil 2026-11-09): the signal is never set.
        _museStopSignal.Reset();
        if (Chat is AssistantClient client) client.AttachFullPcAccess(AccessRequest);
    }

    // 100.016 (Worker B) — raw grant state for the health snapshot
    // (spec §3.1). FullPcAccessStatus is per-route (assistant/account)
    // and folds Paused into "Off"; the snapshot needs the stored grant
    // (Enabled + Generation) and the Muse sign-in state (_accessState:
    // "On" / "Needs sign-in" / "Off" / "Connecting", plus the last
    // error) exactly as recorded. Read under _accessGate, like
    // FullPcAccessStatus. This is a getter only — it changes no state.
    internal (bool Enabled, long Generation, string State, string? Error) GrantSnapshot()
    {
        lock (_accessGate) return (_fullAccess.Enabled, _fullAccess.Generation, _accessState, _accessError);
    }
    internal object FullPcAccessStatus(string assistant, string account)
    {
        lock (_accessGate)
        {
            var supported = SupportedAccessRoute(assistant, account);
            var state = Config.Paused || !supported ? "Off" :
                _accessState == "On" && !_assistantActions.HasActiveGrant() ? "Off" : _accessState;
            return new { assistant, account, supported, enabled = supported && _fullAccess.Enabled && !Config.Paused,
                state, generation = _fullAccess.Generation, approvalPolicy = "confirm_destructive",
                error = supported ? _accessError :
                    "This AI account is unavailable. Refresh assistants or sign in." };
        }
    }

    private async Task<JsonElement> AccessRequest(JsonElement command, Func<Func<bool>, bool>? guard)
    {
        var assistant = command.GetProperty("assistant").GetString() ?? "";
        var account = command.GetProperty("account").GetString() ?? "";
        if (command.GetProperty("op").GetString() == "access_set")
        {
            var enabled = command.GetProperty("enabled").GetBoolean();
            await SetFullPcAccess(assistant, account, enabled, guard);
        }
        return JsonSerializer.SerializeToElement(FullPcAccessStatus(assistant, account));
    }

    internal async Task SetFullPcAccess(string assistant, string account, bool enabled,
        Func<Func<bool>, bool>? guard = null, bool restoring = false)
    {
        if (!_fullAccessInitialized) throw new InvalidOperationException("Full PC access is not initialized.");
        long change;
        lock (_accessGate)
        {
            bool BeginChange()
            {
                _fullAccess = new() { Assistant = assistant, Account = account, Generation = Interlocked.Increment(ref _accessChange) };
                _accessState = enabled ? "Connecting" : "Off"; _accessError = null;
                return true;
            }
            if (!(guard is null ? BeginChange() : guard(BeginChange))) throw new InvalidOperationException("Phone control permission is required.");
            change = _accessChange;
            _assistantActions.Stop();
            Config.PersistentDesktopAccess = false;
            Config.FullPcAccess = _fullAccess;
            Config.Save();
        }
        try
        {
            // Revoke the other executor before granting any selected provider.
            if (Chat is not null)
            {
                await Chat.CallAsync(JsonSerializer.SerializeToElement(new { op = "stop_agent_work" }));
                // STOP latch removed (Phil 2026-11-09): no stop_muse op, no
                // unconfirmed-stop gate. The Muse executor is not stopped here.
            }
            if (!enabled) return;
            if (!SupportedAccessRoute(assistant, account))
                throw new InvalidOperationException("Full PC access is unavailable for this AI adapter.");
            if (Chat is null) throw new InvalidOperationException("Assistant service is unavailable.");
            // Resume the queue before committing authority; no old task is replayed.
            await Chat.CallAsync(JsonSerializer.SerializeToElement(new { op = "resume_chat_local" }));
            lock (_accessGate)
            {
                bool Commit()
                {
                    lock (_gate)
                    {
                    if (change != _accessChange || Config.Paused) return false;
                    // Grant the local action engine only; the Muse stop event is never set
                    // (STOP latch removed 2026-11-09).
                    _fullAccess = _fullAccess with { Enabled = true };
                    Config.FullPcAccess = _fullAccess; Config.Save();
                    _assistantActions.GrantLocally(DateTimeOffset.MaxValue);
                    _desktopGrantExpiresUtc = DateTimeOffset.MaxValue;
                    _museStopSignal.Reset();
                    _accessState = "On";
                    return true;
                    }
                }
                if (!(guard is null ? Commit() : guard(Commit))) throw new InvalidOperationException("Access changed or the phone disconnected; access remains off.");
            }
        }
        catch (Exception ex)
        {
            lock (_accessGate)
                if (change == _accessChange)
                {
                    _fullAccess = _fullAccess with { Enabled = false }; _accessState = ex.Message.Contains("sign-in", StringComparison.OrdinalIgnoreCase) ? "Needs sign-in" : "Off"; _accessError = ex.Message;
                    _assistantActions.Stop();
                    Config.FullPcAccess = _fullAccess; Config.Save();
                }
            throw;
        }
        finally { RaiseChanged(); }
    }

    internal async Task RestoreFullPcAccess()
    {
        // Full PC access is always on: restore (or enable) the grant at
        // every start, even if it was saved off. The panel retries after
        // sign-in and on its access poll, so a failure here is not final.
        var saved = Config.FullPcAccess;
        if (saved.ApprovalPolicy != "confirm_destructive") return;
        var assistant = string.IsNullOrEmpty(saved.Assistant) ? "muse" : saved.Assistant;
        // B2-A B-1: the account default must test the saved account, not the
        // assistant. Testing saved.Assistant here sent SetFullPcAccess an
        // empty account whenever a saved record had an assistant but no
        // account, which SupportedAccessRoute rejects, so restore failed
        // on every start with no hint that the account was the problem.
        var account = string.IsNullOrEmpty(saved.Account)
            ? assistant switch { "muse" => "deskhand", "codex" => "default", _ => "" }
            : saved.Account;
        try { await SetFullPcAccess(assistant, account, true, restoring: true); }
        catch (Exception ex) { RaiseNotice("Full PC access remains off: " + ex.Message); }
    }

    private static bool SupportedAccessRoute(string assistant, string account) =>
        (assistant == "local" && account == "") || (assistant == "muse" && account == "deskhand") ||
        (assistant == "codex" && (account == "default" || (account.StartsWith("codex:", StringComparison.Ordinal) && account.Length == 70)));
}
