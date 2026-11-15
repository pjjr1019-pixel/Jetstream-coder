using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Deskhand.Core;
using Deskhand.Contracts;

namespace Deskhand.Windows.App;

public enum PairDecision { Reject, ViewOnly, Control }

public sealed record PairRequestInfo(string DeviceName, string ConfirmCode);

public sealed record PairingInfo(string Serial, string QrPayload, DateTime ExpiresUtc);

public sealed partial class HostEngine : IDisposable
{
    private const int MaxPairingFailures = 5;
    private static readonly TimeSpan PairingLifetime = TimeSpan.FromMinutes(10);

    private readonly object _gate = new();
    // 100.016 (Worker B) — health snapshot store (spec §3.2, STEP0 §0.2).
    // MainForm collects the 9 C#-owned rows once per 2s tick and publishes
    // them here; the service reads them through the `health.snapshot`
    // HostRpc op. Guarded by its own lock, the same pattern as _gate: a
    // stale snapshot is acceptable, a torn one is not. The store never
    // probes anything — it only holds the last published snapshot.
    private readonly object _healthSnapshotGate = new();
    private Dictionary<string, Dictionary<string, object>>? _healthSnapshot;
    // 100.017 — last sources map from the runtime_stream op, published
    // by MainForm's 1s tick (see PublishStreamPoll). Read by a new
    // session's stream.hello; null until the first tick.
    private JsonElement? _streamSources;
    private readonly List<Listener> _listeners = [];
    private Listener? _pairing;
    private System.Threading.Timer? _pairingTimer;
    private readonly JettJournal _jettJournal;
    private readonly EventWaitHandle _museStopSignal;
    private readonly System.Threading.Timer _museGrantExpiryTimer;
    private readonly string _museStopEventName;
    private readonly AssistantActionEngine _assistantActions;
    private IAssistantChat? _chat;
    private bool _started;
    private DateTimeOffset? _desktopGrantExpiresUtc;
    private bool _disposed;

    private readonly Func<DesktopBrowser> _desktopFactory;
    private readonly Func<IDesktopCapture> _captureFactory;
    private readonly PhoneDisplay _phoneDisplay;
    internal Action<double, double>? InputMoveObservedForFixture { get; set; }
    public HostEngine(AppConfig config, Func<DesktopBrowser>? desktopFactory = null, Func<IDesktopCapture>? captureFactory = null, string? androidUpdateDirectory = null)
        : this(config, null, desktopFactory, captureFactory, androidUpdateDirectory) { }

    internal HostEngine(AppConfig config, byte[]? connectorCapabilityForFixture,
        Func<DesktopBrowser>? desktopFactory = null, Func<IDesktopCapture>? captureFactory = null, string? androidUpdateDirectory = null,
        Func<IAssistantWindowNative>? windowFactoryForFixture = null)
    {
        Config = config;
        _phoneDisplay = new PhoneDisplay(config.PhoneFriendlyDisplay, DisplayNotice);
        // An isolated live fixture must share the installed host's action
        // journal. The installed host ignores this switch.
        var journalDirectory = Environment.GetEnvironmentVariable("DESKHAND_CONFIG_DIR") is not null &&
            Environment.GetEnvironmentVariable("DESKHAND_FIXTURE_SHARED_JOURNAL") == "1"
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Deskhand")
            : AppConfig.DataDirectory;
        _jettJournal = new JettJournal(Path.Combine(journalDirectory, "jett-operator.db"));
        _jettJournal.RecoverAssistantActions();
        _museStopEventName = @"Local\Deskhand.MuseStop." + Guid.NewGuid().ToString("N");
        // A saved setting is not an active grant until the host restores it.
        // STOP latch removed (Phil 2026-11-09): the event is created RESET and
        // is never set. The event still exists because the vendor bridge
        // requires it (ensure_host_allows fails closed without it), but it
        // stays reset so the bridge never trips STOP.
        _museStopSignal = new EventWaitHandle(false, EventResetMode.ManualReset, _museStopEventName);
        _museGrantExpiryTimer = new System.Threading.Timer(_ => CheckMuseGrantExpiry(), null, Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
        AndroidUpdateDirectory = androidUpdateDirectory;
        _captureFactory = captureFactory ?? (() => new ScreenCapturer());
        _desktopFactory = desktopFactory ?? (() => new DesktopBrowser());
        _assistantActions = new AssistantActionEngine(_jettJournal, _desktopFactory, windowFactoryForFixture);
    }

    internal string? AndroidUpdateDirectory { get; }
    internal string MuseStopEventName => _museStopEventName;
    public AppConfig Config { get; }
    public IAssistantChat? Chat
    {
        get => _chat;
        set
        {
            _chat = value;
            if (value is AssistantClient assistant) { assistant.AttachMuseStopEvent(_museStopEventName); assistant.AttachJettJournal(_jettJournal); assistant.AttachHostAction(ExecuteAssistantAction); assistant.AttachTaskLifecycle(BeginAssistantTask, _assistantActions.End, _assistantActions.Active); assistant.AttachTransportLost(AssistantTransportLost); assistant.AttachHealthSnapshot(GetHealthSnapshot); }
        }
    }
    public bool JettActionsEnabled => false;
    public bool LocalAssistantTasksAvailable => true;
    public DateTimeOffset? DesktopGrantExpiresUtc { get { lock (_gate) return _desktopGrantExpiresUtc; } }

    private void CheckMuseGrantExpiry()
    {
        lock (_gate)
        {
            if (_disposed) return;
            if (_desktopGrantExpiresUtc is not { } expiry || expiry <= DateTimeOffset.UtcNow || Config.Paused)
                { /* STOP latch removed: expiry no longer sets the stop signal. */ }
            else if (expiry != DateTimeOffset.MaxValue)
            {
                var remaining = expiry - DateTimeOffset.UtcNow;
                var next = remaining <= TimeSpan.Zero ? TimeSpan.Zero : remaining < TimeSpan.FromSeconds(1) ? remaining : TimeSpan.FromSeconds(1);
                _museGrantExpiryTimer.Change(next, Timeout.InfiniteTimeSpan);
            }
        }
    }

    // Internal host entry points only; model/browser traffic cannot call these directly.
    internal bool BeginAssistantTask(Guid taskId, Guid turnId, string assistant, string model, string account, string effort) =>
        _assistantActions.Begin(taskId, turnId, assistant, model, account, effort);

    internal AssistantApprovals Approvals => _assistantActions.Approvals;
    internal AssistantApproval[] AllApprovals() => Approvals.Pending();
    internal bool ResolveApproval(Guid id, bool approved) => Approvals.Resolve(id, approved);
    internal AssistantActionEngine AssistantActionsForFixture => _assistantActions;
    internal AssistantTaskStatus[] AssistantTasks => _assistantActions.Tasks();

    internal Task<AssistantActionResult> ExecuteAssistantAction(Guid taskId, Guid turnId, Guid actionId, string name, string argumentsJson) =>
        _assistantActions.ExecuteAsync(taskId, turnId, actionId, name, argumentsJson);

    private void AssistantTransportLost()
    {
        // This callback runs under the assistant client's lock: revoke first
        // without taking the host lock, then notify asynchronously.
        _assistantActions.Stop();
        _ = Task.Run(() => RaiseNotice("Assistant disconnected; computer-control authority was revoked."));
    }

    public void PublishChatHistory(JsonElement history)
    {
        Listener[] listeners;
        lock (_gate) listeners = _listeners.ToArray();
        foreach (var listener in listeners) listener.PublishChatHistory(history);
    }

    // 100.017 (Worker B) — stream forwarder fan-out (spec §3.2, STEP0
    // §0.12). MainForm collects the `runtime_stream` op result on its
    // own 1s timer and publishes it here. The engine keeps the last
    // sources map (so a new session's stream.hello can name the live
    // feeds) and each Listener keeps its own lastSentSeq + ack state,
    // dropped on EndSession. No event store anywhere on the host: the
    // service ring is the store; a frame that cannot be sent is
    // dropped, never queued.
    internal bool AnyStreamSubscriber
    {
        get
        {
            lock (_gate) return _listeners.Any(l => l.StreamSubscribed);
        }
    }

    internal long StreamSince()
    {
        lock (_gate)
        {
            var since = long.MaxValue;
            foreach (var l in _listeners)
                if (l.StreamSubscribed && l.StreamLastSentSeq < since) since = l.StreamLastSentSeq;
            return since == long.MaxValue ? 0 : since;
        }
    }

    internal JsonElement? StreamSources
    {
        get { lock (_gate) return _streamSources is { } stored ? stored.Clone() : null; }
    }

    internal void PublishStreamPoll(JsonElement poll)
    {
        Listener[] listeners;
        lock (_gate)
        {
            if (poll.TryGetProperty("sources", out var sources) && sources.ValueKind == JsonValueKind.Object)
                _streamSources = sources.Clone();
            listeners = _listeners.ToArray();
        }
        var resynced = poll.TryGetProperty("resynced", out var resyncedValue) && resyncedValue.ValueKind == JsonValueKind.True;
        var latestSeq = poll.TryGetProperty("latest_seq", out var latestValue) && latestValue.ValueKind == JsonValueKind.Number
            && latestValue.TryGetInt64(out var seq) ? seq : 0L;
        if (!poll.TryGetProperty("events", out var events) || events.ValueKind != JsonValueKind.Array) return;
        if (!resynced && events.GetArrayLength() == 0) return;
        foreach (var listener in listeners) listener.PublishStreamEvents(events, latestSeq, resynced);
    }
    public Func<PairRequestInfo, Task<PairDecision>>? ApprovalHandler { get; set; }
    public event Action? Changed;
    public event Action<string>? Notice;
    public string? LastDisplayNotice { get; private set; }
    private void DisplayNotice(string message) { LastDisplayNotice = message; RaiseNotice(message); }

    public PairingInfo? CurrentPairing { get; private set; }
    public string PairingStatus { get; private set; } = "";

    public bool RelayConnected
    {
        get { lock (_gate) return _listeners.Any(l => l.Connected) || (_pairing?.Connected ?? false); }
    }

    // B2-A B-6: the first RelayLink failure surfaced by any listener (or the
    // pairing listener), so the UI can show why the relay is unreachable
    // instead of only "Relay disconnected". Null when every link is healthy.
    public string? RelayError
    {
        get
        {
            lock (_gate)
            {
                foreach (var l in _listeners)
                {
                    var error = l.RelayError;
                    if (error is not null) return error;
                }
                return _pairing?.RelayError;
            }
        }
    }

    public IReadOnlyList<string> ActiveDevices
    {
        get { lock (_gate) return _listeners.Where(l => l.Streaming).Select(l => l.DeviceName).ToList(); }
    }
    // 100.016 (Worker B) — one §2.1 row: exactly {status, last_ok,
    // version, error}. last_ok is a Unix epoch (0 = never). error is one
    // line, truncated to 500 chars, and version to 120 — the same
    // coercion the service-side make_row applies (Worker A), so the
    // handoff never ships a shape the op has to repair.
    // 100.029 (Worker C) — the row gains `level`, the component's
    // configured log level (E41 §6.3, DEBUG/INFO/WARN/ERROR, default
    // INFO — the §4 default on every sink). Unknown values coerce to
    // INFO, never invented; the C# components have no log file (#11),
    // so INFO is the honest default and every existing call site
    // keeps working unchanged. The row is now exactly {status,
    // last_ok, version, error, level}.
    internal static Dictionary<string, object> HealthRow(string status, long lastOk, string version, string error, string level = "INFO")
    {
        var cleanError = (error ?? "").Replace("\r", " ").Replace("\n", " ");
        if (cleanError.Length > 500) cleanError = cleanError[..500];
        var cleanVersion = version ?? "";
        if (cleanVersion.Length > 120) cleanVersion = cleanVersion[..120];
        var cleanLevel = level is "DEBUG" or "INFO" or "WARN" or "ERROR" ? level : "INFO";
        return new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["status"] = status,
            ["last_ok"] = lastOk,
            ["version"] = cleanVersion,
            ["error"] = cleanError,
            ["level"] = cleanLevel,
        };
    }

    // 100.016 (Worker B) — the 8 C#-owned keys, each UNKNOWN / 0 / "" /
    // "not checked yet" (spec §2.2). Returned when no snapshot has been
    // published yet; the service still serves its own rows.
    // 100.040: the tunnel key is gone with the Cloudflare tunnel. The
    // service pads a missing C# row UNKNOWN on its side
    // (collect_csharp_rows), so the merged snapshot still carries it —
    // the C# snapshot itself just has 8 keys now.
    internal static Dictionary<string, Dictionary<string, object>> UnknownHealthSnapshot()
    {
        var rows = new Dictionary<string, Dictionary<string, object>>(StringComparer.Ordinal);
        foreach (var key in new[] { "gateway", "worker", "relay", "node", "grant", "muse", "phone", "windows_app" })
            rows[key] = HealthRow("UNKNOWN", 0, "", "not checked yet");
        return rows;
    }

    // 100.016 (Worker B) — publish the snapshot MainForm collected on its
    // 2s tick. A deep copy is stored so later mutation by the collector
    // can never tear a read; the reader gets its own copy the same way.
    internal void PublishHealthSnapshot(Dictionary<string, Dictionary<string, object>> snapshot)
    {
        var copy = new Dictionary<string, Dictionary<string, object>>(StringComparer.Ordinal);
        foreach (var pair in snapshot)
            copy[pair.Key] = new Dictionary<string, object>(pair.Value, StringComparer.Ordinal);
        lock (_healthSnapshotGate) _healthSnapshot = copy;
    }

    // 100.016 (Worker B) — the `health.snapshot` HostRpc provider (wired
    // in the Chat setter). Returns the last published snapshot, or the
    // UNKNOWN placeholder when nothing has been published yet. Never
    // throws, never blocks on I/O: the copy is small and in-memory.
    internal object GetHealthSnapshot()
    {
        Dictionary<string, Dictionary<string, object>>? stored;
        lock (_healthSnapshotGate) stored = _healthSnapshot;
        if (stored is null) return UnknownHealthSnapshot();
        var copy = new Dictionary<string, Dictionary<string, object>>(StringComparer.Ordinal);
        foreach (var pair in stored)
            copy[pair.Key] = new Dictionary<string, object>(pair.Value, StringComparer.Ordinal);
        return copy;
    }

    public void Start()
    {
        lock (_gate)
        {
            if (_started) return;
            _started = true;
            foreach (var d in Config.Devices) _listeners.Add(new Listener(this, d, d.GetSecret(), false));
            foreach (var l in _listeners) l.Start();
        }
        RaiseChanged();
    }

    /// <summary>Reconnects every room, used when relay settings change.</summary>
    public void Restart()
    {
        lock (_gate)
        {
            foreach (var l in _listeners) l.Dispose();
            _listeners.Clear();
            _started = false;
        }
        CancelPairing();
        Start();
    }

    public PairingInfo StartPairing(string? phoneRelayUrl = null, string? tailscaleRelayUrl = null)
    {
        CancelPairing();
        var serial = Serial.Generate();
        // 100.015 (Worker B) — QR candidate list (§11.2): r= exactly as
        // before, plus t= (Tailscale, only when discovery succeeded AND it
        // differs from r=) and l= (LAN default, omitted when it equals r= so
        // the QR stays scannable). t= is never omitted when discovery
        // succeeded. No change to the secret, scan flow, or scheme.
        // StartPairing is synchronous; the CALLERS discover Tailscale first
        // (MainForm button handlers) and pass it in here.
        // 100.040 — the Cloudflare tunnel is removed: the primary must
        // never be a tunnel URL. Config.Load migrates a stored tunnel
        // URL to the LAN default, but a stale in-memory value (or an
        // old phoneRelayUrl) is guarded here too: fall back to the
        // Tailscale address when provided, else the LAN default.
        var primary = (phoneRelayUrl ?? Config.RelayUrl).TrimEnd('/');
        if (AppConfig.IsTunnelUrl(primary))
            primary = !string.IsNullOrWhiteSpace(tailscaleRelayUrl)
                ? tailscaleRelayUrl.Trim().TrimEnd('/')
                : AppConfig.DefaultRelayUrl(Config.RelayPort);
        var qr = new StringBuilder($"deskhand://pair?s={serial}&r={Uri.EscapeDataString(primary)}");
        if (!string.IsNullOrWhiteSpace(tailscaleRelayUrl))
        {
            var tailscale = tailscaleRelayUrl.Trim().TrimEnd('/');
            if (!string.Equals(tailscale, primary, StringComparison.OrdinalIgnoreCase))
                qr.Append("&t=").Append(Uri.EscapeDataString(tailscale));
        }
        var lan = AppConfig.DefaultRelayUrl(Config.RelayPort);
        if (!string.Equals(lan, primary, StringComparison.OrdinalIgnoreCase))
            qr.Append("&l=").Append(Uri.EscapeDataString(lan));
        var info = new PairingInfo(
            Serial.Format(serial),
            qr.ToString(),
            DateTime.UtcNow + PairingLifetime);
        var listener = new Listener(this, null, Serial.ToSecret(serial), true);
        lock (_gate)
        {
            _pairing = listener;
            CurrentPairing = info;
            PairingStatus = "Waiting for your phone to scan the code...";
            // B2-A B-3: the timer is created under the same lock that
            // CancelPairing clears it under, so a timer firing (or a
            // cancel) can no longer dispose a timer belonging to a newer
            // pairing, and a fresh timer can no longer be nulled away.
            _pairingTimer = new System.Threading.Timer(_ => CancelPairing("The pairing code expired."), null, PairingLifetime, Timeout.InfiniteTimeSpan);
        }
        listener.Start();
        RaiseChanged();
        return info;
    }

    public void CancelPairing(string? reason = null)
    {
        CancelPairingCore(null, reason);
    }

    // B2-A B-2: cancels only if the given pairing is still the current one,
    // so a delayed FinishPairing cancel can never kill a newer pairing.
    internal void CancelPairingIfCurrent(PairingInfo pairing, string? reason = null)
    {
        CancelPairingCore(pairing, reason);
    }

    private void CancelPairingCore(PairingInfo? expected, string? reason)
    {
        Listener? l;
        System.Threading.Timer? timer;
        lock (_gate)
        {
            if (expected is not null && !ReferenceEquals(CurrentPairing, expected)) return;
            l = _pairing;
            _pairing = null;
            CurrentPairing = null;
            PairingStatus = reason ?? "";
            timer = _pairingTimer;
            _pairingTimer = null;
        }
        timer?.Dispose();
        l?.Dispose();
        RaiseChanged();
    }

    public void Revoke(PairedDevice device)
    {
        lock (_gate)
        {
            var l = _listeners.FirstOrDefault(x => x.Device?.Id == device.Id);
            if (l is not null)
            {
                _listeners.Remove(l);
                l.Dispose();
            }
            Config.Devices.RemoveAll(d => d.Id == device.Id);
            Config.Save();
        }
        RaiseChanged();
    }

    public void SetControl(PairedDevice device, bool allowed)
    {
        lock (_gate)
        {
            device.Control = allowed;
            Config.Save();
            foreach (var listener in _listeners) listener.UpdateInfo();
        }
        RaiseChanged();
    }

    public void SetPaused(bool paused)
    {
        lock (_gate) { Config.Paused = paused; Config.Save(); }
        if (paused) RevokeJettDesktopLocally();
        if (paused) DisconnectAll();
        if (!paused && _fullAccessInitialized) _ = RestoreFullPcAccess();
        RaiseChanged();
    }

    public void SetPhoneFriendlyDisplay(bool enabled)
    {
        Config.PhoneFriendlyDisplay = enabled;
        Config.Save();
        _phoneDisplay.SetEnabled(enabled);
    }

    public void GrantJettDesktopLocally()
    {
        lock (_gate)
        {
            // A new local authority generation invalidates any fixture task
            // admitted under the previous grant, even if the new grant lasts longer.
            var expires = DateTimeOffset.UtcNow.AddMinutes(30);
            _jettJournal.RecordGrant(true, expires);
            _desktopGrantExpiresUtc = expires;
            _museGrantExpiryTimer.Change(TimeSpan.FromSeconds(1), Timeout.InfiniteTimeSpan);
            if (!Config.Paused) _assistantActions.GrantLocally(expires);
            if (!Config.Paused) _museStopSignal.Reset();
        }
        RaiseChanged();
    }

    public void GrantPersistentDesktopLocally()
    {
        lock (_gate)
        {
            Config.PersistentDesktopAccess = true;
            Config.Save();
            _jettJournal.RecordGrant(true, DateTimeOffset.MaxValue);
            _desktopGrantExpiresUtc = DateTimeOffset.MaxValue;
            _museGrantExpiryTimer.Change(Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
            if (!Config.Paused) _assistantActions.GrantLocally(DateTimeOffset.MaxValue);
            if (!Config.Paused) _museStopSignal.Reset();
        }
        RaiseChanged();
    }

    public void RevokeJettDesktopLocally()
    {
        lock (_gate)
        {
            if (_desktopGrantExpiresUtc is not null || Config.PersistentDesktopAccess)
            {
                Config.PersistentDesktopAccess = false;
                Config.Save();
                _desktopGrantExpiresUtc = null;
                _assistantActions.Stop();
                _jettJournal.RecordGrant(false, null);
            }
        }
        RaiseChanged();
    }






    private static bool TryUuidV4(JsonElement value, out Guid id)
    {
        id = Guid.Empty;
        if (value.ValueKind != JsonValueKind.String || !Guid.TryParseExact(value.GetString(), "D", out id))
            return false;
        var text = value.GetString()!;
        return id != Guid.Empty && id.ToString("D") == text && text[14] == '4' && text[19] is '8' or '9' or 'a' or 'b';
    }

    public void DisconnectAll()
    {
        List<Listener> copy;
        lock (_gate) copy = [.. _listeners];
        foreach (var l in copy) l.EndSession(true);
    }

    public void Dispose()
    {
        lock (_gate)
        {
            if (_disposed) return;
            _disposed = true;
            _assistantActions.Stop();
            _desktopGrantExpiresUtc = null;
        }
        CancelPairing();
        try { }
        finally
        {
            lock (_gate)
            {
                foreach (var l in _listeners) l.Dispose();
                _listeners.Clear();
            }
            _assistantActions.Dispose();
            _jettJournal.Dispose();
            _museGrantExpiryTimer.Dispose();
            _museStopSignal.Dispose();
        }
    }

    internal bool AcknowledgeJettPhoneReply(string deviceId, Guid replyId) =>
        _jettJournal.AcknowledgeSurface(replyId, "android:" + deviceId);

    internal void RaiseChanged() => Changed?.Invoke();
    internal void RaiseNotice(string text) => Notice?.Invoke(text);

    internal void SetPairingStatus(string status)
    {
        PairingStatus = status;
        RaiseChanged();
    }

    internal Task<PairDecision> RequestApproval(PairRequestInfo info) =>
        ApprovalHandler is { } h ? h(info) : Task.FromResult(PairDecision.Reject);

    internal void AddDevice(PairedDevice device)
    {
        Listener listener;
        lock (_gate)
        {
            Config.Devices.Add(device);
            Config.Save();
            listener = new Listener(this, device, device.GetSecret(), false);
            _listeners.Add(listener);
        }
        listener.Start();
    }

    internal void FinishPairing(PairedDevice device)
    {
        // B2-A B-2: capture the pairing this finish belongs to. The delayed
        // cancel only fires if that same pairing is still current, so a
        // second pairing started within 1.5 s is no longer cancelled with
        // the first device's "Paired with ..." status.
        var pairing = CurrentPairing;
        _ = Task.Run(async () =>
        {
            await Task.Delay(1500);
            if (pairing is not null) CancelPairingIfCurrent(pairing, $"Paired with {device.Name}.");
        });
        RaiseNotice($"Paired with {device.Name}");
    }

    internal string HostRelayUrl => Config.HostRelayUrl;

    private sealed partial class Listener : IDisposable
    {
        private readonly HostEngine _engine;
        private readonly byte[] _secret;
        private readonly bool _pairMode;
        private readonly RelayLink _link;
        private readonly SemaphoreSlim _sealLock = new(1, 1);
        private readonly object _state = new();
        private readonly SemaphoreSlim _updateGate = new(1, 1);
        private AndroidUpdates? _updates;
        private Handshake? _handshake;
        private SecureSession? _session;
        private CancellationTokenSource? _stream;
        private StreamFlight? _flight;
        private double _remoteMotionAt;
        private bool _dragging;
        private int _failures;
        private bool _pairHandled;
        private DesktopBrowser? _browser;
        private int _browserBusy;
        // 100.017 — per-session stream state (spec §3.2/§4/§5.2). Held
        // here, one place per session, guarded by _state. lastSentSeq
        // is the forwarder cursor (the op's seq is window-relative, so
        // a dropped session must NOT resume from a stale cursor).
        // AckedSeq is the phone's highest contiguous acked seq — a
        // delivery receipt only; it never triggers a re-send.
        // EndSession drops all of it.
        private long _streamLastSentSeq;
        private long _streamAckedSeq;
        private DateTimeOffset _streamAckedAtUtc = DateTimeOffset.MinValue;
        private bool _streamSubscribed;
        internal bool StreamSubscribed { get { lock (_state) return _streamSubscribed; } }
        internal long StreamLastSentSeq { get { lock (_state) return _streamLastSentSeq; } }
        public void UpdateInfo() => _ = SendInfoAsync();

        public Listener(HostEngine engine, PairedDevice? device, byte[] secret, bool pairMode)
        {
            _engine = engine;
            Device = device;
            _secret = secret;
            _pairMode = pairMode;
            _link = new RelayLink(engine.HostRelayUrl, Kdf.RoomId(secret));
            _link.PeerJoined += OnPeerJoined;
            _link.PeerLeft += () => EndSession(false);
            _link.Message += OnMessage;
            _link.ConnectionChanged += engine.RaiseChanged;
        }

        public PairedDevice? Device { get; }
        public string DeviceName => Device?.Name ?? "New phone";
        public bool Connected => _link.Connected;
        public bool Streaming => _stream is { IsCancellationRequested: false };
        // B2-A B-6: surfaces RelayLink.LastError for the host RelayError aggregate.
        internal string? RelayError => _link.LastError;

        public void Start() => _link.Start();

        private void OnPeerJoined()
        {
            EndSession(false);
            if (_engine.Config.Paused) return;
            lock (_state)
            {
                _handshake = new Handshake(Role.Host, _secret);
                _ = _link.SendAsync(_handshake.CreateHello());
            }
        }

        public void EndSession(bool notifyPeer)
        {
            SecureSession? old;
            CancellationTokenSource? stream;
            DesktopBrowser? browser;
            lock (_state)
            {
                old = _session;
                browser = _browser;
                _browser = null;
                stream = _stream;
                _session = null;
                _updates?.Dispose();
                _updates = null;
                _stream = null;
                if (stream is not null) _engine._phoneDisplay.Leave(this);
                _handshake?.Dispose();
                _handshake = null;
                // 100.017 — drop the stream cursor + ack state with the
                // session (§3.2 step 9). No sealed frame is ever
                // replayed: the phone re-subscribes from its last seq
                // and the host re-fetches from the op.
                _streamLastSentSeq = 0;
                _streamAckedSeq = 0;
                _streamAckedAtUtc = DateTimeOffset.MinValue;
                _streamSubscribed = false;
            }
            browser?.End();
            // Fail closed for the isolated prototype when an authenticated phone
            // session drops; a pending approval must not survive reconnection.
            if (notifyPeer && old is not null)
            {
                try
                {
                    // B1: the Bye seal used to bypass _sealLock, so it could
                    // interleave with an in-flight seal and reuse a counter
                    // (= AES-GCM nonce). Take the lock like SendSecure does.
                    // The wait is bounded: if a send is stuck holding the
                    // lock, fall back to a direct Seal, which is itself
                    // serialized inside SecureSession and still cannot
                    // reuse a counter — EndSession must never hang.
                    byte[] record;
                    if (_sealLock.Wait(TimeSpan.FromSeconds(5)))
                    {
                        try { record = old.Seal(Msg.Bye, []); }
                        finally { _sealLock.Release(); }
                    }
                    else record = old.Seal(Msg.Bye, []);
                    _ = _link.SendAsync(record);
                }
                catch (CryptographicException)
                {
                }
            }
            if (stream is not null)
            {
                stream.Cancel();
                _engine.RaiseNotice($"{DeviceName} disconnected");
                _engine.RaiseChanged();
            }
        }

        private void OnMessage(byte[] data)
        {
            SecureSession? session;
            lock (_state)
            {
                session = _session;
                if (session is null)
                {
                    if (_handshake is null) return;
                    var established = _handshake.Complete(data);
                    if (established is null)
                    {
                        if (_pairMode && ++_failures >= MaxPairingFailures)
                        {
                            _engine.CancelPairing("Pairing stopped after too many failed attempts. Start again for a new code.");
                        }
                        return;
                    }
                    _handshake.Dispose();
                    _handshake = null;
                    _session = established;
                    if (!_pairMode) _updates = new AndroidUpdates(_engine.AndroidUpdateDirectory);
                    if (!_pairMode) _browser = _engine._desktopFactory();
                    if (!_pairMode) _ = SendInfoAsync();
                    if (!_pairMode) _ = SendStreamHelloAsync();
                    else _engine.SetPairingStatus($"Phone connected. Check that the code {established.ConfirmCode} matches.");
                    return;
                }
            }

            if (!session.TryOpen(data, out var type, out var payload)) return;
            switch (type)
            {
                case Msg.UpdateCheck or Msg.UpdateGet when !_pairMode && Device is not null && !_engine.Config.Paused:
                    if (payload.Length <= 4096) _ = HandleUpdate(session, type, payload);
                    break;
                case Msg.PairRequest when _pairMode:
                    HandlePairRequest(session, payload);
                    break;
                case Msg.Start when !_pairMode:
                    StartStreaming(payload);
                    break;
                case Msg.Ack:
                    _flight?.Ack(StreamFlight.Now);
                    break;
                case Msg.VideoAck when payload.Length == 8:
                    _videoWindow?.Ack(payload);
                    break;
                case Msg.VideoFailed:
                    _videoFailed = true;
                    break;
                case Msg.Input when !_pairMode:
                    if (Device is { Control: true } && !_engine.Config.Paused)
                    {
                        try
                        {
                            using var input = JsonDocument.Parse(payload);
                            var kind = input.RootElement.GetProperty("t").GetString();
                            if (kind == "down") _dragging = true;
                            if (kind == "up") _dragging = false;
                            if (kind == "scroll" || (kind == "move" && _dragging))
                                Volatile.Write(ref _remoteMotionAt, StreamFlight.Now);
                            InputInjector.Handle(payload);
                            if (kind == "focus_check" && input.RootElement.TryGetProperty("id", out var focusId) && focusId.TryGetInt64(out var id))
                                _ = ReportTextFocusAsync(session, id);
                            if (kind == "move" && _engine.InputMoveObservedForFixture is { } observer &&
                                input.RootElement.TryGetProperty("x", out var x) && x.TryGetDouble(out var px) &&
                                input.RootElement.TryGetProperty("y", out var y) && y.TryGetDouble(out var py)) observer(px, py);
                        }
                        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException) { }
                        catch (System.ComponentModel.Win32Exception ex)
                        {
                            _engine.RaiseNotice($"Remote input failed ({ex.NativeErrorCode}): {ex.Message}");
                        }
                    }
                    break;
                case Msg.DesktopList or Msg.DesktopOpen when !_pairMode:
                    HandleDesktop(session, type, payload);
                    break;
                case Msg.DesktopBack when !_pairMode:
                    HandleDesktopBack(session, payload);
                    break;
                case Msg.ForegroundWindow when !_pairMode:
                    HandleForegroundWindow(session, payload);
                    break;
                case Msg.StopRequest when !_pairMode:
                    // Legacy: message 18 carried foreground-window requests
                    // on previously released phone builds. Shape-sniffing
                    // keeps them working; new senders use Msg.ForegroundWindow (24).
                    if (IsForegroundWindowRequest(payload)) HandleForegroundWindow(session, payload);
                    else _ = HandlePhoneStopAsync(session, payload);
                    break;
                case Msg.ChatRequest when !_pairMode:
                    _ = HandleChat(session, payload);
                    break;
                case Msg.Stream when !_pairMode:
                    HandleStreamMessage(session, payload);
                    break;
                case Msg.Bye:
                    EndSession(false);
                    break;
            }
        }

        public void PublishChatHistory(JsonElement history)
        {
            SecureSession? session;
            lock (_state) session = _session;
            if (session is null || _pairMode || _engine.Config.Paused) return;
            _ = SendChatResult(session, new { op = "history", ok = true, result = history });
        }

        // 100.017 — stream forwarder, socket side (spec §3.2). Called
        // by the engine fan-out from MainForm's 1s tick. Sends only
        // events newer than this session's lastSentSeq, in seq order
        // (the op's order — never re-sorted), ≤100 per frame.
        // resynced=true means the tick's `since` predates the ring:
        // this session resyncs ONLY if its own cursor predates the
        // window's first seq; a session whose cursor already covers
        // the window just filters the events normally (a shared tick
        // fetch uses the oldest cursor, so a resync for one phone must
        // not resync the others). Exactly one stream.resynced, NO
        // partial catch-up — the phone does one full poll fetch (§4).
        // Backpressure: DROP, never queue — a failed send leaves
        // lastSentSeq unadvanced, so the next tick re-fetches from the
        // op (the ring is the store). Never throws out of the fan-out.
        internal void PublishStreamEvents(JsonElement events, long latestSeq, bool resynced)
        {
            SecureSession? session;
            List<byte[]> frames = [];
            long advanceTo = 0;
            lock (_state)
            {
                session = _session;
                if (session is null || _pairMode || !_streamSubscribed) return;
                if (!resynced && events.ValueKind != JsonValueKind.Array) return;
                if (resynced)
                {
                    var firstSeq = FirstEventSeq(events);
                    if (firstSeq <= 0 || _streamLastSentSeq < firstSeq)
                    {
                        frames = [StreamFrames.BuildResyncedFrame(latestSeq)];
                        advanceTo = latestSeq;
                    }
                    else resynced = false;
                }
                if (!resynced)
                {
                    var fresh = new List<JsonElement>();
                    long maxSeq = _streamLastSentSeq;
                    foreach (var item in events.EnumerateArray())
                    {
                        if (item.ValueKind != JsonValueKind.Object) continue;
                        if (!item.TryGetProperty("seq", out var seqValue) || seqValue.ValueKind != JsonValueKind.Number
                            || !seqValue.TryGetInt64(out var seq)) continue;
                        if (seq <= _streamLastSentSeq) continue;
                        fresh.Add(item);
                        if (seq > maxSeq) maxSeq = seq;
                    }
                    if (fresh.Count == 0) return;
                    frames = StreamFrames.BuildEventFrames(fresh, latestSeq);
                    advanceTo = maxSeq;
                }
            }
            _ = SendStreamFramesAsync(session, frames, advanceTo);
        }

        private static long FirstEventSeq(JsonElement events)
        {
            if (events.ValueKind != JsonValueKind.Array) return 0;
            foreach (var item in events.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.Object && item.TryGetProperty("seq", out var seqValue)
                    && seqValue.ValueKind == JsonValueKind.Number && seqValue.TryGetInt64(out var seq)) return seq;
            }
            return 0;
        }

        private async Task SendStreamFramesAsync(SecureSession session, List<byte[]> frames, long advanceTo)
        {
            try
            {
                foreach (var frame in frames)
                    if (!await SendSecure(Msg.Stream, frame, session)) return;
                lock (_state)
                {
                    if (ReferenceEquals(_session, session) && advanceTo > _streamLastSentSeq)
                        _streamLastSentSeq = advanceTo;
                }
            }
            catch (Exception ex) when (ex is ObjectDisposedException or OperationCanceledException or InvalidOperationException or System.Net.WebSockets.WebSocketException) { }
        }

        // 100.017 — inbound stream control frames (spec §3.4/§4/§5.2).
        // Read-only channel: subscribe/ack carry a cursor only — no
        // action, no approval, no grant change, no STOP change (§7).
        // Malformed frames are ignored, never thrown out of OnMessage.
        private void HandleStreamMessage(SecureSession session, byte[] payload)
        {
            try
            {
                using var doc = JsonDocument.Parse(payload);
                if (doc.RootElement.ValueKind != JsonValueKind.Object) return;
                var kind = doc.RootElement.TryGetProperty("kind", out var kindValue) && kindValue.ValueKind == JsonValueKind.String
                    ? kindValue.GetString() : null;
                if (kind == "stream.subscribe")
                {
                    var since = 0L;
                    if (doc.RootElement.TryGetProperty("since", out var sinceValue) && sinceValue.ValueKind == JsonValueKind.Number)
                        sinceValue.TryGetInt64(out since);
                    lock (_state) { _streamSubscribed = true; }
                    _ = HandleStreamSubscribeAsync(session, since);
                }
                else if (kind == "stream.ack")
                {
                    if (doc.RootElement.TryGetProperty("acked_seq", out var ackedValue) && ackedValue.ValueKind == JsonValueKind.Number
                        && ackedValue.TryGetInt64(out var acked) && acked > 0)
                    {
                        lock (_state)
                        {
                            // Receipt only: record the highest contiguous
                            // acked seq + receipt time. Never re-send on
                            // ack alone (§5.2).
                            if (acked > _streamAckedSeq)
                            {
                                _streamAckedSeq = acked;
                                _streamAckedAtUtc = DateTimeOffset.UtcNow;
                            }
                        }
                    }
                }
            }
            catch (JsonException) { }
        }

        // 100.017 — stream.subscribe (spec §3.2 step 7 / §4 step 3):
        // fetch the missed events from the op with the phone's `since`
        // and send them as stream.events, then go live (the 1s tick
        // takes over from this session's lastSentSeq). The call goes
        // through the same AssistantClient.CallAsync path the MainForm
        // tick uses — the engine's Chat IS MainForm's _chat (Program
        // wires engine.Chat = chat); there is no second op-call path.
        // resynced=true → exactly one stream.resynced, no partial
        // catch-up (§4 step 4). A failed fetch leaves _streamSubscribed
        // set, so the 1s tick still picks the session up.
        private async Task HandleStreamSubscribeAsync(SecureSession session, long since)
        {
            var chat = _engine.Chat;
            if (chat is null) return;
            JsonElement data;
            try { data = await chat.CallAsync(JsonSerializer.SerializeToElement(new { op = "runtime_stream", since = since, limit = 100 })); }
            catch (Exception ex) when (ex is IOException or InvalidOperationException or TimeoutException or OperationCanceledException or ObjectDisposedException or JsonException) { return; }
            lock (_state) { if (!ReferenceEquals(_session, session) || !_streamSubscribed) return; }
            var resynced = data.TryGetProperty("resynced", out var resyncedValue) && resyncedValue.ValueKind == JsonValueKind.True;
            var latestSeq = data.TryGetProperty("latest_seq", out var latestValue) && latestValue.ValueKind == JsonValueKind.Number
                && latestValue.TryGetInt64(out var latest) ? latest : 0L;
            if (resynced)
            {
                try
                {
                    if (await SendSecure(Msg.Stream, StreamFrames.BuildResyncedFrame(latestSeq), session))
                        lock (_state) { if (ReferenceEquals(_session, session)) _streamLastSentSeq = latestSeq; }
                }
                catch (Exception ex) when (ex is ObjectDisposedException or OperationCanceledException or InvalidOperationException or System.Net.WebSockets.WebSocketException) { }
                return;
            }
            if (data.TryGetProperty("events", out var events) && events.ValueKind == JsonValueKind.Array && events.GetArrayLength() > 0)
                PublishStreamEvents(events, latestSeq, false);
        }

        // 100.017 — stream.hello on session establish (spec §3.4/§4
        // step 1): latest_seq from this session's cursor, sources from
        // the last loopback op result MainForm published ({} when no
        // tick has run yet — the phone's subscribe fetch returns fresh
        // data either way). Never throws; a failed hello just means the
        // phone subscribes from its own latest_seq.
        private async Task SendStreamHelloAsync()
        {
            SecureSession? session;
            long latestSeq;
            lock (_state)
            {
                session = _session;
                latestSeq = _streamLastSentSeq;
            }
            var sources = _engine.StreamSources;
            if (session is null || _pairMode) return;
            try { await SendSecure(Msg.Stream, StreamFrames.BuildHelloFrame(latestSeq, sources), session); }
            catch (Exception ex) when (ex is ObjectDisposedException or OperationCanceledException or InvalidOperationException or System.Net.WebSockets.WebSocketException) { }
        }

        private async Task SendChatResult(SecureSession session, object response)
        {
            try
            {
                lock (_state) { if (!ReferenceEquals(_session, session)) return; }
                await SendSecure(Msg.ChatResult, JsonSerializer.SerializeToUtf8Bytes(response));
            }
            catch (Exception ex) when (ex is ObjectDisposedException or OperationCanceledException or InvalidOperationException or System.Net.WebSockets.WebSocketException) { }
        }

        private async Task HandleChat(SecureSession session, byte[] payload)
        {
            string? rpc = null;
            string? op = null;
            try
            {
                if (_engine.Config.Paused || Device is null) return;
                using var doc = JsonDocument.Parse(payload);
                rpc = doc.RootElement.GetProperty("rpc").GetString();
                op = doc.RootElement.GetProperty("op").GetString();
                if (op is "jetstream_status" or "jetstream_jobs" or "jetstream_job" or "jetstream_outcome" or "jetstream_action")
                {
                    bool JetstreamAllowed()
                    {
                        lock (_engine._gate) lock (_state)
                            return ReferenceEquals(_session, session) && !_engine.Config.Paused && Device is not null &&
                                _engine.Config.Devices.Contains(Device) && (op != "jetstream_action" || Device.Control);
                    }
                    using var client = new JetstreamClient();
                    var data = await client.CallAsync(doc.RootElement.Clone(), JetstreamAllowed);
                    if (!JetstreamAllowed()) throw new InvalidOperationException("Phone session ended.");
                    await SendChatResult(session, new { rpc, op, ok = true, result = data });
                    return;
                }
                if (op is "access_status" or "access_set")
                {
                    bool GuardAccess(Func<bool> change)
                    {
                        lock (_engine._gate) lock (_state)
                            return ReferenceEquals(_session, session) && !_engine.Config.Paused && Device is not null &&
                                _engine.Config.Devices.Contains(Device) &&
                                (op == "access_status" || !doc.RootElement.GetProperty("enabled").GetBoolean() || Device.Control) && change();
                    }
                    if (!GuardAccess(() => true)) throw new InvalidOperationException("This phone cannot change PC access.");
                    var accessChat = _engine.Chat as AssistantClient ?? throw new InvalidOperationException("PC access is unavailable.");
                    var accessResult = await accessChat.CallFromPhoneAsync(doc.RootElement.Clone(), GuardAccess);
                    await SendChatResult(session, new { rpc, op, ok = true, result = accessResult });
                    return;
                }
                if (op == "jett_ack")
                {
                    if (Device is null || !doc.RootElement.TryGetProperty("reply_id", out var replyElement)
                        || !TryUuidV4(replyElement, out var replyId))
                        throw new InvalidOperationException("Invalid reply display acknowledgment.");
                    var acknowledged = _engine.AcknowledgeJettPhoneReply(Device.Id, replyId);
                    await SendChatResult(session, new { rpc, op, ok = acknowledged, result = new { acknowledged } });
                    return;
                }
                if (op is "approval_list" or "approval_resolve")
                {
                    if (op == "approval_resolve")
                    {
                        if (!doc.RootElement.TryGetProperty("approval_id", out var approval) || !TryUuidV4(approval, out var approvalId) ||
                            !doc.RootElement.TryGetProperty("approved", out var approved) || approved.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                            throw new InvalidOperationException("Invalid approval decision.");
                        bool accepted;
                        lock (_engine._gate) lock (_state)
                        {
                            if (!ReferenceEquals(_session, session) || _engine.Config.Paused || Device is null || !_engine.Config.Devices.Contains(Device))
                                throw new InvalidOperationException("This phone cannot approve computer-control actions.");
                            accepted = _engine.ResolveApproval(approvalId, approved.GetBoolean());
                        }
                        await SendChatResult(session, new { rpc, op, ok = accepted, result = new { accepted } });
                        return;
                    }
                    object[] items;
                    object[] tasks;
                    lock (_engine._gate) lock (_state)
                    {
                        if (!ReferenceEquals(_session, session) || _engine.Config.Paused || Device is null || !_engine.Config.Devices.Contains(Device))
                            throw new InvalidOperationException("This phone cannot review computer-control actions.");
                        items = _engine.AllApprovals().Select(a => (object)new { id = a.Id.ToString("D"), taskId = a.TaskId.ToString("D"), operation = a.Operation, target = a.Target, purpose = a.Purpose, expiresUtc = a.ExpiresUtc }).ToArray();
                        tasks = _engine.AssistantTasks.Select(task => (object)new { taskId = task.TaskId, turnId = task.TurnId, assistant = task.Assistant, model = task.Model, active = task.Active, runningActions = task.RunningActions }).ToArray();
                    }
                    await SendChatResult(session, new { rpc, op, ok = true, result = new { approvals = items, tasks } });
                    return;
                }
                if (op == "resume_chat")
                {
                    Task<JsonElement> resumePending;
                    lock (_engine._gate) lock (_state)
                    {
                        if (!ReferenceEquals(_session, session) || Device is null || !_engine.Config.Devices.Contains(Device) || _engine.Config.Paused)
                            throw new InvalidOperationException("Phone session is no longer active.");
                        bool Guard(Func<bool> admit)
                        {
                            lock (_engine._gate) lock (_state)
                                return ReferenceEquals(_session, session) && !_engine.Config.Paused && Device is not null && _engine.Config.Devices.Contains(Device) && admit();
                        }
                        var assistant = _engine.Chat as AssistantClient ?? throw new InvalidOperationException("PC chat service is not available");
                        resumePending = assistant.CallFromPhoneAsync(JsonSerializer.SerializeToElement(new { op = "resume_chat_local" }), Guard);
                    }
                    var resumed = await resumePending;
                    await SendChatResult(session, new { rpc, op, ok = true, result = resumed });
                    return;
                }
                if (op is not ("catalog" or "submit" or "history" or "get" or "runtime_stream" or "route_suggest" or "router_settings" or "timing" or "warm_start" or "stop_agent_work" or "stop_chat" or "restart_chat")) throw new InvalidOperationException("Use the PC to resume chat");
                var chat = _engine.Chat ?? throw new InvalidOperationException("PC chat service is not available");
                Task<JsonElement> pending;
                if (op is "stop_chat" or "restart_chat")
                {
                    lock (_engine._gate) lock (_state)
                    {
                        if (!ReferenceEquals(_session, session) || _engine.Config.Paused || Device is null || !_engine.Config.Devices.Contains(Device))
                            throw new InvalidOperationException("Phone session is no longer active.");
                        bool Guard(Func<bool> admit)
                        {
                            lock (_engine._gate) lock (_state)
                                return ReferenceEquals(_session, session) && !_engine.Config.Paused && Device is not null && _engine.Config.Devices.Contains(Device) && admit();
                        }
                        pending = chat is AssistantClient client
                            ? client.CallFromPhoneAsync(doc.RootElement.Clone(), Guard)
                            : throw new InvalidOperationException("Phone chat control is unavailable.");
                    }
                }
                else if (op == "submit" && doc.RootElement.TryGetProperty("agent", out var agent) && agent.ValueKind == JsonValueKind.True)
                {
                    if (!doc.RootElement.TryGetProperty("assistant", out var assistant) || assistant.ValueKind != JsonValueKind.String ||
                        assistant.GetString() is not ("local" or "muse" or "codex"))
                        throw new InvalidOperationException("Computer tasks are unavailable for this assistant.");
                    lock (_engine._gate) lock (_state)
                    {
                        if (!ReferenceEquals(_session, session) || _engine.Config.Paused || Device is null || !_engine.Config.Devices.Contains(Device))
                            throw new InvalidOperationException("This phone is not permitted to start computer-control tasks.");
                        // Recheck
                        // the phone session under both locks immediately at admission.
                        bool Guard(Func<bool> admit)
                        {
                            lock (_engine._gate) lock (_state)
                                return ReferenceEquals(_session, session) && !_engine.Config.Paused && Device is not null && _engine.Config.Devices.Contains(Device) && admit();
                        }
                        pending = chat is AssistantClient client ? client.CallFromPhoneAsync(doc.RootElement.Clone(), Guard) : chat.CallAsync(doc.RootElement.Clone());
                    }
                }
                else pending = chat.CallAsync(doc.RootElement.Clone());
                var result = await pending;
                await SendChatResult(session, new { rpc, op, ok = true, result });
            }
            catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException or IOException or TimeoutException)
            {
                await SendChatResult(session, new { rpc, op, ok = false, error = ex.Message });
            }
        }

        private async Task HandleUpdate(SecureSession session, byte type, byte[] payload)
        {
            // Bound queued work to one request per session, independently of frame credits.
            if (!await _updateGate.WaitAsync(0)) return;
            try
            {
                AndroidUpdates? updates;
                lock (_state) updates = ReferenceEquals(session, _session) ? _updates : null;
                if (updates is null) return;
                try
                {
                    var response = await Task.Run(() => type == Msg.UpdateCheck ? updates.Check(payload) : updates.Chunk(payload));
                    await SendSecure(type == Msg.UpdateCheck ? Msg.UpdateInfo : Msg.UpdateChunk, response, session);
                }
                catch (Exception ex) when (ex is InvalidDataException or IOException or JsonException or ArgumentException or InvalidOperationException or KeyNotFoundException or UnauthorizedAccessException)
                {
                    await SendSecure(Msg.UpdateError, JsonSerializer.SerializeToUtf8Bytes(new {
                        error = "Update unavailable. Check again.",
                        request = Convert.ToBase64String(payload)
                    }), session);
                }
            }
            catch (Exception ex) when (ex is OperationCanceledException or System.Net.WebSockets.WebSocketException or ObjectDisposedException) { }
            finally { _updateGate.Release(); }
        }

        private async Task SendInfoAsync()
        {
            var b = ScreenCapturer.Bounds;
            var json = JsonSerializer.SerializeToUtf8Bytes(new
            {
                host = _engine.Config.DeviceName,
                w = b.Width,
                h = b.Height,
                control = Device?.Control ?? false,
                androidUpdates = 1,
                desktopBrowser = 1,
                fileBrowser = 3,
                fullPcAccess = _engine._fullAccessInitialized ? 1 : 0,
                desktopBack = _browser?.SupportsBack == true ? 1 : 0,
                foregroundWindow = _browser?.SupportsForeground == true ? 1 : 0,
                jettActionsEnabled = _engine.JettActionsEnabled,
                localAiTasks = _engine.LocalAssistantTasksAvailable,
                adaptiveVideo = GpuVideoEncoder.Available ? 1 : 0,
            });
            await SendSecure(Msg.Info, json);
        }

        // Two previously released phone builds use message 18 for different
        // operations. Their strict, disjoint JSON shapes preserve both behaviors.
        // New senders use Msg.ForegroundWindow (24); this test exists only
        // for the legacy 18 senders.
        private static bool IsForegroundWindowRequest(byte[] payload)
        {
            if (payload.Length > 4096) return false;
            try
            {
                using var document = JsonDocument.Parse(payload);
                var root = document.RootElement;
                if (root.ValueKind != JsonValueKind.Object) return false;
                var names = root.EnumerateObject().Select(p => p.Name).ToArray();
                return names.Length == 2 && names.Contains("requestId", StringComparer.Ordinal)
                    && names.Contains("action", StringComparer.Ordinal);
            }
            catch (JsonException) { return false; }
        }

        private async Task HandlePhoneStopAsync(SecureSession session, byte[] payload)
        {
            // STOP latch removed (Phil 2026-11-09): the phone STOP button
            // no longer stops Muse or sets any latch. Acknowledge with
            // muse_stop=not_enabled so the phone clears its banner, but
            // nothing is stopped and no latch is set.
            // B2: the reply MUST carry "latched" (and echo "stop_id" when
            // the request had one). Android routes a type-19 reply to its
            // stop handler only when the payload has "latched", and clears
            // the pending stop only when "stop_id" matches. A bare
            // {"muse_stop":"not_enabled"} matches neither branch and is
            // silently dropped on the phone.
            string? stopId = null;
            try
            {
                using var request = JsonDocument.Parse(payload);
                if (request.RootElement.ValueKind == JsonValueKind.Object &&
                    request.RootElement.TryGetProperty("stop_id", out var id) && id.ValueKind == JsonValueKind.String)
                    stopId = id.GetString();
            }
            catch (JsonException) { }
            try
            {
                var reply = stopId is null
                    ? JsonSerializer.SerializeToUtf8Bytes(new { muse_stop = "not_enabled", latched = false }, DesktopWire.Json)
                    : JsonSerializer.SerializeToUtf8Bytes(new { muse_stop = "not_enabled", latched = false, stop_id = stopId }, DesktopWire.Json);
                await SendSecure(Msg.StopStatus, reply, session);
            }
            catch (Exception ex) when (ex is ObjectDisposedException or OperationCanceledException or InvalidOperationException or System.Net.WebSockets.WebSocketException)
            {
            }
        }

        private void HandleForegroundWindow(SecureSession session, byte[] payload)
        {
            var browser = _browser;
            if (browser is null) return;
            var id = Guid.Empty;
            ForegroundWindowRequest request;
            try
            {
                request = JsonSerializer.Deserialize<ForegroundWindowRequest>(payload, DesktopWire.Json) ?? throw new JsonException();
                id = request.RequestId;
                if (id == Guid.Empty || request.Action is not ("minimize" or "close")) throw new JsonException();
            }
            catch (Exception ex) when (ex is JsonException or InvalidOperationException) { _ = Reply(new(id, "malformed")); return; }
            if (Interlocked.CompareExchange(ref _browserBusy, 1, 0) != 0) { _ = Reply(new(id, "busy")); return; }
            _ = Task.Run(async () =>
            {
                try
                {
                    ForegroundWindowResult result;
                    lock (_engine._gate) lock (_state)
                        result = browser.Foreground(request, () => ReferenceEquals(_session, session) && ReferenceEquals(_browser, browser)
                            && !_engine.Config.Paused && Device is { Control: true } && _engine.Config.Devices.Contains(Device));
                    await Reply(result);
                }
                catch (Exception ex) when (ex is InvalidOperationException or ObjectDisposedException) { await Reply(new(id, "unknown")); }
                finally { Interlocked.Exchange(ref _browserBusy, 0); }
            });
            async Task Reply(ForegroundWindowResult result)
            {
                // Legacy wire value: shipped phone builds listen for the
                // foreground result on 19 (Msg.StopStatus). Switch to
                // Msg.ForegroundWindowResult (25) only together with the
                // Android side (B2-G); sending 25 today would be dropped.
                try { await SendSecure(Msg.StopStatus, JsonSerializer.SerializeToUtf8Bytes(result, DesktopWire.Json), session); }
                catch (Exception ex) when (ex is System.Net.WebSockets.WebSocketException or ObjectDisposedException or InvalidOperationException or OperationCanceledException) { }
            }
        }

        private async Task ReportTextFocusAsync(SecureSession session, long id)
        {
            await Task.Delay(150);
            if (Device is not { Control: true } || _engine.Config.Paused) return;
            var editable = await Task.Run(() => {
                try {
                    var element = System.Windows.Automation.AutomationElement.FocusedElement;
                    if (element is null || !element.Current.IsEnabled) return false;
                    if (element.TryGetCurrentPattern(System.Windows.Automation.ValuePattern.Pattern, out var value))
                        return !((System.Windows.Automation.ValuePattern)value).Current.IsReadOnly;
                    return element.Current.ControlType == System.Windows.Automation.ControlType.Edit;
                } catch (Exception ex) when (ex is System.Windows.Automation.ElementNotAvailableException or InvalidOperationException or System.Runtime.InteropServices.COMException) { return false; }
            });
            if (Device is not { Control: true } || _engine.Config.Paused) return;
            try { await SendSecure(Msg.TextFocus, JsonSerializer.SerializeToUtf8Bytes(new { id, editable }), session); }
            catch (Exception ex) when (ex is System.Net.WebSockets.WebSocketException or ObjectDisposedException or InvalidOperationException or OperationCanceledException) { }
        }

        // B2-A B-4: reports whether the sealed frame actually went out, so
        // the pairing accept path can tell a dropped connection from a
        // delivered one. Callers that ignore the result are unaffected.
        private async Task<bool> SendSecure(byte type, byte[] payload, SecureSession? expected = null)
        {
            await _sealLock.WaitAsync();
            try
            {
                var s = _session;
                if (s is null || (expected is not null && !ReferenceEquals(s, expected))) return false;
                return await _link.SendAsync(s.Seal(type, payload));
            }
            finally
            {
                _sealLock.Release();
            }
        }

        private void HandleDesktopBack(SecureSession session, byte[] payload)
        {
            var browser = _browser;
            if (browser is null) return;
            var id = Guid.Empty;
            try
            {
                if (payload.Length > 4096) throw new JsonException();
                using var doc = JsonDocument.Parse(payload);
                if (doc.RootElement.EnumerateObject().Count() != 1) throw new JsonException();
                id = (JsonSerializer.Deserialize<DesktopBackRequest>(payload, DesktopWire.Json) ?? throw new JsonException()).RequestId;
                if (id == Guid.Empty) throw new JsonException();
            }
            catch (Exception ex) when (ex is JsonException or InvalidOperationException) { _ = Reply(new(id, "malformed", null)); return; }
            if (Interlocked.CompareExchange(ref _browserBusy, 1, 0) != 0) { _ = Reply(new(id, "busy", null)); return; }
            _ = Task.Run(async () =>
            {
                try
                {
                    DesktopBackResult result;
                    lock (_engine._gate) lock (_state)
                        result = browser.Back(new(id), () => ReferenceEquals(_session, session) && ReferenceEquals(_browser, browser)
                            && !_engine.Config.Paused && Device is { Control: true } && _engine.Config.Devices.Contains(Device));
                    await Reply(result);
                }
                catch (Exception ex) when (ex is InvalidOperationException or ObjectDisposedException)
                { await Reply(new(id, "unknown", null)); }
                finally { Interlocked.Exchange(ref _browserBusy, 0); }
            });
            async Task Reply(DesktopBackResult result)
            {
                try { await SendSecure(Msg.DesktopBackResult, JsonSerializer.SerializeToUtf8Bytes(result, DesktopWire.Json), session); }
                catch (Exception ex) when (ex is System.Net.WebSockets.WebSocketException or ObjectDisposedException or InvalidOperationException or OperationCanceledException) { }
            }
        }

        private void HandleDesktop(SecureSession session, byte type, byte[] payload)
        {
            var browser = _browser;
            if (browser is null) return;
            Guid id = Guid.Empty;
            DesktopListRequest? list = null;
            DesktopOpenRequest? open = null;
            try
            {
                if (payload.Length > 4096) throw new JsonException();
                using var document = JsonDocument.Parse(payload);
                var names = new HashSet<string>(StringComparer.Ordinal);
                foreach (var property in document.RootElement.EnumerateObject())
                    if (!names.Add(property.Name)) throw new JsonException();
                if (type == Msg.DesktopList) { list = JsonSerializer.Deserialize<DesktopListRequest>(payload, DesktopWire.Json) ?? throw new JsonException(); id = list.RequestId; }
                else { open = JsonSerializer.Deserialize<DesktopOpenRequest>(payload, DesktopWire.Json) ?? throw new JsonException(); id = open.RequestId; }
            }
            catch (Exception ex) when (ex is JsonException or InvalidOperationException)
            {
                _ = Reply("malformed");
                return;
            }
            if (Interlocked.CompareExchange(ref _browserBusy, 1, 0) != 0) { _ = Reply("busy"); return; }
            _ = Task.Run(async () =>
            {
                try
                {
                    object result;
                    bool Active() => ReferenceEquals(_session, session) && ReferenceEquals(_browser, browser) && !_engine.Config.Paused;
                    if (list is not null) result = browser.List(list, Active);
                    else
                    {
                        // Permission changes, revocation and session teardown cannot interleave with launch.
                        lock (_engine._gate) lock (_state)
                            result = browser.Open(open!, () => Active() && Device is { Control: true } && _engine.Config.Devices.Contains(Device));
                    }
                    var error = result is DesktopListResponse l ? l.Error : ((DesktopOpenResult)result).Error;
                    if (error is not null) _engine.RaiseNotice($"Desktop request {id}: {error}");
                    await SendSecure(type == Msg.DesktopList ? Msg.DesktopListResult : Msg.DesktopOpenResult,
                        JsonSerializer.SerializeToUtf8Bytes(result, DesktopWire.Json), session);
                }
                catch (Exception ex) when (ex is System.Net.WebSockets.WebSocketException or ObjectDisposedException or InvalidOperationException or OperationCanceledException)
                { _engine.RaiseNotice($"Desktop request {id}: connection ended"); }
                finally { Interlocked.Exchange(ref _browserBusy, 0); }
            });
            async Task Reply(string error)
            {
                try
                {
                    object result = type == Msg.DesktopList
                        ? new DesktopListResponse(id, null, "Desktop", null, [], error)
                        : new DesktopOpenResult(id, error);
                    await SendSecure(type == Msg.DesktopList ? Msg.DesktopListResult : Msg.DesktopOpenResult,
                        JsonSerializer.SerializeToUtf8Bytes(result, DesktopWire.Json), session);
                }
                catch (Exception ex) when (ex is System.Net.WebSockets.WebSocketException or ObjectDisposedException or InvalidOperationException or OperationCanceledException) { }
            }
        }

        private void HandlePairRequest(SecureSession session, byte[] payload)
        {
            if (_pairHandled) return;
            _pairHandled = true;
            var name = "Phone";
            try
            {
                using var doc = JsonDocument.Parse(payload);
                name = Sanitize(doc.RootElement.GetProperty("name").GetString());
            }
            catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException)
            {
            }

            _ = Task.Run(async () =>
            {
                var decision = await _engine.RequestApproval(new PairRequestInfo(name, session.ConfirmCode));
                if (decision == PairDecision.Reject)
                {
                    await SendSecure(Msg.PairReject, []);
                    _engine.CancelPairing("Pairing was rejected.");
                    return;
                }

                var secret = RandomNumberGenerator.GetBytes(32);
                var device = new PairedDevice
                {
                    Name = name,
                    ProtectedSecret = PairedDevice.Protect(secret),
                    Control = decision == PairDecision.Control,
                };
                var accept = JsonSerializer.SerializeToUtf8Bytes(new
                {
                    secret = Convert.ToBase64String(secret),
                    control = device.Control,
                    host = _engine.Config.DeviceName,
                });
                _engine.AddDevice(device);
                // B2-A B-4: the device was persisted (and its listener
                // started) before the accept was sent, so a dropped
                // connection used to leave an orphan device whose secret
                // nobody holds. If the accept did not go out, roll the
                // device back instead of leaving it in the list forever.
                if (!await SendSecure(Msg.PairAccept, accept))
                {
                    _engine.Revoke(device);
                    _engine.CancelPairing("Pairing failed — the phone connection was lost before the pairing was confirmed.");
                    return;
                }
                _engine.FinishPairing(device);
            });
        }

        private static string Sanitize(string? name)
        {
            var clean = new string((name ?? "").Where(c => !char.IsControl(c)).ToArray()).Trim();
            if (clean.Length > 40) clean = clean[..40];
            return clean.Length == 0 ? "Phone" : clean;
        }

        private void StartStreaming(byte[] payload)
        {
            CancellationTokenSource cts;
            lock (_state)
            {
                if (_session is null) return;
                var hasOrientation = payload.Length is 1 or 2 && payload[0] <= 1;
                var landscape = hasOrientation && payload[0] == 1;
                if (_stream is not null)
                {
                    if (hasOrientation)
                    {
                        Volatile.Write(ref _remoteMotionAt, StreamFlight.Now);
                        _engine._phoneDisplay.Update(this, landscape);
                    }
                    return;
                }
                _engine._phoneDisplay.Enter(this, landscape);
                cts = _stream = new CancellationTokenSource();
                _flight = new StreamFlight();
                _dragging = false;
                _remoteMotionAt = 0;
                _videoFailed = false;
            }
            _engine.RaiseNotice($"{DeviceName} connected");
            _engine.RaiseChanged();
            var flight = _flight!;
            var session = _session!;
            var video = payload.Length == 2 && payload[1] == 1 && GpuVideoEncoder.Available;
            _ = Task.Run(() => video ? HybridStreamLoop(cts.Token, flight, session) : StreamLoop(cts.Token, flight, session));
        }

        private async Task<bool> SendFrame(SecureSession session, StreamFlight flight, EncodedDesktop frame,
            CancellationToken ct, bool refinement, double motionAt, double motionAge)
        {
            await _sealLock.WaitAsync(ct);
            try
            {
                if (ct.IsCancellationRequested || !ReferenceEquals(_session, session) || _engine.Config.Paused ||
                    (refinement && Volatile.Read(ref _remoteMotionAt) > motionAt)) return false;
                flight.Sent(StreamFlight.Now, frame.Bytes.Length);
                await _link.SendAsync(session.Seal(Msg.Frame, frame.Bytes));
                var log = Environment.GetEnvironmentVariable("DESKHAND_FPS_LOG");
                if (log is not null)
                    try { File.AppendAllText(log, $"{DateTime.UtcNow:O} frame state={(refinement ? "settled" : "motion")} dimensions={frame.Width}x{frame.Height} quality={frame.Quality} bytes={frame.Bytes.Length} motion_age_ms={motionAge:F1} ack_ms={flight.AckMilliseconds:F1}\r\n"); }
                    catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
                return true;
            }
            finally { _sealLock.Release(); }
        }

        private async Task StreamLoop(CancellationToken ct, StreamFlight flight, SecureSession session)
        {
            using var capture = _engine._captureFactory();
            var policy = new AdaptiveStream();
            Task<bool>? sending = null;
            var fpsLog = Environment.GetEnvironmentVariable("DESKHAND_FPS_LOG");
            var windowStart = StreamFlight.Now;
            var sent = 0;
            long skipped = 0;
            var lastFrame = new EncodedDesktop([], 0, 0, 0);
            var encodeMs = 0.0;
            var lastWasRefinement = false;
            var remoteObserved = 0.0;
            var failureReported = false;
            try
            {
                while (!ct.IsCancellationRequested)
                {
                    var loopAt = StreamFlight.Now;
                    if (sending is { IsCompleted: true })
                    {
                        if (await sending)
                        {
                            sent++;
                            if (!lastWasRefinement)
                                policy.Sample(flight.AckMilliseconds, encodeMs, lastFrame.Bytes.Length, flight.BytesPerSecond);
                        }
                        sending = null;
                    }
                    try
                    {
                        var change = capture.Capture();
                        var now = StreamFlight.Now;
                        var remoteAt = Volatile.Read(ref _remoteMotionAt);
                        policy.Observe(change, now, remoteAt > remoteObserved);
                        remoteObserved = remoteAt;
                        if (change.Resized)
                        {
                            flight.ResetEstimate();
                            await SendInfoAsync();
                        }
                        // Batch 2 (100.030, B1): a JPEG frame whose ACK never
                        // arrives must not block all later frames forever.
                        flight.RetireStalled(now);
                        if (policy.Due(now))
                        {
                            if (sending is null && flight.Available)
                            {
                                var refinement = !policy.Motion(now);
                                var profile = policy.Profile(now, capture.Width);
                                var encodeAt = StreamFlight.Now;
                                var frame = capture.Encode(profile.Width, profile.Quality,
                                    AdaptiveStream.ByteBudget(refinement, flight.BytesPerSecond));
                                encodeMs = StreamFlight.Now - encodeAt;
                                // Detect motion during expensive encoding before a quality frame enters the wire.
                                DesktopChange localizedDuringEncode = default;
                                if (refinement)
                                {
                                    var newer = capture.Capture();
                                    var newRemote = Volatile.Read(ref _remoteMotionAt);
                                    policy.Observe(newer, StreamFlight.Now, newRemote > remoteObserved);
                                    remoteObserved = newRemote;
                                    if (policy.Motion(StreamFlight.Now)) { skipped++; continue; }
                                    localizedDuringEncode = newer;
                                }
                                lastFrame = frame;
                                lastWasRefinement = refinement;
                                policy.Sent(StreamFlight.Now, refinement);
                                policy.Observe(localizedDuringEncode, StreamFlight.Now);
                                sending = SendFrame(session, flight, frame, ct, refinement, remoteObserved, policy.MotionAge(StreamFlight.Now));
                            }
                            else skipped++; // The reusable raw capture is the sole latest pending image.
                        }
                        if (now - windowStart >= 2000)
                        {
                            if (fpsLog is not null)
                                File.AppendAllText(fpsLog, $"{DateTime.UtcNow:O} state={(policy.Motion(now) ? "motion" : "settled")} sent_fps={sent * 1000 / (now - windowStart):F1} dimensions={lastFrame.Width}x{lastFrame.Height} quality={lastFrame.Quality} bytes={lastFrame.Bytes.Length} ack_ms={flight.AckMilliseconds:F1} encode_ms={encodeMs:F1} throughput_Bps={flight.BytesPerSecond:F0} in_flight={flight.Count} skipped={skipped} tier={policy.Tier}\r\n");
                            windowStart = now; sent = 0; skipped = 0;
                        }
                    }
                    catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or ExternalException or InvalidOperationException)
                    {
                        if (!failureReported)
                        {
                            failureReported = true;
                            _engine.RaiseNotice($"Desktop capture failed ({ex.GetType().Name}, 0x{ex.HResult:X8}): {ex.Message}");
                        }
                    }
                    var wait = policy.PollMilliseconds(StreamFlight.Now) - (int)(StreamFlight.Now - loopAt);
                    if (wait > 0) await Task.Delay(wait, ct);
                }
            }
            catch (Exception ex) when (ex is OperationCanceledException or System.Net.WebSockets.WebSocketException or ObjectDisposedException) { }
            finally
            {
                if (sending is not null)
                    try { await sending; }
                    catch (Exception ex) when (ex is OperationCanceledException or System.Net.WebSockets.WebSocketException or ObjectDisposedException or InvalidOperationException) { }
            }
        }

        public void Dispose()
        {
            EndSession(true);
            _link.Dispose();
        }
    }
}

// 100.017 (Worker B) — stream frame builders (spec §3.4). Pure
// functions over the op's envelope, verbatim: no new event fields, no
// token/file-contents/screenshot data (the op already excludes them).
// The Listener sends these via SendSecure(Msg.Stream, ...); the spec
// test (AssistantHostRpcSpecs.StreamForwarder) proves the chunking /
// resync shape without a live socket. Max 100 events per frame, seq
// order preserved as received; a resynced frame carries no events.
internal static class StreamFrames
{
    internal const int MaxEventsPerFrame = 100;

    internal static List<byte[]> BuildEventFrames(IEnumerable<JsonElement> events, long latestSeq)
    {
        var frames = new List<byte[]>();
        var batch = new List<JsonElement>();
        foreach (var item in events)
        {
            batch.Add(item);
            if (batch.Count >= MaxEventsPerFrame)
            {
                frames.Add(JsonSerializer.SerializeToUtf8Bytes(new { kind = "stream.events", events = batch, latest_seq = latestSeq }));
                batch = [];
            }
        }
        if (batch.Count > 0)
            frames.Add(JsonSerializer.SerializeToUtf8Bytes(new { kind = "stream.events", events = batch, latest_seq = latestSeq }));
        return frames;
    }

    internal static byte[] BuildResyncedFrame(long latestSeq) =>
        JsonSerializer.SerializeToUtf8Bytes(new { kind = "stream.resynced", latest_seq = latestSeq });

    internal static byte[] BuildHelloFrame(long latestSeq, JsonElement? sources) =>
        sources is { } s && s.ValueKind == JsonValueKind.Object
            ? JsonSerializer.SerializeToUtf8Bytes(new { kind = "stream.hello", latest_seq = latestSeq, sources = s })
            : JsonSerializer.SerializeToUtf8Bytes(new { kind = "stream.hello", latest_seq = latestSeq, sources = new Dictionary<string, string>() });
}
