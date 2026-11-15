using System.Drawing;
using System.Text.Json;
using Deskhand.Relay;
using Microsoft.Win32;
using System.Windows.Forms;

namespace Deskhand.Windows.App;

public sealed partial class MainForm : Form
{
    private readonly HostEngine _engine;
    private readonly AppConfig _config;
    private readonly BuiltInRelay _relay;
    private readonly Button _reviewApprovalButton = new() { Text = "Review AI approval", AutoSize = true, Enabled = false, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48), FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
    private readonly Label _authorityStatus = new() { AutoSize = true, Padding = new Padding(6, 8, 0, 0) };
    private readonly Label _status = new() { Dock = DockStyle.Fill, Font = new Font("Segoe UI", 14, FontStyle.Bold), TextAlign = ContentAlignment.MiddleLeft };
    private readonly Label _statusDot = new() { Dock = DockStyle.Fill, Font = new Font("Segoe UI", 10), Text = "\u25cf", TextAlign = ContentAlignment.MiddleCenter, ForeColor = Color.FromArgb(91, 107, 122) };
    private readonly Label _relayStatus = new() { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, Font = new Font("Segoe UI", 8), AutoEllipsis = true };
    private readonly Label _relayDot = new() { Dock = DockStyle.Fill, Font = new Font("Segoe UI", 10), Text = "\u25cf", TextAlign = ContentAlignment.MiddleCenter, ForeColor = Color.FromArgb(91, 107, 122) };
    private DateTime _relayLastOk = DateTime.MinValue;
    // 100.015 (Worker A) — relay-health assessment state: the last
    // RelayHealth assessment. 100.040: the tunnel echo-failure
    // counter and the re-mint seams are gone with the tunnel.
    private RelayAssessment? _relayAssessment;
    private DateTime _relayHealthLastCheckedUtc = DateTime.MinValue;
    private bool _relayHealthEvaluating;
    // 100.037 (Worker B) -- Muse-worker watchdog line (BUILD-SPEC-100037
    // §7.1). The 4th row of the relay panel: its own dot + label, plus
    // the manual Reconnect button. This line is the MUSE WORKER line --
    // the relay line above it is the phone link; the two are different
    // sessions and are never blended into one claim. The line renders
    // state the service publishes (worker.* stream events + the
    // sources.worker key in the same runtime_stream feed the 1 s Poll
    // already delivers); it runs NO detection of its own and adds NO
    // second poller. There is no STOP latch in this version, so no
    // STOP state is read, recorded, or shown anywhere here. Colours
    // are the polish design-system tokens only (muted #5B6B7A, error
    // #B3261E, ok green #2E7D4F already live in this file); the
    // disconnected state is bold, per spec D6. The 9pt size follows
    // D6's "9pt informational text" (the panel's relay/node rows are
    // 8pt; the worker row is two lines, so it takes the spec size).
    private readonly Label _workerDot = new() { Dock = DockStyle.Fill, Font = new Font("Segoe UI", 10), Text = "\u25cf", TextAlign = ContentAlignment.MiddleCenter, ForeColor = Color.FromArgb(91, 107, 122) };
    private readonly Label _workerStatus = new() { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, Font = new Font("Segoe UI", 9), AutoEllipsis = true };
    private readonly Label _workerCause = new() { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, Font = new Font("Segoe UI", 9), AutoEllipsis = true, ForeColor = Color.FromArgb(91, 107, 122) };
    private readonly Button _workerReconnectButton = new() { Text = "Reconnect", AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48), FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false, Anchor = AnchorStyles.None, Visible = false };
    private readonly TableLayoutPanel _workerPanel = new() { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, BackColor = Color.FromArgb(247, 248, 250) };
    private readonly Font _workerFont = new Font("Segoe UI", 9);
    private readonly Font _workerFontBold = new Font("Segoe UI", 9, FontStyle.Bold);
    // Watchdog state: last good values win (a failed render keeps the
    // last good line on screen; the 2 s tick re-renders from these).
    private bool _workerSeen; // any worker.* event or sources.worker key seen yet
    private bool _workerConnected = true; // last known worker state (unknown until first feed)
    private DateTimeOffset _workerLastGoodUtc = DateTimeOffset.MinValue; // last good muse_status ready read ("last heartbeat")
    private DateTimeOffset _workerDisconnectedSince = DateTimeOffset.MinValue; // FIRST miss, not the flip time
    private DateTimeOffset _workerLastAttempt = DateTimeOffset.MinValue; // most recent reconnect attempt ("no attempt yet" while MinValue)
    private DateTimeOffset _workerNextAttemptAt = DateTimeOffset.MinValue; // countdown target (attempt event ts + next_delay_s)
    private int _workerAttempts; // attempts so far this episode
    private bool _workerAutoStopped; // cap (spec §6.2) or stop-early (§6.3): no countdown, button stays
    private string _workerCauseCode = ""; // grant_off / grant_paused / tunnel_down / "" unknown
    private DateTimeOffset _workerReconnectedAt = DateTimeOffset.MinValue; // recovery line window (shown ~10 s, then normal form)
    private long _workerDownSeconds; // "was down Xm Ys" for the recovery line
    private bool _workerStatusBold; // tracks the current Font so the bold swap happens on transition only (GDI handle rule, cf. _activityAge)
    private bool _workerReconnectInFlight; // local double-press guard (the service single-flight guard is the second line of defence)
    private readonly ListView _list = new() { Dock = DockStyle.Fill, View = View.Details, FullRowSelect = true, MultiSelect = false };
    // B2-A B8: the device-list buttons are fields so RefreshView can disable
    // them when nothing is selected (previously they silently did nothing).
    private readonly Button _toggleControlButton = new() { Text = "Allow / block touch control", AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48), FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
    private readonly Button _revokeButton = new() { Text = "Remove phone", AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(179, 38, 30), FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
    private string _deviceSnapshot = "";
    private readonly TextBox _nameBox = new() { Dock = DockStyle.Fill };
    private readonly TextBox _relayBox = new() { Dock = DockStyle.Fill };
    private readonly CheckBox _builtInBox = new() { Text = "Run the relay inside this app (no separate server needed)", AutoSize = true };
    private readonly NumericUpDown _portBox = new() { Minimum = 1024, Maximum = 65535, Width = 80 };
    private readonly CheckBox _startupBox = new() { Text = "Start Deskhand when I sign in to Windows", AutoSize = true };
    private readonly CheckBox _phoneDisplayBox = new() { Text = "Follow phone orientation", AutoSize = true };
    private readonly Button _pauseButton = new() { AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48), FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
    private readonly AssistantClient? _chat;
    private readonly Label _activityTask = new() { AutoSize = true, MaximumSize = new Size(800, 0) };
    private readonly ListBox _activityFiles = new() { Dock = DockStyle.Fill };
    // v100.011 stale indicator (stale-indicator-design.md): last good
    // poll/task timestamps; the age label is recomputed from these on
    // every 2s timer tick, so a dead poll ages visibly with no traffic.
    private readonly Label _activityAge = new() { AutoSize = true, TextAlign = ContentAlignment.MiddleRight };
    private GroupBox? _activityGroupBox;
    private DateTimeOffset _lastActivityAsOfUtc = DateTimeOffset.MinValue; // last good poll time
    private DateTimeOffset _lastTaskUpdatedUtc = DateTimeOffset.MinValue;  // last good task.updated
    private string _activityStatus = "idle"; // last good poll status
    private bool _activityStale;
    private bool _activityPolling;
    // 100.016 (Worker B) — Diagnostics GroupBox state (spec §5). The view
    // is fed by the service `health` op on the existing 2s tick; the
    // snapshot below is the last good op result, so a dead service ages
    // visibly with no extra traffic. Row ages and the footer age are
    // recomputed from these on every tick (RenderDiagnostics, called at
    // the end of RefreshView).
    private GroupBox? _diagnosticsGroupBox;
    private TableLayoutPanel? _diagnosticsRoot;
    private int _diagnosticsRowIndex = -1;
    private Label? _diagnosticsSummaryDot;
    private Label? _diagnosticsSummary;
    private Button? _diagnosticsToggle;
    private TableLayoutPanel? _diagnosticsBody;
    private Button? _diagnosticsCopyButton;
    private Button? _diagnosticsWarnErrorButton;
    private CheckBox? _diagnosticsIncludeDebugCheckBox;
    private Label? _diagnosticsAge;
    private Label? _approvalsEmpty;
    private Button? _approvalsToggle;
    private bool _diagnosticsExpanded;
    private bool _approvalsExpanded = true;
    private bool _diagnosticsPolling;
    private bool _diagnosticsCopying;
    // 100.029 (Worker C) — diagnostics export options (E41 §6.1).
    // _diagnosticsWarnErrorOnly selects the WARN/ERROR-first tail
    // (level_filter "warn_error"); _diagnosticsIncludeDebug adds DEBUG
    // lines (include_debug) — Windows only, never the Android share.
    // Both are UI state only, never persisted.
    private bool _diagnosticsWarnErrorOnly;
    private bool _diagnosticsIncludeDebug;
    // 100.017 (Worker B) — stream forwarder state (spec §3.2, STEP0
    // §0.9/§0.12). The forwarder runs on its OWN 1s timer — the 2s
    // panel tick below is untouched (D7). Each tick calls the service
    // `runtime_stream` op on loopback with since = the engine's oldest
    // subscribed cursor, through the same `_chat.CallAsync` path the
    // diagnostics poll uses; the engine fans the result out to the
    // subscribed sessions. _streamPolling is the in-flight guard, the
    // same pattern as _diagnosticsPolling: a slow op call never stacks,
    // and a failed tick just retries next second (DROP, never queue).
    private System.Windows.Forms.Timer? _streamTimer;
    private bool _streamPolling;
    private DateTimeOffset _lastDiagnosticsAsOfUtc = DateTimeOffset.MinValue;
    private bool _diagnosticsStale;
    private readonly Dictionary<string, DiagnosticsRow> _diagnosticsRows = new(StringComparer.Ordinal);
    private readonly bool[] _diagnosticsRowStale = new bool[12];
    private readonly Label[] _diagnosticsDots = new Label[12];
    private readonly Label[] _diagnosticsStates = new Label[12];
    private readonly Label[] _diagnosticsAges = new Label[12];
    private readonly Label[] _diagnosticsVersions = new Label[12];
    private readonly Label[] _diagnosticsErrors = new Label[12];
    // 100.016 (Worker B) — last-good observation stamps for the C#
    // snapshot (spec §3.1). Each is stamped only when the component is
    // actually observed good on the 2s tick; a component that has never
    // been observed good keeps MinValue, which renders as "never" and
    // serializes as last_ok 0. Nothing here is a fresh probe: the stamps
    // record observations the tick already made.
    private DateTime _grantLastOkUtc = DateTime.MinValue;
    private DateTime _museLastOkUtc = DateTime.MinValue;
    private DateTime _phoneLastStreamingUtc = DateTime.MinValue;
    private DateTime _relayAssessmentLastOkUtc = DateTime.MinValue;
    private DateTime _nodeLastOkUtc = DateTime.MinValue;

    // 100.016 (Worker B) — one rendered row of the last `health` op
    // snapshot. LastOkEpoch is a Unix epoch (0 = never).
    // 100.029 (Worker C) — Level is the component's configured log
    // level from the same snapshot (E41 §6.3, DEBUG/INFO/WARN/ERROR,
    // INFO when the service omits it or sends an unknown value).
    private sealed record DiagnosticsRow(string Status, long LastOkEpoch, string Version, string Error, string Level);

    // 100.016 (Worker B) — the 12 §2.1 keys in §5.2 order, with the row
    // labels the spec fixes. The view renders these keys and nothing else.
    // 100.040: the "tunnel" key is gone with the Cloudflare tunnel.
    private static readonly string[] DiagnosticsKeys = ["windows_app", "service", "gateway", "worker", "relay", "node", "agent", "bridge", "grant", "muse", "lifecycle", "phone"];
    private static readonly string[] DiagnosticsLabels = ["Windows app", "Service (deskhand_service.py)", "Gateway", "Worker", "Relay (built-in)", "Node (Tailscale)", "Laptop agent", "Bridge (MuseBridge)", "Grant (Full PC access)", "Muse sign-in", "Lifecycle monitor", "Paired phone(s)"];
    private readonly ListBox _eventsList = new() { Dock = DockStyle.Fill };
    private readonly Label _eventsHealth = new() { AutoSize = true, MaximumSize = new Size(800, 0) };
    // 100.033 (Worker C) — Message board state (spec §8). The board
    // renders the runtime_stream snapshot OnRuntimeStream already
    // receives — no second fetch, no second cursor. _boardPosts is the
    // last good list: a failed render keeps it on screen, and the
    // footer says so instead of claiming fresh. _boardVmEvents is the
    // last seen vm_events source state ("ok" / "missing" / "error" /
    // "" when never seen); the footer never says "Live" while it is
    // not "ok" (spec §5.3).
    private GroupBox? _boardGroupBox;
    private readonly ListBox _boardList = new() { Dock = DockStyle.Fill };
    private readonly Label _boardHealth = new() { AutoSize = true, MaximumSize = new Size(800, 0), Font = new Font("Segoe UI", 9), ForeColor = Color.FromArgb(91, 107, 122) };
    private readonly Label _boardDetail = new() { Dock = DockStyle.Fill, AutoSize = false, Font = new Font("Segoe UI", 9), ForeColor = Color.FromArgb(28, 37, 48) };
    private readonly List<BoardPost> _boardPosts = new();
    private string _boardVmEvents = "";
    private DateTimeOffset _boardLastGoodUtc = DateTimeOffset.MinValue;
    private string? _boardSelectedId;
    private bool _boardDetailExpanded;
    private const string BoardDetailHint = "Select a post to see the full text and source.";

    // 100.033 (Worker C) — one rendered board post. Id is the stable
    // dedup key (spec §3.1): "vm:seq:<seq>" when the event carries a
    // seq, else type + true event time + actor. Ts is the true event
    // time, never arrival time. Detail is the expanded in-place block
    // (full text, full ref, said vs relayed time).
    private sealed record BoardPost(string Id, DateTimeOffset Ts, string Author, string Origin, string Kind, string Text, string Detail);
    private readonly ComboBox _timingTurns = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 360 };
    private readonly Label _timingLabel = new() { AutoSize = true, MaximumSize = new Size(800, 0) };
    private bool _timingPolling;
    private readonly NotifyIcon _tray = new() { Icon = SystemIcons.Shield, Visible = true };
    private EmbeddedWindowsNode? _embeddedNode;
    private bool _embeddedListening;
    private readonly SemaphoreSlim _embeddedGate = new(1, 1);
    private readonly Label _nodeStatus = new() { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, Font = new Font("Segoe UI", 8), AutoEllipsis = true };
    private readonly Label _nodeDot = new() { Dock = DockStyle.Fill, Font = new Font("Segoe UI", 10), Text = "\u25cf", TextAlign = ContentAlignment.MiddleCenter, ForeColor = Color.FromArgb(91, 107, 122) };
    private string _nodeStatusText = "Embedded: starting...";
    private readonly CancellationTokenSource _embeddedLifetime = new();

    internal async Task<object> JetstreamNetworkStatus()
    {
        await EnsureEmbeddedStarted();
        var status = await _embeddedNode!.StatusAsync();
        if (status.State == "Running" && !string.IsNullOrEmpty(status.IPv4)) await EnsureEmbeddedListening(_embeddedNode);
        return new { state = status.State, address = status.IPv4, needsLogin = status.State.Contains("Login") || !string.IsNullOrEmpty(status.AuthURL) };
    }

    internal async Task<object> JetstreamLogin()
    {
        await EnsureEmbeddedStarted();
        await _embeddedNode!.LoginAsync();
        var state = await _embeddedNode.StatusAsync();
        return new { url = state.AuthURL, state = state.State };
    }

    internal async Task<object> JetstreamCreatePairing(bool configuredRelay = false)
    {
        if (_config.Paused) throw new InvalidOperationException("Resume remote access before pairing.");
        var address = _config.RelayUrl;
        if (!configuredRelay)
        {
            await EnsureEmbeddedStarted();
            var status = await _embeddedNode!.StatusAsync();
            if (status.State != "Running" || string.IsNullOrEmpty(status.IPv4)) throw new InvalidOperationException("Sign in to Tailscale and wait for the connection to be ready.");
            await EnsureEmbeddedListening(_embeddedNode);
            address = TailscaleAddress.RelayUrl(status.IPv4, _config.RelayPort);
        }
        var pairing = _engine.StartPairing(address, address);
        using var generator = new QRCoder.QRCodeGenerator();
        using var data = generator.CreateQrCode(pairing.QrPayload, QRCoder.QRCodeGenerator.ECCLevel.M);
        return new { status = "ready", png = Convert.ToBase64String(new QRCoder.PngByteQRCode(data).GetGraphic(8)), expires = pairing.ExpiresUtc };
    }

    private bool _jetstreamPairingBusy;

    internal async void PrepareJetstreamPairing()
    {
        if (_jetstreamPairingBusy) return;
        _jetstreamPairingBusy = true;
        var resultPath = Path.Combine(AppConfig.DataDirectory, "jetstream-pairing.json");
        void Publish(object value)
        {
            var temporaryPath = resultPath + ".tmp";
            File.WriteAllText(temporaryPath, System.Text.Json.JsonSerializer.Serialize(value));
            File.Move(temporaryPath, resultPath, overwrite: true);
        }
        try
        {
            if (!_relay.Running || _config.Paused)
                throw new InvalidOperationException("Resume remote access in Deskhand before pairing.");
            await EnsureEmbeddedStarted();
            var status = await _embeddedNode!.StatusAsync();
            if (status.State.Contains("Login", StringComparison.OrdinalIgnoreCase) || !string.IsNullOrEmpty(status.AuthURL))
            {
                Publish(new { status = "sign-in" });
                await ShowEmbeddedConnection();
                return;
            }
            if (status.State != "Running" || string.IsNullOrEmpty(status.IPv4))
            {
                Publish(new { status = "starting" });
                return;
            }
            await EnsureEmbeddedListening(_embeddedNode);
            var address = TailscaleAddress.RelayUrl(status.IPv4, _config.RelayPort);
            var pairing = _engine.StartPairing(address, address);
            using var generator = new QRCoder.QRCodeGenerator();
            using var data = generator.CreateQrCode(pairing.QrPayload, QRCoder.QRCodeGenerator.ECCLevel.M);
            var png = new QRCoder.PngByteQRCode(data).GetGraphic(8);
            Publish(new { status = "ready", png = Convert.ToBase64String(png), expires = pairing.ExpiresUtc });
        }
        catch (Exception ex)
        {
            try { Publish(new { status = "error", message = ex.Message }); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        finally { _jetstreamPairingBusy = false; }
    }

    private bool _pairingDialogOpen;

    internal async void ShowPhonePairing()
    {
        if (_pairingDialogOpen) return;
        _pairingDialogOpen = true;
        try
        {
            Show();
            if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
            Activate();
            if (_config.EmbeddedNetworkEnabled) { await ShowEmbeddedConnection(); return; }
            if (!_config.RunBuiltInRelay || !_relay.Running || _config.Paused)
            {
                MessageBox.Show(this, "Start Deskhand's built-in relay and resume remote access first.", "Pair a new phone");
                return;
            }
            string? tailscaleAddress = null;
            try { tailscaleAddress = await TailscaleAddress.DiscoverAsync(_config.RelayPort); }
            catch (Exception ex) when (ex is InvalidOperationException or IOException or System.ComponentModel.Win32Exception) { }
            using var form = new PairForm(_engine, null, tailscaleAddress);
            form.ShowDialog(this);
            RefreshView();
        }
        finally { _pairingDialogOpen = false; }
    }

    public MainForm(HostEngine engine, AppConfig config, BuiltInRelay relay, AssistantClient? chat = null)
    {
        _engine = engine;
        _config = config;
        _relay = relay;
        _chat = chat;
        Text = "Deskhand";
        if (Environment.GetEnvironmentVariable("DESKHAND_CONFIG_DIR") is not null &&
            Environment.GetEnvironmentVariable("DESKHAND_EMBEDDED_TEST") == "1")
        {
            Text = "Deskhand embedded connection test";
            Shown += async (_, _) => await ShowEmbeddedConnection();
        }
        Icon = LoadDeskhandIcon();
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(850, 940);
        BackColor = Color.FromArgb(247, 248, 250);
        ForeColor = Color.FromArgb(28, 37, 48);
        MinimumSize = new Size(560, 600);
        StartPosition = FormStartPosition.CenterScreen;

        var root = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, Padding = new Padding(12), BackColor = Color.FromArgb(247, 248, 250) };
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 36));
        // 100.037 (Worker B): row 1 grows 64 -> 112 for the 4th relay-panel
        // row (the Muse-worker line: two text lines + Reconnect button).
        // The row only exists when the chat service is present, so the
        // no-chat layout keeps the old 64 px.
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, chat is not null ? 112 : 64));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 44));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 40));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 200));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 112));
        if (chat is not null) root.RowStyles.Add(new RowStyle(SizeType.Absolute, 260));
        if (chat is not null) root.RowStyles.Add(new RowStyle(SizeType.Absolute, 118));

        var statusPanel = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, BackColor = Color.FromArgb(247, 248, 250) };
        statusPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 16));
        statusPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        statusPanel.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        statusPanel.Controls.Add(_statusDot, 0, 0);
        statusPanel.Controls.Add(_status, 1, 0);
        // Build version, top right next to the status line - keep track of what build is running.
        var versionLabel = new Label { Text = "v" + Application.ProductVersion, AutoSize = true, Anchor = AnchorStyles.Right, ForeColor = Color.FromArgb(28, 37, 48), Font = new Font("Segoe UI", 10, FontStyle.Bold), TextAlign = ContentAlignment.MiddleRight };
        versionLabel.Cursor = Cursors.Hand;
        versionLabel.Font = new Font(versionLabel.Font, FontStyle.Bold | FontStyle.Underline);
        versionLabel.Click += (s, e) => ShowChangelog();
        statusPanel.Controls.Add(versionLabel, 2, 0);
        root.Controls.Add(statusPanel, 0, 0);
        var relayPanel = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 2, BackColor = Color.FromArgb(247, 248, 250) };
        relayPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 16));
        relayPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        relayPanel.RowStyles.Add(new RowStyle(SizeType.Percent, 50F));
        relayPanel.RowStyles.Add(new RowStyle(SizeType.Percent, 50F));
        relayPanel.Controls.Add(_relayDot, 0, 0);
        relayPanel.Controls.Add(_relayStatus, 1, 0);
        relayPanel.Controls.Add(_nodeDot, 0, 1);
        relayPanel.Controls.Add(_nodeStatus, 1, 1);
        if (chat is not null)
        {
            // 100.037 (Worker B): the 3rd row -- the Muse-worker line
            // (BUILD-SPEC-100037 §7.1). Row geometry: row 1 is 112 px;
            // this row is Absolute 49 (two 9pt text lines), and rows
            // 0-1 keep their 50% shares of the remaining 63 px
            // (~31 px each). 100.040: the old 3rd row (the re-mint
            // notice line) is gone with the tunnel manager. The dot
            // sits in column 0, the
            // two-line label panel in column 1, and the Reconnect
            // button in a new AutoSize column 2 (empty on rows 0-2,
            // so it costs no width there; it collapses entirely when
            // the button is hidden in the connected state).
            relayPanel.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            relayPanel.ColumnCount = 3;
            relayPanel.RowCount = 3;
            relayPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, 49));
            _workerPanel.RowStyles.Add(new RowStyle(SizeType.Percent, 50F));
            _workerPanel.RowStyles.Add(new RowStyle(SizeType.Percent, 50F));
            _workerPanel.Controls.Add(_workerStatus, 0, 0);
            _workerPanel.Controls.Add(_workerCause, 0, 1);
            relayPanel.Controls.Add(_workerDot, 0, 2);
            relayPanel.Controls.Add(_workerPanel, 1, 2);
            relayPanel.Controls.Add(_workerReconnectButton, 2, 2);
            _workerReconnectButton.Click += async (_, _) => await WorkerReconnectAsync();
        }
        root.Controls.Add(relayPanel, 0, 1);

        var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, BackColor = Color.FromArgb(247, 248, 250) };
        var pair = new Button { Text = "Pair a new phone", AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(47, 107, 102), ForeColor = Color.White, FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
        pair.FlatAppearance.BorderColor = Color.FromArgb(47, 107, 102);
        pair.FlatAppearance.MouseOverBackColor = Color.FromArgb(38, 89, 84);
        pair.Click += (_, _) => ShowPhonePairing();
        _pauseButton.Click += (_, _) => _engine.SetPaused(!_config.Paused);
        _pauseButton.FlatAppearance.BorderColor = Color.FromArgb(217, 222, 228);
        actions.Controls.Add(pair);
        var pairTailscale = new Button { Text = "Pair via Tailscale", AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48), FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
        pairTailscale.FlatAppearance.BorderColor = Color.FromArgb(217, 222, 228);
        pairTailscale.Click += async (_, _) =>
        {
            if (_config.EmbeddedNetworkEnabled) { await ShowEmbeddedConnection(); return; }
            pairTailscale.Enabled = false;
            try
            {
                if (!_config.RunBuiltInRelay || !_relay.Running || _config.Paused)
                    throw new InvalidOperationException("Start Deskhand's built-in relay and resume remote access first.");
                var address = await TailscaleAddress.DiscoverAsync(_config.RelayPort);
                MessageBox.Show(this, "Connect Tailscale on your phone to the same network, then scan the next QR code. Your existing phone pairing and relay address remain available.", "Pair via Tailscale");
                using var f = new PairForm(_engine, address, address);
                f.ShowDialog(this);
                RefreshView();
            }
            catch (Exception ex) when (ex is InvalidOperationException or IOException or System.ComponentModel.Win32Exception)
            { MessageBox.Show(this, ex.Message, "Pair via Tailscale"); }
            finally { pairTailscale.Enabled = true; }
        };
        actions.Controls.Add(pairTailscale);
        var embedded = new Button { Text = "Embedded connection", AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48), FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
        embedded.FlatAppearance.BorderColor = Color.FromArgb(217, 222, 228);
        embedded.Click += async (_, _) => await ShowEmbeddedConnection();
        actions.Controls.Add(embedded);
        actions.Controls.Add(_pauseButton);
        root.Controls.Add(actions, 0, 2);

        _list.Columns.Add("Paired phone", 240);
        _list.Columns.Add("Touch control", 110);
        _list.Columns.Add("Paired", 140);
        // v100.009 polish: 24 px minimum device-list rows. A ListView
        // has no RowHeight property; an empty image list of that
        // height is the standard way to set it. No images are added.
        var listRowHeight = new ImageList { ImageSize = new Size(1, 24) };
        _list.SmallImageList = listRowHeight;
        root.Controls.Add(_list, 0, 3);

        var listButtons = new FlowLayoutPanel { Dock = DockStyle.Fill, BackColor = Color.FromArgb(247, 248, 250) };
        _toggleControlButton.FlatAppearance.BorderColor = Color.FromArgb(217, 222, 228);
        _toggleControlButton.Click += (_, _) => { if (Selected() is { } d) _engine.SetControl(d, !d.Control); };
        _revokeButton.FlatAppearance.BorderColor = Color.FromArgb(179, 38, 30);
        _revokeButton.Click += (_, _) =>
        {
            if (Selected() is { } d && MessageBox.Show(this, $"Remove \"{d.Name}\"? It will no longer be able to connect.", "Deskhand", MessageBoxButtons.YesNo) == DialogResult.Yes)
                _engine.Revoke(d);
        };
        listButtons.Controls.Add(_toggleControlButton);
        listButtons.Controls.Add(_revokeButton);
        root.Controls.Add(listButtons, 0, 4);

        var settings = new GroupBox { Text = "Settings", Dock = DockStyle.Fill, BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48) };
        var grid = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, Padding = new Padding(12), BackColor = Color.FromArgb(255, 255, 255) };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 150));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        grid.Controls.Add(new Label { Text = "This PC's name", AutoSize = true, Anchor = AnchorStyles.Left }, 0, 0);
        grid.Controls.Add(_nameBox, 1, 0);
        grid.Controls.Add(new Label { Text = "Relay address", AutoSize = true, Anchor = AnchorStyles.Left }, 0, 1);
        grid.Controls.Add(_relayBox, 1, 1);
        grid.Controls.Add(_builtInBox, 0, 2);
        grid.SetColumnSpan(_builtInBox, 2);
        var portRow = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, WrapContents = true, BackColor = Color.FromArgb(247, 248, 250) };
        portRow.Controls.Add(new Label { Text = "Relay port", AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(0, 6, 8, 0) });
        portRow.Controls.Add(_portBox);
        portRow.Controls.Add(_phoneDisplayBox);
        var apply = new Button { Text = "Apply settings", AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48), FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
        apply.FlatAppearance.BorderColor = Color.FromArgb(217, 222, 228);
        apply.Click += async (_, _) => await ApplySettings();
        portRow.Controls.Add(apply);
        grid.Controls.Add(portRow, 0, 3);
        grid.SetColumnSpan(portRow, 2);
        grid.Controls.Add(_startupBox, 0, 4);
        grid.SetColumnSpan(_startupBox, 2);
        settings.Controls.Add(grid);
        root.Controls.Add(settings, 0, 5);
        var authority = new GroupBox { Text = "AI computer access", Dock = DockStyle.Fill, BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48) };
        var authorityControls = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = true, BackColor = Color.FromArgb(247, 248, 250) };
        _reviewApprovalButton.Click += (_, _) => ReviewAssistantApproval();
        _reviewApprovalButton.FlatAppearance.BorderColor = Color.FromArgb(217, 222, 228);
        authorityControls.Controls.Add(_reviewApprovalButton);
        authorityControls.Controls.Add(_authorityStatus);
        authority.Controls.Add(authorityControls);
        root.Controls.Add(authority, 0, 6);
        if (chat is not null)
        {
            var assistant = new GroupBox { Text = "Assistant chat", Dock = DockStyle.Fill, BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48) };
            assistant.Controls.Add(new ChatPanel(chat, config));
            root.Controls.Add(assistant, 0, 7);
        }
        if (chat is not null)
        {
            // Runtime activity: what Jett is doing right now, and the
            // files being worked on. Clean and minimal — one line for
            // the current task, up to five recent file lines. Reads
            // live from the service `activity` op (turn box + vendor
            // bridge audit), never a static list.
            var activity = new GroupBox { Text = "Runtime activity", Dock = DockStyle.Fill, BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48) };
            _activityGroupBox = activity;
            var activityLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 2, Padding = new Padding(12), BackColor = Color.FromArgb(247, 248, 250) };
            activityLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            activityLayout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            activityLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 20));
            activityLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            _activityTask.Text = "Idle";
            _activityFiles.Items.Add("No files being worked on");
            activityLayout.Controls.Add(_activityTask, 0, 0);
            activityLayout.Controls.Add(_activityAge, 1, 0);
            activityLayout.Controls.Add(_activityFiles, 0, 1);
            activity.Controls.Add(activityLayout);
            root.Controls.Add(activity, 0, 8);
        }
        if (chat is not null)
        {
            // Batch 2: runtime event feed (runtime_stream, 1s poll owned by
            // the AssistantClient cursor) + per-hop timing diagnostics.
            // The client owns the feed; this panel only renders its
            // snapshot, newest last, with the sources health map.
            var events = new GroupBox { Text = "Runtime events", Dock = DockStyle.Fill };
            var eventsLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4 };
            eventsLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 20));
            eventsLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            eventsLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 28));
            eventsLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 20));
            _eventsHealth.Text = "Events: waiting for the runtime feed\u2026";
            var timingRow = new FlowLayoutPanel { Dock = DockStyle.Fill };
            timingRow.Controls.Add(new Label { Text = "Turn timing:", AutoSize = true });
            timingRow.Controls.Add(_timingTurns);
            _timingLabel.Text = "Select a turn to see per-hop timing (diagnostics only).";
            eventsLayout.Controls.Add(_eventsHealth, 0, 0);
            eventsLayout.Controls.Add(_eventsList, 0, 1);
            eventsLayout.Controls.Add(timingRow, 0, 2);
            eventsLayout.Controls.Add(_timingLabel, 0, 3);
            events.Controls.Add(eventsLayout);
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 150));
            root.Controls.Add(events, 0, 9);
            chat.RuntimeStreamChanged += OnRuntimeStream;
            _timingTurns.SelectedIndexChanged += async (_, _) => await RefreshTimingAsync();
        }
        if (chat is not null)
        {
            // 100.033 (Worker C) — Message board (agent message board
            // spec §8). Row 10 in the root table: directly below the
            // Runtime events GroupBox (row 9), which is itself below
            // the Runtime activity GroupBox (row 8). Placement
            // reasoning — the spec's "new row below activity" cannot
            // go at row 9: that row is already the Runtime events
            // panel, and inserting between activity and events would
            // split two panels fed by the same runtime_stream leg and
            // force the events control to be renumbered. Row 10 groups
            // the board with the stream-fed panels above it, appends a
            // single RowStyle with no existing control renumbered, and
            // the Diagnostics row below is computed dynamically
            // (_diagnosticsRowIndex = RowStyles.Count), so it moves to
            // row 11 on its own. ~180px tall, per spec §8.1.
            // Read-only: tapping a row expands its detail in place in
            // the detail line below the list; the board never approves,
            // cancels, restarts, or messages anything (spec §9.8).
            // No pop-up windows. Colours are the polish design-system
            // tokens only (bg #F7F8FA, surface white, primary #2F6B66,
            // text #1C2530, muted #5B6B7A, error #B3261E).
            var board = new GroupBox { Text = "Message board", Dock = DockStyle.Fill, BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48) };
            _boardGroupBox = board;
            var boardLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Padding = new Padding(12, 6, 12, 6), BackColor = Color.FromArgb(255, 255, 255) };
            boardLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 20));
            boardLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            boardLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 52));
            _boardHealth.Text = "Message board: waiting for the runtime feed\u2026";
            _boardDetail.Text = BoardDetailHint;
            boardLayout.Controls.Add(_boardHealth, 0, 0);
            boardLayout.Controls.Add(_boardList, 0, 1);
            boardLayout.Controls.Add(_boardDetail, 0, 2);
            board.Controls.Add(boardLayout);
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 180));
            root.Controls.Add(board, 0, 10);
            _boardList.SelectedIndexChanged += (_, _) => ShowBoardDetail();
            _boardList.DoubleClick += (_, _) =>
            {
                if (_boardDetailExpanded) { _boardDetailExpanded = false; _boardDetail.Text = BoardDetailHint; }
                else ShowBoardDetail();
            };
        }
        if (chat is not null)
        {
            // 100.016 (Worker B) — Diagnostics GroupBox (spec §5.1).
            // Collapsed by default, always-start-collapsed: no E22
            // panel-state mechanism exists in the live tree, so decision
            // 6 falls back to collapsed on every start (spec §1.3
            // decision 6 fallback — recorded in the build log, not
            // invented here). White GroupBox card, like every other
            // GroupBox. Segoe UI 14 Bold header, 9pt rows (§5.2).
            _diagnosticsRoot = root;
            var diagnostics = new GroupBox { Text = "Diagnostics", Dock = DockStyle.Fill, BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48), Font = new Font("Segoe UI", 14, FontStyle.Bold) };
            _diagnosticsGroupBox = diagnostics;
            var diagnosticsHeader = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 20, BackColor = Color.FromArgb(255, 255, 255), Padding = new Padding(12, 0, 12, 0) };
            _diagnosticsSummaryDot = new Label { Text = "\u25cf", Font = new Font("Segoe UI", 10), ForeColor = Color.FromArgb(91, 107, 122), AutoSize = true, TextAlign = ContentAlignment.MiddleCenter };
            _diagnosticsSummary = new Label { Text = "Diagnostics — not checked yet", Font = new Font("Segoe UI", 9), ForeColor = Color.FromArgb(28, 37, 48), AutoSize = true, TextAlign = ContentAlignment.MiddleLeft };
            _diagnosticsToggle = new Button { Text = "\u25b8", Font = new Font("Segoe UI", 9), AutoSize = true, FlatStyle = FlatStyle.Flat, BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48) };
            _diagnosticsToggle.FlatAppearance.BorderSize = 0;
            _diagnosticsToggle.Click += (_, _) => SetDiagnosticsExpanded(!_diagnosticsExpanded);
            _diagnosticsSummary.Click += (_, _) => SetDiagnosticsExpanded(!_diagnosticsExpanded);
            diagnosticsHeader.Controls.Add(_diagnosticsSummaryDot);
            diagnosticsHeader.Controls.Add(_diagnosticsSummary);
            diagnosticsHeader.Controls.Add(_diagnosticsToggle);
            diagnostics.Controls.Add(diagnosticsHeader);
            var diagnosticsBody = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4, BackColor = Color.FromArgb(255, 255, 255), Padding = new Padding(12, 0, 12, 12) };
            diagnosticsBody.RowStyles.Add(new RowStyle(SizeType.Absolute, 260));
            diagnosticsBody.RowStyles.Add(new RowStyle(SizeType.Absolute, 36));
            diagnosticsBody.RowStyles.Add(new RowStyle(SizeType.Absolute, 20));
            diagnosticsBody.RowStyles.Add(new RowStyle(SizeType.Absolute, 20));
            var diagnosticsRows = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 6, RowCount = 12, BackColor = Color.FromArgb(255, 255, 255) };
            diagnosticsRows.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 16));
            diagnosticsRows.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 78));
            diagnosticsRows.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 170));
            diagnosticsRows.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 150));
            diagnosticsRows.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 70));
            diagnosticsRows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            for (var i = 0; i < 12; i++)
            {
                diagnosticsRows.RowStyles.Add(new RowStyle(SizeType.Absolute, 20));
                _diagnosticsDots[i] = new Label { Text = "\u25cf", Font = new Font("Segoe UI", 10), ForeColor = Color.FromArgb(91, 107, 122), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter };
                _diagnosticsStates[i] = new Label { Text = "UNKNOWN", Font = new Font("Segoe UI", 9), ForeColor = Color.FromArgb(91, 107, 122), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
                var diagnosticsName = new Label { Text = DiagnosticsLabels[i], Font = new Font("Segoe UI", 9), ForeColor = Color.FromArgb(28, 37, 48), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
                _diagnosticsAges[i] = new Label { Text = "never", Font = new Font("Segoe UI", 9), ForeColor = Color.FromArgb(91, 107, 122), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true };
                _diagnosticsVersions[i] = new Label { Text = "", Font = new Font("Segoe UI", 9), ForeColor = Color.FromArgb(91, 107, 122), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true };
                _diagnosticsErrors[i] = new Label { Text = "", Font = new Font("Segoe UI", 9), ForeColor = Color.FromArgb(179, 38, 30), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true };
                diagnosticsRows.Controls.Add(_diagnosticsDots[i], 0, i);
                diagnosticsRows.Controls.Add(_diagnosticsStates[i], 1, i);
                diagnosticsRows.Controls.Add(diagnosticsName, 2, i);
                diagnosticsRows.Controls.Add(_diagnosticsAges[i], 3, i);
                diagnosticsRows.Controls.Add(_diagnosticsVersions[i], 4, i);
                diagnosticsRows.Controls.Add(_diagnosticsErrors[i], 5, i);
            }
            diagnosticsBody.Controls.Add(diagnosticsRows, 0, 0);
            var diagnosticsFooter = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 4, BackColor = Color.FromArgb(255, 255, 255) };
            diagnosticsFooter.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            diagnosticsFooter.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            diagnosticsFooter.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            diagnosticsFooter.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            _diagnosticsCopyButton = new Button { Text = "Copy diagnostics", Font = new Font("Segoe UI", 9), AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(47, 107, 102), ForeColor = Color.White, FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
            _diagnosticsCopyButton.FlatAppearance.BorderColor = Color.FromArgb(47, 107, 102);
            _diagnosticsCopyButton.FlatAppearance.MouseOverBackColor = Color.FromArgb(38, 89, 84);
            _diagnosticsCopyButton.Click += async (_, _) => await CopyDiagnosticsAsync();
            // 100.029 (Worker C) — export options (E41 §6.1): the
            // WARN/ERROR-first button selects the level_filter the
            // export is called with; the "Include debug" checkbox is
            // Windows only (the Android share never includes DEBUG).
            // Both are plain UI state, read at click time.
            _diagnosticsWarnErrorButton = new Button { Text = "All levels", Font = new Font("Segoe UI", 9), AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48), FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
            _diagnosticsWarnErrorButton.FlatAppearance.BorderColor = Color.FromArgb(180, 188, 196);
            _diagnosticsWarnErrorButton.Click += (_, _) => { _diagnosticsWarnErrorOnly = !_diagnosticsWarnErrorOnly; UpdateDiagnosticsExportOptions(); };
            _diagnosticsIncludeDebugCheckBox = new CheckBox { Text = "Include debug", Font = new Font("Segoe UI", 9), AutoSize = true, BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48), Checked = false };
            _diagnosticsIncludeDebugCheckBox.CheckedChanged += (_, _) => { _diagnosticsIncludeDebug = _diagnosticsIncludeDebugCheckBox?.Checked ?? false; };
            _diagnosticsAge = new Label { Text = "age unknown", Font = new Font("Segoe UI", 9), ForeColor = Color.FromArgb(91, 107, 122), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleRight };
            diagnosticsFooter.Controls.Add(_diagnosticsCopyButton, 0, 0);
            diagnosticsFooter.Controls.Add(_diagnosticsWarnErrorButton, 1, 0);
            diagnosticsFooter.Controls.Add(_diagnosticsIncludeDebugCheckBox, 2, 0);
            diagnosticsFooter.Controls.Add(_diagnosticsAge, 3, 0);
            diagnosticsBody.Controls.Add(diagnosticsFooter, 0, 1);
            var approvalsHeader = new FlowLayoutPanel { Dock = DockStyle.Fill, BackColor = Color.FromArgb(255, 255, 255) };
            var approvalsTitle = new Label { Text = "Approvals", Font = new Font("Segoe UI", 9, FontStyle.Bold), ForeColor = Color.FromArgb(28, 37, 48), AutoSize = true, TextAlign = ContentAlignment.MiddleLeft };
            _approvalsToggle = new Button { Text = "\u25be", Font = new Font("Segoe UI", 9), AutoSize = true, FlatStyle = FlatStyle.Flat, BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(28, 37, 48) };
            _approvalsToggle.FlatAppearance.BorderSize = 0;
            _approvalsToggle.Click += (_, _) => SetApprovalsExpanded(!_approvalsExpanded);
            approvalsHeader.Controls.Add(approvalsTitle);
            approvalsHeader.Controls.Add(_approvalsToggle);
            diagnosticsBody.Controls.Add(approvalsHeader, 0, 2);
            // 100.016 (Worker B) — Approvals section (§5.5, reserved for
            // E08): the honest empty state and nothing else — not a 14th
            // row, no placeholder rows, no count. Expanded by default so
            // the empty state is visible in the baseline screenshot.
            _approvalsEmpty = new Label { Text = "no approvals recorded", Font = new Font("Segoe UI", 9), ForeColor = Color.FromArgb(91, 107, 122), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
            diagnosticsBody.Controls.Add(_approvalsEmpty, 0, 3);
            _diagnosticsBody = diagnosticsBody;
            diagnostics.Controls.Add(diagnosticsBody);
            diagnosticsBody.Visible = false;
            _diagnosticsRowIndex = root.RowStyles.Count;
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 44));
            root.Controls.Add(diagnostics, 0, _diagnosticsRowIndex);
        }
        Controls.Add(root);
        // v100.009 polish (item 20): let the window scroll instead of
        // clipping the lower sections when it is shrunk below its
        // content height. B2-A B12: the rows sum to 568 without chat
        // and 1320 with it (36+112+44+40+200+112+260+118+150+180+44 = 1296,
        // plus 24 px padding); the old comment said 882, which predated
        // the 150 px Runtime-events row, understating it by 126 px.
        // 100.015 (Worker B): the relay row grew 48 -> 64 for the §8
        // re-pair notice line (third row in the relay panel).
        // 100.040: the notice line is gone with the tunnel manager;
        // the row keeps 64 (relay + node lines only) so the layout
        // heights and the row sums below stay valid.
        // 100.037 (Worker B): +48 px -- the relay row grows 64 -> 112
        // for the 3rd relay-panel row (Muse-worker line + Reconnect
        // button, two 9pt lines); chat layout only, no-chat keeps 64.
        // 100.016 (Worker B): +44 px for the Diagnostics GroupBox,
        // collapsed (its expanded height scrolls; it is not in the
        // minimum, so the window still opens at the old size).
        // 100.033 (Worker C): +180 px for the Message board GroupBox
        // (row 10, below Runtime events). Known limit (spec §8.2):
        // AutoScroll is set (see below) but the 100.009 shrink test
        // showed the form clipping below ~920 px tall; whether the
        // board scrolls or clips when shrunk is a Gate 4 T10 check,
        // not claimed fixed here. Maximized (Phil's real layout) is
        // the verified layout; ClientSize stays 940, so the board
        // sits below the fold at open until scrolled, like Runtime
        // events today.
        // NOTE: the window still opens 940 px tall, so Runtime events
        // stays below the fold until scrolled; enlarging ClientSize
        // was left out so the window cannot exceed shorter screens.
        AutoScroll = true;
        root.MinimumSize = new Size(536, chat is not null ? 1320 : 568);

        _nameBox.Text = _config.DeviceName;
        _relayBox.Text = _config.RelayUrl;
        _builtInBox.Checked = _config.RunBuiltInRelay;
        _portBox.Value = _config.RelayPort;
        _startupBox.Checked = _config.StartWithWindows;
        _phoneDisplayBox.Checked = _config.PhoneFriendlyDisplay;
        _phoneDisplayBox.CheckedChanged += (_, _) => _engine.SetPhoneFriendlyDisplay(_phoneDisplayBox.Checked);

        BuildTray();
        _tray.Icon = Icon;
        _ = Handle;
        _engine.Changed += () => { if (IsHandleCreated && !IsDisposed) BeginInvoke(RefreshView); };
        _engine.Notice += text =>
        {
            if (IsHandleCreated && !IsDisposed) BeginInvoke(() => _tray.ShowBalloonTip(3000, "Deskhand", text, ToolTipIcon.Info));
        };
        if (_engine.LastDisplayNotice is { } displayNotice)
            Shown += (_, _) => _tray.ShowBalloonTip(5000, "Deskhand", displayNotice, ToolTipIcon.Info);
        var timer = new System.Windows.Forms.Timer { Interval = 2000 };
        timer.Tick += (_, _) => { RefreshView(); _ = RefreshActivityAsync(); _ = EvaluateRelayHealthAsync(); CollectDiagnosticsSnapshot(); _ = RefreshDiagnosticsAsync(); RenderDiagnostics(); RefreshBoardAge(); };
        timer.Start();
        // 100.017 — the forwarder's own 1s timer (spec D3). Separate
        // from the 2s panel tick above: the panel polls for its own
        // rendering on 2s; the forwarder pushes to the phone on 1s.
        _streamTimer = new System.Windows.Forms.Timer { Interval = 1000 };
        _streamTimer.Tick += (_, _) => _ = ForwardStreamAsync();
        _streamTimer.Start();
        RefreshView();
        // 100.040 — best-effort Tailscale upgrade of the RelayUrl at
        // startup (D2): if the stored address is the LAN default
        // (which is what Config.Load writes for a just-migrated
        // tunnel URL), try to replace it with the stable Tailscale
        // address. Async, silent on failure, never blocks startup.
        _ = UpgradeRelayUrlToTailscaleAsync();
    }



    // 100.040 (D2) — best-effort startup upgrade of the RelayUrl to the
    // Tailscale address. Runs only when the stored address is the LAN
    // default (including a just-migrated tunnel URL — Config.Load
    // replaces a tunnel URL with the LAN default before this form is
    // built). Discovery failing (no Tailscale, not signed in, timeout)
    // keeps the LAN default silently; a failed save does the same.
    // Never throws out of the fire-and-forget call in the constructor.
    private async Task UpgradeRelayUrlToTailscaleAsync()
    {
        string? tailscale;
        try
        {
            if (!string.Equals(_config.RelayUrl, AppConfig.DefaultRelayUrl(_config.RelayPort), StringComparison.OrdinalIgnoreCase)) return;
            tailscale = await TailscaleAddress.DiscoverAsync(_config.RelayPort).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is InvalidOperationException or IOException or System.ComponentModel.Win32Exception)
        {
            return;
        }
        if (IsDisposed || !IsHandleCreated) return;
        try
        {
            _config.RelayUrl = tailscale;
            _config.Save();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.SecurityException)
        {
            return;
        }
        BeginInvoke(() =>
        {
            if (IsDisposed || !IsHandleCreated) return;
            _relayBox.Text = tailscale;
            RefreshView();
        });
    }

    private PairedDevice? Selected() => _list.SelectedItems.Count == 1 ? _list.SelectedItems[0].Tag as PairedDevice : null;

    // 100.015 (Worker A) — the last relay-health assessment (read-only).
    // CurrentRelayAssessment is null until the first 60 s throttled
    // evaluation has run. 100.040: the echo-tracker and re-mint seams
    // are removed with the tunnel.
    public RelayAssessment? CurrentRelayAssessment => _relayAssessment;

    // Startup can be minimized to the tray, in which case Shown is not raised.
    // Program invokes this after constructing the form and before the message loop.
    internal async void StartEmbeddedOnLaunch()
    {
        if (!_config.EmbeddedNetworkEnabled) return;
        try { await EnsureEmbeddedStarted(); }
        catch (Exception ex) when (ex is InvalidOperationException or IOException or UnauthorizedAccessException or DllNotFoundException or BadImageFormatException)
        { _engine.RaiseNotice("Embedded connection could not start: " + ex.Message); }
    }

    private async Task EnsureEmbeddedStarted()
    {
        await _embeddedGate.WaitAsync();
        try
        {
            if (_embeddedNode is not null) return;
            var node = new EmbeddedWindowsNode();
            try
            {
                await node.StartAsync(Path.Combine(AppConfig.DataDirectory, "embedded-network"));
                if (_embeddedLifetime.IsCancellationRequested) throw new ObjectDisposedException(nameof(MainForm));
                _config.EmbeddedNetworkEnabled = true;
                try { _config.Save(); }
                catch { _config.EmbeddedNetworkEnabled = false; throw; }
                _embeddedNode = node;
                _ = MaintainEmbeddedListener(node, _embeddedLifetime.Token);
            }
            catch { node.Dispose(); throw; }
        }
        finally { _embeddedGate.Release(); }
    }

    private async Task EnsureEmbeddedListening(EmbeddedWindowsNode node)
    {
        await _embeddedGate.WaitAsync();
        try
        {
            if (!_embeddedListening)
            {
                await node.ListenAsync(_config.RelayPort);
                _embeddedListening = true;
            }
        }
        finally { _embeddedGate.Release(); }
    }

    private async Task MaintainEmbeddedListener(EmbeddedWindowsNode node, CancellationToken cancellation)
    {
        // B2-A embedded B2: this loop used to exit permanently once the
        // listener started, and _embeddedListening was a one-way latch,
        // so a later node/listener failure was never detected or repaired
        // and the status line froze on "Running". It now polls for the
        // life of the form: while Running+IPv4 it keeps the listener up
        // (EnsureEmbeddedListening is a no-op once listening); if the node
        // leaves Running the latch is cleared, so a later Running state
        // re-establishes the listener. The catch is deliberately broad:
        // StatusAsync can also throw JsonException on a malformed native
        // payload, and any such failure must retry, not kill the loop.
        while (!cancellation.IsCancellationRequested)
        {
            try
            {
                var status = await node.StatusAsync();
                var nodeState = status.State ?? "";
                _nodeStatusText = nodeState == "Running" && !string.IsNullOrEmpty(status.IPv4)
                    ? "Embedded: Running " + status.IPv4
                    : nodeState.Contains("Login") ? "Embedded: Needs sign-in" : "Embedded: " + nodeState;
                if (nodeState == "Running" && !string.IsNullOrEmpty(status.IPv4)) _nodeLastOkUtc = DateTime.UtcNow;
                if (status.State == "Running" && !string.IsNullOrEmpty(status.IPv4)) await EnsureEmbeddedListening(node);
                else _embeddedListening = false;
                await Task.Delay(TimeSpan.FromSeconds(3), cancellation);
            }
            catch (OperationCanceledException) { return; }
            catch (Exception ex)
            {
                _nodeStatusText = "Embedded: Failed - " + ex.Message;
                _embeddedListening = false;
                try { await Task.Delay(TimeSpan.FromSeconds(10), cancellation); }
                catch (OperationCanceledException) { return; }
            }
        }
    }

    private async Task ShowEmbeddedConnection()
    {
        try
        {
            if (!_config.RunBuiltInRelay || !_relay.Running || _config.Paused)
                throw new InvalidOperationException("Start Deskhand's built-in relay and resume remote access first.");
            await EnsureEmbeddedStarted();
            using var form = new EmbeddedConnectionForm(_embeddedNode!, _engine, _config.RelayPort,
                () => EnsureEmbeddedListening(_embeddedNode!));
            form.ShowDialog(this);
        }
        catch (Exception ex) when (ex is InvalidOperationException or IOException or UnauthorizedAccessException or DllNotFoundException or BadImageFormatException)
        { MessageBox.Show(this, ex.Message, "Embedded connection"); }
    }

    public bool StartHidden { get; set; }

    protected override void SetVisibleCore(bool value)
    {
        if (StartHidden)
        {
            StartHidden = false;
            base.SetVisibleCore(false);
            return;
        }
        base.SetVisibleCore(value);
    }

    private static Icon LoadDeskhandIcon()
    {
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, "deskhand.ico");
            if (File.Exists(path)) return new Icon(path);
        }
        catch { /* fall through to the shield */ }
        return SystemIcons.Shield;
    }

    private void BuildTray()
    {
        var menu = new ContextMenuStrip();
        menu.Items.Add("Open Deskhand", null, (_, _) => ShowWindow());
        menu.Items.Add("Disconnect phones now", null, (_, _) => _engine.DisconnectAll());
        menu.Items.Add("Pause / resume remote access", null, (_, _) => _engine.SetPaused(!_config.Paused));
        menu.Items.Add("Exit", null, (_, _) => Close());
        _tray.ContextMenuStrip = menu;
        _tray.DoubleClick += (_, _) => ShowWindow();
    }

    public void ShowWindow()
    {
        Show();
        WindowState = FormWindowState.Normal;
        Activate();
    }

    // Live runtime activity: current task + recent files, from the
    // service `activity` op. Polled every 2s with the view refresh.
    // A failed poll leaves the last good text in place; it never
    // throws, never blocks the UI, and never overlaps itself.
    private void ShowChangelog()
    {
        // Changelog, shown when the version label is clicked. Reads
        // CHANGELOG.md next to the exe (shipped with the build); falls
        // back to a short embedded note if the file is missing.
        string path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "CHANGELOG.md");
        string text;
        try
        {
            text = File.Exists(path) ? File.ReadAllText(path)
                : "Changelog not found next to the app.\n\nv" + Application.ProductVersion + " (see the full changelog on the Desktop).";
        }
        catch (Exception ex)
        {
            text = "Could not read the changelog: " + ex.Message;
        }
        var form = new Form { Text = "Deskhand changelog", Size = new Size(560, 480), StartPosition = FormStartPosition.CenterParent, Padding = new Padding(16) };
        var box = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, Text = text, Font = new Font("Segoe UI", 9) };
        var close = new Button { Text = "Close", Dock = DockStyle.Bottom, DialogResult = DialogResult.OK };
        form.Controls.Add(box);
        form.Controls.Add(close);
        form.AcceptButton = close;
        form.ShowDialog(this);
    }


    // 100.029 (Worker C) — runtime-stream footer (E41 §6.3): one
    // "health · idle age · level" segment per stream source, read from
    // the last good `health` snapshot (_diagnosticsRows, 2s tick) —
    // never from the stream itself, which carries no level and no
    // per-source age. The stream's source names map to health keys:
    // chat and vm_events are both served by the service row, bridge by
    // the bridge row. Idle age is never shown without the health word
    // next to it; a DOWN source is named "source down", never with a
    // stale age dressed up as fresh; a source with no health row yet
    // says so instead of inventing a level.
    private string RuntimeStreamFooter(JsonElement stream)
    {
        if (!stream.TryGetProperty("sources", out var sourcesValue) || sourcesValue.ValueKind != JsonValueKind.Object) return "sources unknown";
        var parts = new List<string>();
        foreach (var source in sourcesValue.EnumerateObject())
        {
            var streamStatus = source.Value.ValueKind == JsonValueKind.String ? source.Value.GetString() ?? "" : "";
            parts.Add(RuntimeStreamSourceText(source.Name, streamStatus));
        }
        return parts.Count == 0 ? "sources unknown" : string.Join(" \u00b7 ", parts);
    }

    private string RuntimeStreamSourceText(string sourceName, string streamStatus)
    {
        var healthKey = sourceName switch { "chat" => "service", "bridge" => "bridge", "vm_events" => "service", _ => null };
        if (healthKey is null || !_diagnosticsRows.TryGetValue(healthKey, out var row))
        {
            var word = streamStatus == "ok" ? "OK" : "DOWN";
            var middle = streamStatus == "ok" ? "no health snapshot yet" : "source down";
            return sourceName + ": " + word + " \u00b7 " + middle + " \u00b7 level unknown";
        }
        var level = row.Level is ("DEBUG" or "INFO" or "WARN" or "ERROR") ? row.Level : "INFO";
        if (row.Status == "DOWN") return sourceName + ": DOWN \u00b7 source down \u00b7 " + level;
        if (row.LastOkEpoch <= 0) return sourceName + ": " + row.Status + " \u00b7 never \u00b7 " + level;
        var age = DateTimeOffset.UtcNow - DateTimeOffset.FromUnixTimeSeconds(row.LastOkEpoch);
        if (age < TimeSpan.Zero) age = TimeSpan.Zero;
        // STALE at >6s, the same threshold the diagnostics panel uses
        // (§5.3): a source whose last good poll is old is STALE, not
        // "OK with an old timestamp".
        var health = age > TimeSpan.FromSeconds(6) ? "STALE" : row.Status;
        var middleText = row.Status == "IDLE" && !string.IsNullOrEmpty(row.Error) ? row.Error : "idle " + FormatAge(age);
        return sourceName + ": " + health + " \u00b7 " + middleText + " \u00b7 " + level;
    }

    // Batch 2: render the runtime_stream snapshot the AssistantClient
    // already polls (1s, cursor-owned). Newest last; sources health map
    // and skipped-bad-lines count in the header line.
    private void OnRuntimeStream(JsonElement stream)
    {
        if (!IsHandleCreated || IsDisposed) return;
        if (InvokeRequired) { BeginInvoke(() => OnRuntimeStream(stream)); return; }
        // 100.037 (Worker B): the worker line renders this same delivery --
        // worker.* events + the sources.worker key -- no second fetch.
        ApplyWorkerStream(stream);
        if (!stream.TryGetProperty("events", out var events) || events.ValueKind != JsonValueKind.Array) return;
        var rows = events.EnumerateArray().ToArray();
        _eventsList.BeginUpdate();
        _eventsList.Items.Clear();
        foreach (var item in rows.TakeLast(30))
        {
            var type = item.TryGetProperty("type", out var typeValue) && typeValue.ValueKind == JsonValueKind.String ? typeValue.GetString() ?? "" : "";
            var summary = item.TryGetProperty("summary", out var summaryValue) && summaryValue.ValueKind == JsonValueKind.String ? summaryValue.GetString() ?? "" : "";
            var actor = item.TryGetProperty("actor", out var actorValue) && actorValue.ValueKind == JsonValueKind.String ? actorValue.GetString() ?? "" : "";
            _eventsList.Items.Add(type + (string.IsNullOrEmpty(actor) ? "" : " \u00b7 " + actor) + (string.IsNullOrEmpty(summary) ? "" : " \u2014 " + summary));
        }
        if (rows.Length == 0) _eventsList.Items.Add("No runtime events yet.");
        _eventsList.EndUpdate();
        if (_eventsList.Items.Count > 0) _eventsList.TopIndex = _eventsList.Items.Count - 1;
        var skipped = stream.TryGetProperty("counts", out var counts) && counts.TryGetProperty("skipped_bad_lines", out var skippedValue) && skippedValue.TryGetInt64(out var skippedCount) ? skippedCount : 0;
        _eventsHealth.Text = "Events: " + RuntimeStreamFooter(stream) + " \u00b7 skipped bad lines: " + skipped;
        RenderBoard(stream);
        // Turn picker for per-hop timing: recent chat turns, newest last.
        var turnIds = new List<string>();
        foreach (var item in rows)
        {
            if (item.TryGetProperty("detail", out var detail) && detail.TryGetProperty("turn_id", out var turnId) && turnId.ValueKind == JsonValueKind.String)
            {
                var id = turnId.GetString()!;
                if (!string.IsNullOrEmpty(id) && !turnIds.Contains(id)) turnIds.Add(id);
            }
        }
        var selected = _timingTurns.SelectedItem?.ToString();
        _timingTurns.BeginUpdate();
        _timingTurns.Items.Clear();
        foreach (var id in turnIds) _timingTurns.Items.Add(id);
        if (selected is not null && turnIds.Contains(selected)) _timingTurns.SelectedItem = selected;
        _timingTurns.EndUpdate();
    }

    // 100.037 (Worker B) -- consume the worker state the service publishes
    // in this same runtime_stream delivery: the worker.* events (episode
    // facts) plus the sources.worker key (the per-second live/dead
    // signal). No detection here, no second poller, no new op call.
    // Defensive throughout: an older service sends neither, and the
    // line stays "status unknown" instead of inventing a state. A
    // changed delivery re-renders immediately; unchanged ones leave
    // the 2 s tick (RefreshView) to tick the countdown and ages.
    private void ApplyWorkerStream(JsonElement stream)
    {
        var changed = false;
        if (stream.TryGetProperty("sources", out var sources) && sources.ValueKind == JsonValueKind.Object
            && sources.TryGetProperty("worker", out var workerSource) && workerSource.ValueKind == JsonValueKind.String)
        {
            var sourceState = workerSource.GetString();
            if (sourceState == "ok")
            {
                _workerSeen = true;
                _workerLastGoodUtc = DateTimeOffset.UtcNow;
                if (!_workerConnected)
                {
                    // The feed says the worker is live again. The
                    // service also emits worker_reconnected for the
                    // episode; the event below stamps the recovery
                    // line, this only clears the disconnected state
                    // so a missing event cannot leave the line stuck
                    // on "disconnected".
                    _workerConnected = true;
                    changed = true;
                }
            }
            else if (sourceState == "disconnected")
            {
                _workerSeen = true;
                if (_workerConnected)
                {
                    _workerConnected = false;
                    if (_workerDisconnectedSince == DateTimeOffset.MinValue)
                        _workerDisconnectedSince = DateTimeOffset.UtcNow;
                    changed = true;
                }
            }
        }
        if (!stream.TryGetProperty("events", out var events) || events.ValueKind != JsonValueKind.Array)
        {
            if (changed) RefreshView();
            return;
        }
        foreach (var item in events.EnumerateArray())
        {
            var type = item.TryGetProperty("type", out var typeValue) && typeValue.ValueKind == JsonValueKind.String ? typeValue.GetString() ?? "" : "";
            if (type is not ("worker_disconnected" or "worker_reconnect_attempt" or "worker_auto_reconnect_stopped" or "worker_reconnected")) continue;
            _workerSeen = true;
            changed = true;
            JsonElement? detail = null;
            if (item.TryGetProperty("detail", out var detailValue) && detailValue.ValueKind == JsonValueKind.Object)
                detail = detailValue;
            // Event time, epoch-first with an ISO fallback -- the same
            // rule as TryBoardTs. An event with no parseable time
            // falls back to arrival time for its own stamps only;
            // disconnected_since is never invented (it stays MinValue
            // and renders as "?").
            var eventTs = TryBoardTs(item, out var ts) ? ts : DateTimeOffset.UtcNow;
            switch (type)
            {
                case "worker_disconnected":
                    _workerConnected = false;
                    _workerAttempts = 0;
                    _workerDownSeconds = 0;
                    _workerAutoStopped = false;
                    _workerReconnectedAt = DateTimeOffset.MinValue;
                    if (detail is { } disconnected)
                    {
                        if (TryWorkerTime(disconnected, "disconnected_since", out var since)) _workerDisconnectedSince = since;
                        else if (_workerDisconnectedSince == DateTimeOffset.MinValue) _workerDisconnectedSince = eventTs;
                        _workerCauseCode = WorkerCauseCode(disconnected, "cause");
                        if (TryWorkerInt(disconnected, "attempts", out var attempts)) _workerAttempts = attempts;
                    }
                    else if (_workerDisconnectedSince == DateTimeOffset.MinValue) _workerDisconnectedSince = eventTs;
                    break;
                case "worker_reconnect_attempt":
                    _workerLastAttempt = eventTs;
                    _workerAutoStopped = false;
                    if (detail is { } attempt)
                    {
                        if (TryWorkerInt(attempt, "attempt", out var number)) _workerAttempts = number;
                        if (TryWorkerDouble(attempt, "next_delay_s", out var delay)) _workerNextAttemptAt = eventTs.AddSeconds(delay);
                    }
                    break;
                case "worker_auto_reconnect_stopped":
                    _workerAutoStopped = true;
                    if (detail is { } stopped && TryWorkerInt(stopped, "attempts", out var stoppedAttempts)) _workerAttempts = stoppedAttempts;
                    break;
                case "worker_reconnected":
                    _workerConnected = true;
                    _workerAutoStopped = false;
                    if (detail is { } recovered)
                    {
                        if (TryWorkerTime(recovered, "reconnected_at", out var at)) _workerReconnectedAt = at;
                        else _workerReconnectedAt = eventTs;
                        if (TryWorkerDouble(recovered, "down_seconds", out var down)) _workerDownSeconds = (long)down;
                        else if (_workerDisconnectedSince != DateTimeOffset.MinValue) _workerDownSeconds = (long)(_workerReconnectedAt - _workerDisconnectedSince).TotalSeconds;
                    }
                    else
                    {
                        _workerReconnectedAt = eventTs;
                        if (_workerDisconnectedSince != DateTimeOffset.MinValue) _workerDownSeconds = (long)(eventTs - _workerDisconnectedSince).TotalSeconds;
                    }
                    _workerDisconnectedSince = DateTimeOffset.MinValue;
                    _workerLastAttempt = DateTimeOffset.MinValue;
                    _workerNextAttemptAt = DateTimeOffset.MinValue;
                    break;
            }
        }
        if (changed) RefreshView();
    }


    // 100.037 (Worker B) -- render the Muse-worker line from the last
    // good published state. Called from RefreshView on the 2 s tick,
    // so the countdown and the "last heartbeat" age tick with it (the
    // same cadence as the board age). The line never claims recovery
    // without verification: "reconnected" is shown only from the
    // service's verified worker_reconnected event (or the
    // worker_reconnect op's own verified result), never from a single
    // ready read. The relay state above is untouched -- this line is
    // the Muse worker, never the phone link.
    private void RenderWorkerLine()
    {
        if (_chat is null) return;
        var now = DateTimeOffset.UtcNow;
        string text;
        string cause = "";
        Color dot;
        Color fore;
        var bold = false;
        var showButton = false;
        if (!_workerSeen)
        {
            text = "Worker: status unknown \u2014 waiting for the worker feed\u2026";
            dot = Color.FromArgb(91, 107, 122);
            fore = Color.FromArgb(91, 107, 122);
        }
        else if (_workerConnected && _workerReconnectedAt != DateTimeOffset.MinValue && now - _workerReconnectedAt < TimeSpan.FromSeconds(10))
        {
            // Recovery line, shown once (~10 s window), then the
            // normal connected form below takes over.
            text = "Worker: reconnected " + _workerReconnectedAt.ToLocalTime().ToString("HH:mm:ss") + " (was down " + FormatDownDuration(_workerDownSeconds) + ")";
            dot = Color.FromArgb(46, 125, 79);
            fore = Color.FromArgb(91, 107, 122);
        }
        else if (_workerConnected)
        {
            // "last heartbeat" is E12's phrase; live it means the last
            // good muse_status ready read (no heartbeat exists --
            // spec §13 R3). Never invented: before the first good read
            // it says "never".
            var age = _workerLastGoodUtc == DateTimeOffset.MinValue ? "never" : FormatAge(now - _workerLastGoodUtc) + " ago";
            text = "Worker: connected \u00b7 last heartbeat " + age;
            dot = Color.FromArgb(46, 125, 79);
            fore = Color.FromArgb(91, 107, 122);
        }
        else
        {
            var since = _workerDisconnectedSince == DateTimeOffset.MinValue ? "?" : _workerDisconnectedSince.ToLocalTime().ToString("HH:mm:ss");
            var attempt = _workerLastAttempt == DateTimeOffset.MinValue ? "no attempt yet" : _workerLastAttempt.ToLocalTime().ToString("HH:mm:ss");
            if (_workerAutoStopped)
            {
                text = "Worker disconnected since " + since + ", last attempt " + attempt + " \u2014 auto-reconnect stopped after " + _workerAttempts + " attempts";
            }
            else if (_workerNextAttemptAt != DateTimeOffset.MinValue)
            {
                var nextIn = Math.Max(0, (int)Math.Ceiling((_workerNextAttemptAt - now).TotalSeconds));
                text = "Worker disconnected since " + since + ", last attempt " + attempt + ", next in " + nextIn + "s";
            }
            else
            {
                text = "Worker disconnected since " + since + ", last attempt " + attempt;
            }
            cause = WorkerCauseText(_workerCauseCode);
            dot = Color.FromArgb(179, 38, 30);
            fore = Color.FromArgb(179, 38, 30);
            bold = true;
            showButton = true;
        }
        _workerStatus.Text = text;
        _workerStatus.ForeColor = fore;
        _workerDot.ForeColor = dot;
        _workerCause.Text = cause;
        // Bold only on the disconnected transition (GDI handle rule,
        // cf. _activityAge): the same Font object is re-assigned, so
        // no new handle is created on every tick.
        if (bold != _workerStatusBold)
        {
            _workerStatusBold = bold;
            _workerStatus.Font = bold ? _workerFontBold : _workerFont;
        }
        _workerReconnectButton.Visible = showButton;
        _workerReconnectButton.Enabled = showButton && !_workerReconnectInFlight;
    }


    // 100.037 (Worker B) -- the cause line under the disconnected line,
    // verbatim E12 strings (spec §7.1). The tunnel line's check time is
    // the last relay-health evaluation (the tunnel assessment it
    // refreshes); "unknown" cause shows nothing, never an invented one.
    private string WorkerCauseText(string causeCode) => causeCode switch
    {
        "grant_off" => "Full PC access is off \u2014 reconnect cannot succeed until you turn it on",
        "grant_paused" => "Full PC access is paused \u2014 reconnect cannot succeed until you resume it",
        "tunnel_down" => _relayHealthLastCheckedUtc == DateTime.MinValue
            ? "Tunnel stale \u2014 reconnect cannot succeed until the tunnel recovers"
            : "Tunnel stale \u2014 last tunnel-watchdog check " + _relayHealthLastCheckedUtc.ToLocalTime().ToString("HH:mm"),
        _ => "",
    };


    // 100.037 (Worker B) -- "1m 46s" for the recovery line's down time.
    private static string FormatDownDuration(long seconds)
    {
        if (seconds < 60) return seconds + "s";
        return (seconds / 60) + "m " + (seconds % 60) + "s";
    }


    // 100.037 (Worker B) -- defensive field readers for worker event
    // detail / worker_reconnect results (epoch-first, ISO fallback --
    // the same rule as TryBoardTs). A missing or mistyped field is a
    // miss, never an invented value.
    private static bool TryWorkerTime(JsonElement detail, string name, out DateTimeOffset value)
    {
        value = default;
        if (!detail.TryGetProperty(name, out var field)) return false;
        if (field.ValueKind == JsonValueKind.Number && field.TryGetDouble(out var seconds) && seconds > 0)
        {
            value = DateTimeOffset.FromUnixTimeSeconds((long)seconds);
            return true;
        }
        if (field.ValueKind == JsonValueKind.String && DateTimeOffset.TryParse(field.GetString(), out var parsed))
        {
            value = parsed;
            return true;
        }
        return false;
    }


    private static bool TryWorkerInt(JsonElement detail, string name, out int value)
    {
        value = 0;
        if (!detail.TryGetProperty(name, out var field)) return false;
        if (field.ValueKind == JsonValueKind.Number && field.TryGetInt32(out var i)) { value = i; return true; }
        if (field.ValueKind == JsonValueKind.Number && field.TryGetInt64(out var l) && l >= int.MinValue && l <= int.MaxValue) { value = (int)l; return true; }
        return false;
    }


    private static bool TryWorkerDouble(JsonElement detail, string name, out double value)
    {
        value = 0;
        if (!detail.TryGetProperty(name, out var field)) return false;
        if (field.ValueKind == JsonValueKind.Number && field.TryGetDouble(out var d)) { value = d; return true; }
        return false;
    }


    private static string WorkerCauseCode(JsonElement detail, string name) =>
        detail.TryGetProperty(name, out var cause) && cause.ValueKind == JsonValueKind.String ? cause.GetString() ?? "" : "";


    // 100.037 (Worker B) -- manual Reconnect: one attempt immediately,
    // outside the backoff schedule, via the new service op
    // worker_reconnect over the existing AssistantClient stdin/stdout
    // channel (no new HostEngine op, no HandleChat). The local
    // in-flight guard stops a double-press; the service single-flight
    // guard is the second line of defence. The op returns the attempt
    // outcome + the current watchdog state, which is rendered
    // immediately instead of waiting for the next 1 s poll. Never
    // touches a grant, an approval, a tunnel, a process, or any STOP
    // flag (there is none).
    private async Task WorkerReconnectAsync()
    {
        if (_chat is null || _workerReconnectInFlight) return;
        _workerReconnectInFlight = true;
        _workerReconnectButton.Enabled = false;
        try
        {
            var result = await _chat.CallAsync(new { op = "worker_reconnect" });
            if (IsDisposed || !IsHandleCreated) return;
            ApplyWorkerReconnectResult(result);
        }
        catch (Exception)
        {
            // The line keeps the last good state; the next 1 s poll
            // re-renders it from the stream. A failed call is never
            // shown as a recovery.
        }
        finally
        {
            _workerReconnectInFlight = false;
            if (!IsDisposed && IsHandleCreated) RefreshView();
        }
    }


    // 100.037 (Worker B) -- render the worker_reconnect op result at
    // once. Field names are parsed defensively (the op is new in
    // 100.037; an older service answers with an error and CallAsync
    // throws before this runs). A verified recovery is signalled by
    // the op's own outcome (reconcile ok AND status ready, spec §5.4
    // -- checked service-side); a bare ready flag is never enough.
    private void ApplyWorkerReconnectResult(JsonElement result)
    {
        _workerSeen = true;
        var now = DateTimeOffset.UtcNow;
        _workerLastAttempt = now;
        if (TryWorkerInt(result, "attempts", out var attempts)) _workerAttempts = attempts;
        if (TryWorkerTime(result, "disconnected_since", out var since)) _workerDisconnectedSince = since;
        if (TryWorkerDouble(result, "next_delay_s", out var delay)) _workerNextAttemptAt = now.AddSeconds(delay);
        else if (TryWorkerDouble(result, "next_in_s", out var nextIn)) _workerNextAttemptAt = now.AddSeconds(nextIn);
        var cause = WorkerCauseCode(result, "cause");
        if (!string.IsNullOrEmpty(cause)) _workerCauseCode = cause;
        var recovered = false;
        if (result.TryGetProperty("reconnected", out var reconnected) && reconnected.ValueKind == JsonValueKind.True) recovered = true;
        if (result.TryGetProperty("ready", out var ready) && ready.ValueKind == JsonValueKind.True
            && result.TryGetProperty("reconcile_ok", out var reconcileOk) && reconcileOk.ValueKind == JsonValueKind.True) recovered = true;
        if (result.TryGetProperty("status", out var status) && status.ValueKind == JsonValueKind.String && status.GetString() == "connected") recovered = true;
        if (recovered)
        {
            _workerConnected = true;
            _workerAutoStopped = false;
            _workerReconnectedAt = now;
            _workerDownSeconds = _workerDisconnectedSince != DateTimeOffset.MinValue ? (long)(now - _workerDisconnectedSince).TotalSeconds : 0;
            _workerDisconnectedSince = DateTimeOffset.MinValue;
            _workerLastAttempt = DateTimeOffset.MinValue;
            _workerNextAttemptAt = DateTimeOffset.MinValue;
        }
        else
        {
            _workerConnected = false;
            if (result.TryGetProperty("auto_reconnect_stopped", out var stopped) && stopped.ValueKind == JsonValueKind.True) _workerAutoStopped = true;
        }
    }

    // 100.033 (Worker C) — Message board rendering (spec §8, §5.3).
    // Renders the runtime_stream snapshot OnRuntimeStream already
    // receives — the board adds NO second fetch and NO second cursor
    // (spec §12 #10): the AssistantClient owns the 1s poll and its
    // since cursor, and this panel only renders the window it
    // delivers. Each delivery rebuilds the list from the window
    // (newest first, dedup by id, capped at 100); a resynced=true
    // delivery is the re-fetch from since=null, so the rebuild IS the
    // resync handling — the local list is dropped and re-derived,
    // never duplicated, and a failed render keeps the last good list
    // on screen instead of blanking the board.
    private void RenderBoard(JsonElement stream)
    {
        try
        {
            if (!stream.TryGetProperty("events", out var events) || events.ValueKind != JsonValueKind.Array) return;
            var fresh = new Dictionary<string, BoardPost>(StringComparer.Ordinal);
            foreach (var item in events.EnumerateArray())
            {
                var type = item.TryGetProperty("type", out var typeValue) && typeValue.ValueKind == JsonValueKind.String ? typeValue.GetString() ?? "" : "";
                if (type is not ("board.post" or "subagent.spawned" or "subagent.finished" or "subagent.failed" or "subagent.step")) continue;
                if (!TryBoardTs(item, out var ts)) continue;
                var summary = item.TryGetProperty("summary", out var summaryValue) && summaryValue.ValueKind == JsonValueKind.String ? summaryValue.GetString() ?? "" : "";
                var actor = item.TryGetProperty("actor", out var actorValue) && actorValue.ValueKind == JsonValueKind.String ? actorValue.GetString() ?? "" : "";
                var kind = "";
                var hasDetail = item.TryGetProperty("detail", out var detail) && detail.ValueKind == JsonValueKind.Object;
                if (hasDetail && detail.TryGetProperty("kind", out var kindValue) && kindValue.ValueKind == JsonValueKind.String) kind = kindValue.GetString() ?? "";
                var origin = type == "board.post" && kind == "relay" ? "relayed" : "live";
                var id = item.TryGetProperty("seq", out var seqValue) && seqValue.ValueKind == JsonValueKind.Number && seqValue.TryGetInt64(out var seq)
                    ? "vm:seq:" + seq
                    : "vm:" + type + ":" + ts.ToUnixTimeSeconds() + ":" + actor;
                if (fresh.ContainsKey(id)) continue;
                fresh[id] = new BoardPost(id, ts, string.IsNullOrWhiteSpace(actor) ? "subagent" : actor, origin, type, summary, BoardDetailText(type, summary, hasDetail ? detail : (JsonElement?)null));
            }
            _boardPosts.Clear();
            _boardPosts.AddRange(fresh.Values.OrderByDescending(p => p.Ts).ThenBy(p => p.Id, StringComparer.Ordinal).Take(100));
            _boardVmEvents = stream.TryGetProperty("sources", out var sources) && sources.ValueKind == JsonValueKind.Object && sources.TryGetProperty("vm_events", out var vmEvents) && vmEvents.ValueKind == JsonValueKind.String ? vmEvents.GetString() ?? "" : "";
            _boardLastGoodUtc = DateTimeOffset.UtcNow;
            _boardList.BeginUpdate();
            _boardList.Items.Clear();
            foreach (var post in _boardPosts)
            {
                var rowText = post.Text.Length > 300 ? post.Text[..297] + "\u2026" : post.Text;
                _boardList.Items.Add(post.Ts.ToLocalTime().ToString("HH:mm") + " \u00b7 " + post.Author + " [" + post.Origin + "] \u2014 " + rowText);
            }
            if (_boardPosts.Count == 0) _boardList.Items.Add("No posts yet \u2014 posts appear as agents start and finish work.");
            _boardList.EndUpdate();
            if (_boardSelectedId is not null)
            {
                var selected = _boardPosts.FindIndex(p => p.Id == _boardSelectedId);
                if (selected >= 0) _boardList.SelectedIndex = selected;
            }
            _boardHealth.Text = BoardFooterText();
        }
        catch (Exception)
        {
            // Last good list stays; the next poll retries.
        }
    }

    // 100.033 (Worker C) — the board footer (spec §5.3, one quiet 9pt
    // muted line). The footer never says "Live": it says when the
    // list was last updated, and only when vm_events is "ok". A
    // missing vm_events leg is named, never hidden — until the
    // vm_events.jsonl feed is flushed to the PC (Step 0 flush gap),
    // the board cannot show live posts and the footer says so.
    private string BoardFooterText()
    {
        var count = _boardPosts.Count;
        if (_boardLastGoodUtc != DateTimeOffset.MinValue && DateTimeOffset.UtcNow - _boardLastGoodUtc > TimeSpan.FromSeconds(6))
            return "STALE \u2014 no update for " + FormatAge(DateTimeOffset.UtcNow - _boardLastGoodUtc) + " \u00b7 last good posts shown";
        if (_boardVmEvents == "missing")
            return "Message board \u00b7 " + count + " posts \u00b7 live step notes not wired yet \u2014 the vm_events feed has not flushed to this PC";
        if (_boardVmEvents is not "ok")
            return "Message board \u00b7 " + count + " posts \u00b7 feed state unknown \u2014 posts shown are the last good list";
        if (count == 0)
            return "No posts yet \u2014 posts appear as agents start and finish work.";
        return "Message board \u00b7 " + count + " posts \u00b7 updated " + FormatAge(DateTimeOffset.UtcNow - _boardLastGoodUtc) + " ago \u00b7 subagent posts up to ~4 min behind (1-min feed)";
    }

    // 100.033 (Worker C) — called on the existing 2s timer tick (same
    // tick as RefreshView): recomputes only the board footer age from
    // the last good poll. WinForms timer ticks run serialised on the
    // UI thread, so no in-flight guard is needed here — there is no
    // async fetch to overlap; the guard lives on the stream delivery
    // itself, and a dead poll ages visibly through the footer.
    private void RefreshBoardAge()
    {
        if (_boardGroupBox is null || IsDisposed || !IsHandleCreated) return;
        _boardHealth.Text = BoardFooterText();
    }

    // 100.033 (Worker C) — tap expands the post's detail in place
    // (full text, full ref, said vs relayed time). No pop-up window,
    // and expanding never approves, cancels, restarts, or messages
    // anything — the board is read-only (spec §9.8).
    private void ShowBoardDetail()
    {
        if (_boardList.SelectedIndex < 0 || _boardList.SelectedIndex >= _boardPosts.Count)
        {
            _boardDetailExpanded = false;
            _boardSelectedId = null;
            _boardDetail.Text = BoardDetailHint;
            return;
        }
        var post = _boardPosts[_boardList.SelectedIndex];
        _boardSelectedId = post.Id;
        _boardDetailExpanded = true;
        _boardDetail.Text = post.Detail;
    }

    // 100.033 (Worker C) — true event time, epoch-first (the service
    // emits epoch seconds; drawer-feed design §A5). ISO-8601 only as
    // a fallback. An event with no parseable time is skipped — the
    // board never invents a time or sorts by arrival.
    private static bool TryBoardTs(JsonElement item, out DateTimeOffset ts)
    {
        ts = default;
        if (item.TryGetProperty("ts", out var tsValue) && tsValue.ValueKind == JsonValueKind.Number && tsValue.TryGetDouble(out var seconds) && seconds > 0)
        {
            ts = DateTimeOffset.FromUnixTimeSeconds((long)seconds);
            return true;
        }
        if (item.TryGetProperty("ts", out var tsText) && tsText.ValueKind == JsonValueKind.String && DateTimeOffset.TryParse(tsText.GetString(), out var parsed))
        {
            ts = parsed;
            return true;
        }
        return false;
    }

    // 100.033 (Worker C) — the expanded detail block: full text (the
    // detail.text when the writer sent one, else the summary), the
    // full ref (agent id, task, relay source chat + message id), and
    // said vs relayed time for relay posts. Relayed words are shown
    // verbatim as delivered; a summary is never presented as a quote.
    private static string BoardDetailText(string type, string summary, JsonElement? detail)
    {
        var lines = new List<string>();
        var text = "";
        if (detail is { } detailObject && detailObject.TryGetProperty("text", out var textValue) && textValue.ValueKind == JsonValueKind.String) text = textValue.GetString() ?? "";
        lines.Add(string.IsNullOrWhiteSpace(text) ? summary : text);
        if (detail is { } detailRef)
        {
            if (detailRef.TryGetProperty("agent_id", out var agentId) && agentId.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(agentId.GetString()))
                lines.Add("agent: " + agentId.GetString());
            if (detailRef.TryGetProperty("task", out var task) && task.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(task.GetString()))
                lines.Add("task: " + task.GetString());
            if (detailRef.TryGetProperty("sourceChatId", out var chat) && chat.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(chat.GetString()))
                lines.Add("relayed from chat: " + chat.GetString());
            if (detailRef.TryGetProperty("sourceMessageId", out var message) && message.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(message.GetString()))
                lines.Add("source message: " + message.GetString());
            var said = BoardDetailTs(detailRef, "saidTs");
            var relayed = BoardDetailTs(detailRef, "relayedTs");
            if (said is not null && relayed is not null) lines.Add("said " + said + " \u00b7 relayed " + relayed);
            else if (said is not null) lines.Add("said " + said);
            else if (relayed is not null) lines.Add("relayed " + relayed);
        }
        lines.Add("type: " + type);
        return string.Join(Environment.NewLine, lines);
    }

    private static string? BoardDetailTs(JsonElement detail, string name)
    {
        if (!detail.TryGetProperty(name, out var value)) return null;
        if (value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var seconds) && seconds > 0)
            return DateTimeOffset.FromUnixTimeSeconds((long)seconds).ToLocalTime().ToString("yyyy-MM-dd HH:mm");
        if (value.ValueKind == JsonValueKind.String && DateTimeOffset.TryParse(value.GetString(), out var parsed))
            return parsed.ToLocalTime().ToString("yyyy-MM-dd HH:mm");
        return null;
    }

    // Batch 2: per-hop timing for the selected turn (diagnostics only:
    // never used to re-route or replay).
    private async Task RefreshTimingAsync()
    {
        if (_chat is null || _timingPolling || IsDisposed || !IsHandleCreated) return;
        if (_timingTurns.SelectedItem is not string turnId) return;
        _timingPolling = true;
        try
        {
            var data = await _chat.CallAsync(new { op = "timing", id = turnId });
            if (IsDisposed || !IsHandleCreated) return;
            if (!data.TryGetProperty("timing", out var timing) || timing.ValueKind != JsonValueKind.Object)
            {
                _timingLabel.Text = "No timing recorded for this turn yet.";
                return;
            }
            string Seconds(string name) => timing.TryGetProperty(name, out var value) && value.TryGetDouble(out var seconds) ? seconds.ToString("0.000") + "s" : "\u2014";
            _timingLabel.Text = "TTFB " + Seconds("time_to_first_byte_secs") + " \u00b7 generation " + Seconds("generation_secs") + " \u00b7 total " + Seconds("total_secs");
        }
        catch (Exception) { _timingLabel.Text = "Timing unavailable for this turn."; }
        finally { _timingPolling = false; }
    }

    // 100.015 (Worker A) — relay-health check, on the existing 2 s
    // timer path (no new timer). Throttled to at most one evaluation per
    // 60 s, runs off the UI thread, never throws, never blocks the UI:
    // a failed check leaves the last assessment in place and the next
    // tick retries. The probe is the TCP/TLS check in CheckAsync.
    // 100.040: the echo check for ephemeral tunnel URLs is gone with
    // the Cloudflare tunnel.
    // (Named EvaluateRelayHealthAsync: the spec's similarly-named method
    // does not exist in the live tree, so this is a new method on the
    // live RefreshView/RefreshActivityAsync structure.)
    private async Task EvaluateRelayHealthAsync()
    {
        if (_relayHealthEvaluating || IsDisposed || !IsHandleCreated) return;
        if (DateTime.UtcNow - _relayHealthLastCheckedUtc < TimeSpan.FromSeconds(60)) return;
        _relayHealthEvaluating = true;
        _relayHealthLastCheckedUtc = DateTime.UtcNow;
        try
        {
            var relayUrl = _config.RelayUrl;
            var assessment = await RelayHealth.CheckAsync(relayUrl, AppConfig.CurrentLanAddresses(), _config.RelayPort).ConfigureAwait(false);
            if (IsDisposed || !IsHandleCreated) return;
            _relayAssessment = assessment;
            BeginInvoke(RefreshView);
        }
        catch (Exception)
        {
            // Last good assessment stays; the next tick retries.
        }
        finally { _relayHealthEvaluating = false; }
    }

    private async Task RefreshActivityAsync()
    {
        if (_chat is null || _activityPolling || IsDisposed || !IsHandleCreated) return;
        _activityPolling = true;
        try
        {
            var data = await _chat.CallAsync(new { op = "activity" });
            if (IsDisposed || !IsHandleCreated) return;
            var status = data.TryGetProperty("status", out var statusValue) ? statusValue.GetString() : "idle";
            string taskText;
            if (data.TryGetProperty("task", out var task) && task.ValueKind == JsonValueKind.Object)
            {
                var text = task.TryGetProperty("text", out var textValue) ? textValue.GetString() ?? "" : "";
                var taskStatus = task.TryGetProperty("status", out var taskStatusValue) ? taskStatusValue.GetString() ?? "" : "";
                if (task.TryGetProperty("updated", out var taskUpdatedValue) && taskUpdatedValue.ValueKind == JsonValueKind.Number
                    && taskUpdatedValue.TryGetDouble(out var taskUpdatedSeconds) && taskUpdatedSeconds > 0)
                {
                    var taskUpdated = DateTimeOffset.FromUnixTimeSeconds((long)taskUpdatedSeconds);
                    _lastTaskUpdatedUtc = taskUpdated > DateTimeOffset.UtcNow.AddSeconds(5) ? DateTimeOffset.UtcNow : taskUpdated;
                }
                taskText = string.IsNullOrWhiteSpace(text)
                    ? (status == "busy" ? "Working…" : "Idle")
                    : (status == "busy" ? "Working: " + text : "Last: " + text + " [" + taskStatus + "]");
            }
            else taskText = status == "busy" ? "Working…" : "Idle";
            _activityTask.Text = taskText;
            _activityStatus = status;
            if (data.TryGetProperty("as_of", out var asOfValue) && asOfValue.ValueKind == JsonValueKind.Number
                && asOfValue.TryGetDouble(out var asOfSeconds) && asOfSeconds > 0)
            {
                var asOf = DateTimeOffset.FromUnixTimeSeconds((long)asOfSeconds);
                _lastActivityAsOfUtc = asOf > DateTimeOffset.UtcNow.AddSeconds(5) ? DateTimeOffset.UtcNow : asOf;
            }
            _activityFiles.BeginUpdate();
            _activityFiles.Items.Clear();
            var shown = 0;
            if (data.TryGetProperty("files", out var files) && files.ValueKind == JsonValueKind.Array)
            {
                foreach (var file in files.EnumerateArray())
                {
                    if (shown >= 5) break;
                    var operation = file.TryGetProperty("operation", out var opValue) ? opValue.GetString() ?? "" : "";
                    var path = file.TryGetProperty("path", out var pathValue) ? pathValue.GetString() ?? "" : "";
                    if (string.IsNullOrWhiteSpace(path)) continue;
                    var verb = operation switch
                    {
                        "fs.read" => "reading",
                        "fs.write" => "writing",
                        "fs.list" => "listing",
                        "fs.delete" => "deleting",
                        "fs.mkdir" => "creating folder",
                        "exec.run" => "running in",
                        _ => operation
                    };
                    _activityFiles.Items.Add(verb + " " + path);
                    shown++;
                }
            }
            if (shown == 0) _activityFiles.Items.Add("No files being worked on");
            _activityFiles.EndUpdate();
            UpdateActivityAge();
        }
        catch (Exception)
        {
            // Last good text stays; the next poll retries.
        }
        finally { _activityPolling = false; }
    }

    private void UpdateActivityAge()
    {
        // No as_of ever seen (old service): say so, never claim fresh.
        if (_lastActivityAsOfUtc == DateTimeOffset.MinValue)
        {
            _activityAge.Text = "age unknown";
            _activityAge.ForeColor = Color.FromArgb(0x5B, 0x6B, 0x7A);
            return;
        }
        var pollAge = DateTimeOffset.UtcNow - _lastActivityAsOfUtc;
        if (pollAge < TimeSpan.Zero) pollAge = TimeSpan.Zero; // clock clamp
        var stale = pollAge > TimeSpan.FromSeconds(6);
        // Idle: show the TASK's age (how old the last turn is); busy: the
        // POLL's age (the task is current by definition).
        var showAge = _activityStatus == "idle" && _lastTaskUpdatedUtc != DateTimeOffset.MinValue
            ? DateTimeOffset.UtcNow - _lastTaskUpdatedUtc
            : pollAge;
        if (showAge < TimeSpan.Zero) showAge = TimeSpan.Zero;
        _activityAge.Text = stale
            ? "STALE — no update for " + FormatAge(pollAge)
            : "updated " + FormatAge(showAge) + " ago";
        _activityAge.ForeColor = stale ? Color.FromArgb(0xB3, 0x26, 0x1E) : Color.FromArgb(0x5B, 0x6B, 0x7A);
        if (stale != _activityStale)
        {
            // Transition only: changing Font on every 2s tick would leak
            // GDI font handles. Text/ForeColor above update every call.
            _activityStale = stale;
            _activityAge.Font = new Font(_activityAge.Font, stale ? FontStyle.Bold : FontStyle.Regular);
            if (_activityGroupBox is not null)
                _activityGroupBox.Text = stale ? "Runtime activity — STALE" : "Runtime activity";
            _activityFiles.ForeColor = stale ? Color.FromArgb(0x5B, 0x6B, 0x7A) : SystemColors.WindowText;
        }
    }

    private static string FormatAge(TimeSpan age)
    {
        if (age < TimeSpan.FromMinutes(1)) return ((int)age.TotalSeconds) + "s";
        if (age < TimeSpan.FromHours(1)) return ((int)age.TotalMinutes) + "m";
        return ((int)age.TotalHours) + "h " + age.Minutes + "m";
    }

    private void RefreshView()
    {
        var approvals = _engine.Approvals.Pending();
        _reviewApprovalButton.Enabled = approvals.Length != 0;
        _reviewApprovalButton.Text = approvals.Length == 0 ? "Review AI approval" : $"Review AI approval ({approvals.Length})";
        var tasks = _engine.AssistantTasks;
        if (tasks.Length > 0) _reviewApprovalButton.Text = approvals.Length == 0 ? $"AI: {tasks[0].Model} · {tasks[0].RunningActions.Length} action(s)" : $"Review AI approval ({approvals.Length})";
        var active = _engine.ActiveDevices;
        _status.Text = _config.Paused ? "Remote access is paused"
            : active.Count > 0 ? $"Streaming to: {string.Join(", ", active)}"
            : _config.Devices.Count == 0 ? "No phone paired yet" : "Ready - waiting for your phone";
        _statusDot.ForeColor = _config.Paused ? Color.FromArgb(138, 90, 0) : Color.FromArgb(46, 125, 79);
        _status.ForeColor = _config.Paused ? Color.FromArgb(138, 90, 0) : active.Count > 0 ? Color.FromArgb(46, 125, 79) : Color.FromArgb(28, 37, 48);
        if (_engine.RelayConnected) _relayLastOk = DateTime.UtcNow;
        string relayText;
        if (_relay.Error != null) relayText = _relay.Error;
        else if (_engine.RelayConnected) relayText = $"Relay connected. Phone address: {_config.RelayUrl}. Last OK {_relayLastOk.ToLocalTime():t}.";
        else if (_relayLastOk == DateTime.MinValue) relayText = $"Relay disconnected. Phone address: {_config.RelayUrl}.";
        else if (DateTime.UtcNow - _relayLastOk > TimeSpan.FromMinutes(5)) relayText = $"Relay disconnected for over 5 minutes. Phone address: {_config.RelayUrl}. Last OK {_relayLastOk.ToLocalTime():t}.";
        else relayText = $"Relay disconnected. Phone address: {_config.RelayUrl}. Last OK {_relayLastOk.ToLocalTime():t}.";
        // B2-A B-6: surface the RelayLink failure instead of only saying
        // the relay is disconnected. The built-in relay's own Error wins.
        if (_relay.Error is null && !_engine.RelayConnected && _engine.RelayError is { } relayError)
            relayText += " Relay error: " + relayError;
        // 100.015 (Worker A): surface the relay-health assessment in the
        // existing relay status line. No full relay URL is written here
        // — only fixed warning text (the URL is already shown in the
        // relay-address box). Dead/stale/unreachable take the red
        // treatment below. 100.040: the temporary-tunnel warnings are
        // gone with the Cloudflare tunnel.
        if (_relayAssessment is { } assessment)
        {
            if (assessment.IsStaleLanAddress && assessment.SuggestedRelayUrl is not null)
                relayText += " Old LAN address — pair again (current address is " + assessment.SuggestedRelayUrl + ").";
            else if (assessment.Health == RelayHealthStatus.Unreachable)
                relayText += " Saved relay address is unreachable from this PC.";
        }
        _relayStatus.Text = relayText;
        // v100.009 polish follow-up: relay health in the section-5 dot language
        // (relay shape B detected; the polish script's anchor line does not
        // exist in this file, so this block is inserted after the Text
        // assignment instead). Warning = amber, never dark red.
        if (_relay.Error is not null)
        {
            _relayDot.ForeColor = Color.FromArgb(179, 38, 30);
            _relayStatus.ForeColor = Color.FromArgb(179, 38, 30);
            _relayStatus.BackColor = Color.Transparent;
        }
        else if (_engine.RelayConnected)
        {
            _relayDot.ForeColor = Color.FromArgb(46, 125, 79);
            _relayStatus.ForeColor = Color.FromArgb(91, 107, 122);
            _relayStatus.BackColor = Color.Transparent;
        }
        else if (_relayLastOk == DateTime.MinValue || DateTime.UtcNow - _relayLastOk > TimeSpan.FromMinutes(5))
        {
            _relayDot.ForeColor = Color.FromArgb(179, 38, 30);
            _relayStatus.ForeColor = Color.FromArgb(179, 38, 30);
            _relayStatus.BackColor = Color.Transparent;
        }
        else
        {
            _relayDot.ForeColor = Color.FromArgb(138, 90, 0);
            _relayStatus.ForeColor = Color.FromArgb(138, 90, 0);
            _relayStatus.BackColor = Color.Transparent;
        }
        // 100.015 (Worker A): the assessment warning overrides the dot/text
        // colour the same way the relay-error line does — dead, stale, or
        // unreachable is red. 100.040: the live-but-temporary-tunnel amber
        // case is gone with the Cloudflare tunnel.
        if (_relayAssessment is { } warningAssessment)
        {
            if (warningAssessment.IsStaleLanAddress
                || warningAssessment.Health == RelayHealthStatus.Unreachable)
            {
                _relayDot.ForeColor = Color.FromArgb(179, 38, 30);
                _relayStatus.ForeColor = Color.FromArgb(179, 38, 30);
            }
        }
        // B2-A B8: the device-list buttons are only usable with a selection.
        var hasSelection = Selected() is not null;
        _toggleControlButton.Enabled = hasSelection;
        _revokeButton.Enabled = hasSelection;
        _nodeStatus.Text = _nodeStatusText;
        _nodeDot.ForeColor = _nodeStatusText.StartsWith("Embedded: Running") ? Color.FromArgb(46, 125, 79) : _nodeStatusText.Contains("Needs sign-in") ? Color.FromArgb(138, 90, 0) : _nodeStatusText.StartsWith("Embedded: Failed") ? Color.FromArgb(179, 38, 30) : Color.FromArgb(91, 107, 122);
        _nodeStatus.ForeColor = _nodeStatusText.StartsWith("Embedded: Failed") ? Color.FromArgb(179, 38, 30) : _nodeStatusText.Contains("Needs sign-in") ? Color.FromArgb(138, 90, 0) : Color.FromArgb(91, 107, 122);
        // 100.037 (Worker B): the Muse-worker line renders on the 2 s tick, beside the relay/node lines above.
        RenderWorkerLine();
        _pauseButton.Text = _config.Paused ? "Resume remote access" : "Pause remote access";
        var grantExpiry = _engine.DesktopGrantExpiresUtc;
        _authorityStatus.Text = grantExpiry is { } expiry && expiry > DateTimeOffset.UtcNow
                ? (expiry == DateTimeOffset.MaxValue ? "Desktop access is on until revoked." : $"Computer-task grant expires {expiry.LocalDateTime:t}.")
                : "Full PC access is always on. AI connections must be ready to receive tasks.";
        _tray.Text = active.Count > 0 ? "Deskhand - phone connected" : _config.Paused ? "Deskhand - paused" : "Deskhand - ready";

        var selectedId = Selected()?.Id;
        var snapshot = "";
        foreach (var d in _config.Devices) snapshot += d.Id + ":" + d.Control + ":" + d.PairedUtc.ToString("o") + "|";
        if (snapshot != _deviceSnapshot)
        {
            _deviceSnapshot = snapshot;
            _list.BeginUpdate();
            _list.Items.Clear();
            foreach (var d in _config.Devices)
            {
                var item = new ListViewItem([d.Name, d.Control ? "\u25cf Allowed" : "View only", d.PairedUtc.ToLocalTime().ToString("g")]) { Tag = d, Selected = d.Id == selectedId };
                item.UseItemStyleForSubItems = false;
                if (d.Control) item.SubItems[1].ForeColor = Color.FromArgb(46, 125, 79);
                _list.Items.Add(item);
            }
            _list.EndUpdate();
        }
        UpdateActivityAge();
        RenderDiagnostics();
    }



    private static long ToEpoch(DateTime utc) => utc == DateTime.MinValue ? 0 : new DateTimeOffset(utc).ToUnixTimeSeconds();

    // 100.016 (Worker B) — collect the 9 C#-owned rows (spec §3.1) once
    // per 2s tick and publish them to the engine, where the service reads
    // them through `health.snapshot` (STEP0 §0.2). Reads only state the
    // tick already observes — the engine (RelayConnected, devices,
    // grant), the last relay assessment, the node status text — and
    // triggers NO new probe: the relay assessment is the last one the
    // 60s-throttled evaluation produced, the tunnel flags come from
    // Classify() (offline, no traffic), and the node address is the one
    // the window already shows. A row that has never been observed good
    // keeps last_ok 0 ("never"), never a fresh timestamp. Never throws:
    // a failed collection leaves the last published snapshot in place.
    private void CollectDiagnosticsSnapshot()
    {
        try
        {
            var nowUtc = DateTime.UtcNow;
            var nowEpoch = new DateTimeOffset(nowUtc).ToUnixTimeSeconds();
            var rows = new Dictionary<string, Dictionary<string, object>>(StringComparer.Ordinal);

            // Windows app: the MainForm label literal, read locally.
            rows["windows_app"] = HostEngine.HealthRow("OK", nowEpoch, "v" + Application.ProductVersion, "");

            // Gateway: NO probe exists in this app for the funnel
            // /status + /capabilities routes (the app holds no gateway
            // token). Report UNKNOWN, never fake it from the bridge.
            // 100.040: the tunnel manager's gateway check is gone
            // with the manager.
            rows["gateway"] = HostEngine.HealthRow("UNKNOWN", 0, "", "not checked yet");

            // Worker: the engine's relay/worker state, the same state the
            // window renders. No new worker probe.
            if (_engine.RelayConnected)
            {
                rows["worker"] = HostEngine.HealthRow("OK", ToEpoch(_relayLastOk == DateTime.MinValue ? nowUtc : _relayLastOk), "", "");
            }
            else
            {
                var workerError = _engine.RelayError ?? "worker disconnected";
                rows["worker"] = HostEngine.HealthRow("DOWN", ToEpoch(_relayLastOk), "", workerError);
            }

            // Relay: the last assessment, Detail verbatim. Reused as-is
            // (same early-return rule as the 60s evaluation); never a
            // new check from here.
            var assessment = _relayAssessment;
            if (assessment is null)
            {
                rows["relay"] = HostEngine.HealthRow("UNKNOWN", 0, "", "not checked yet");
            }
            else if (assessment.Health == RelayHealthStatus.Reachable)
            {
                _relayAssessmentLastOkUtc = _relayHealthLastCheckedUtc;
                rows["relay"] = HostEngine.HealthRow("OK", ToEpoch(_relayHealthLastCheckedUtc), "", "");
            }
            else if (assessment.Health == RelayHealthStatus.Unreachable)
            {
                rows["relay"] = HostEngine.HealthRow("DOWN", ToEpoch(_relayAssessmentLastOkUtc), "", assessment.Detail);
            }
            else
            {
                rows["relay"] = HostEngine.HealthRow("UNKNOWN", 0, "", assessment.Detail);
            }


            // Node: the embedded-network / Tailscale address the window
            // already shows (_nodeStatusText). No new network scan: the
            // Tailscale address is only discovered on demand at pair
            // time and is not retained, so when the embedded network is
            // not running the row is UNKNOWN (spec §13 — report the gap,
            // do not invent a scan).
            var nodeText = _nodeStatusText;
            if (nodeText.StartsWith("Embedded: Running ", StringComparison.Ordinal))
            {
                var nodeAddress = nodeText["Embedded: Running ".Length..].Trim();
                rows["node"] = HostEngine.HealthRow("OK", ToEpoch(_nodeLastOkUtc), nodeAddress, "");
            }
            else if (nodeText.Contains("Needs sign-in", StringComparison.Ordinal))
            {
                rows["node"] = HostEngine.HealthRow("DOWN", ToEpoch(_nodeLastOkUtc), "", "Embedded: Needs sign-in");
            }
            else if (nodeText.StartsWith("Embedded: Failed", StringComparison.Ordinal))
            {
                rows["node"] = HostEngine.HealthRow("DOWN", ToEpoch(_nodeLastOkUtc), "", nodeText);
            }
            else
            {
                rows["node"] = HostEngine.HealthRow("UNKNOWN", 0, "", _config.EmbeddedNetworkEnabled ? "embedded network starting" : "embedded network not running");
            }

            // Grant + Muse sign-in: the stored grant (Enabled +
            // Generation) and the sign-in state, the same sources
            // RefreshView renders in _authorityStatus. _accessState is
            // "On" / "Needs sign-in" / "Off" / "Connecting".
            var grant = _engine.GrantSnapshot();
            var generation = "Generation " + grant.Generation;
            if (_config.Paused)
            {
                // Spec §2.2: paused-by-Phil reports IDLE, never DOWN.
                rows["grant"] = HostEngine.HealthRow("IDLE", ToEpoch(_grantLastOkUtc), generation, "paused by Phil");
            }
            else if (grant.Enabled && grant.State == "On")
            {
                _grantLastOkUtc = nowUtc;
                rows["grant"] = HostEngine.HealthRow("OK", nowEpoch, generation, "");
            }
            else if (grant.State == "Needs sign-in")
            {
                rows["grant"] = HostEngine.HealthRow("DOWN", ToEpoch(_grantLastOkUtc), generation, "FullPcAccess.Enabled=false — Muse sign-in: Needs sign-in");
            }
            else if (grant.State == "Connecting")
            {
                rows["grant"] = HostEngine.HealthRow("UNKNOWN", ToEpoch(_grantLastOkUtc), generation, "grant check in progress");
            }
            else
            {
                rows["grant"] = HostEngine.HealthRow("DOWN", ToEpoch(_grantLastOkUtc), generation, "FullPcAccess.Enabled=false — Muse sign-in: " + grant.State);
            }

            if (grant.State == "On")
            {
                _museLastOkUtc = nowUtc;
                rows["muse"] = HostEngine.HealthRow("OK", nowEpoch, "", "");
            }
            else if (grant.State == "Needs sign-in")
            {
                rows["muse"] = HostEngine.HealthRow("DOWN", ToEpoch(_museLastOkUtc), "", "Needs sign-in");
            }
            else if (grant.State == "Connecting")
            {
                rows["muse"] = HostEngine.HealthRow("UNKNOWN", ToEpoch(_museLastOkUtc), "", "checking sign-in");
            }
            else
            {
                rows["muse"] = HostEngine.HealthRow("DOWN", ToEpoch(_museLastOkUtc), "", string.IsNullOrEmpty(grant.Error) ? "Muse sign-in: " + grant.State : grant.Error);
            }

            // Phone(s): one summary row (spec §3.1) — worst state across
            // devices. No phone paired → DOWN. A paired phone is OK when
            // its channel exists (the engine's relay link is up); the
            // last_ok is the last stream activity, which the tick
            // observes directly. Per-device Android version is omitted
            // in v1 (decision 4); device names are not smuggled into any
            // field — the §2.1 shape has no field for them.
            var paired = _config.Devices;
            var streaming = _engine.ActiveDevices;
            if (paired.Count == 0)
            {
                rows["phone"] = HostEngine.HealthRow("DOWN", 0, "", "no phone paired");
            }
            else if (streaming.Count > 0)
            {
                _phoneLastStreamingUtc = nowUtc;
                rows["phone"] = HostEngine.HealthRow("OK", nowEpoch, "", "");
            }
            else if (!_engine.RelayConnected)
            {
                rows["phone"] = HostEngine.HealthRow("DOWN", ToEpoch(_phoneLastStreamingUtc), "", "relay disconnected — no phone can connect");
            }
            else
            {
                rows["phone"] = HostEngine.HealthRow("OK", ToEpoch(_phoneLastStreamingUtc), "", "");
            }

            _engine.PublishHealthSnapshot(rows);
        }
        catch (Exception)
        {
            // Last published snapshot stays; the next tick retries.
            // Never let snapshot collection break the 2s tick.
        }
    }

    // 100.016 (Worker B) — diagnostics refresh (spec §5.3): the fourth
    // call on the existing 2s tick, reading the `health` op through the
    // service like everything else, so the Windows view can never
    // disagree with the Android view about the same snapshot. Same
    // _activityPolling-style in-flight guard, so a slow service call
    // never stacks; a failed poll leaves the last good snapshot in
    // place and the footer ages into STALE. Never throws, never blocks
    // the UI thread.
    private async Task RefreshDiagnosticsAsync()
    {
        if (_chat is null || _diagnosticsPolling || IsDisposed || !IsHandleCreated) return;
        if (_diagnosticsGroupBox is null) return;
        _diagnosticsPolling = true;
        try
        {
            var data = await _chat.CallAsync(new { op = "health" });
            if (IsDisposed || !IsHandleCreated) return;
            if (data.TryGetProperty("as_of", out var asOfValue) && asOfValue.ValueKind == JsonValueKind.Number
                && asOfValue.TryGetDouble(out var asOfSeconds) && asOfSeconds > 0)
            {
                var asOf = DateTimeOffset.FromUnixTimeSeconds((long)asOfSeconds);
                _lastDiagnosticsAsOfUtc = asOf > DateTimeOffset.UtcNow.AddSeconds(5) ? DateTimeOffset.UtcNow : asOf;
            }
            if (!data.TryGetProperty("components", out var components) || components.ValueKind != JsonValueKind.Object) return;
            var rows = new Dictionary<string, DiagnosticsRow>(StringComparer.Ordinal);
            foreach (var component in components.EnumerateObject())
            {
                var row = component.Value;
                if (row.ValueKind != JsonValueKind.Object) continue;
                var status = row.TryGetProperty("status", out var statusValue) && statusValue.ValueKind == JsonValueKind.String ? statusValue.GetString() ?? "UNKNOWN" : "UNKNOWN";
                if (status is not ("OK" or "DEGRADED" or "DOWN" or "UNKNOWN" or "IDLE")) status = "UNKNOWN";
                var lastOk = row.TryGetProperty("last_ok", out var lastOkValue) && lastOkValue.ValueKind == JsonValueKind.Number && lastOkValue.TryGetInt64(out var lastOkSeconds) && lastOkSeconds > 0 ? lastOkSeconds : 0;
                var version = row.TryGetProperty("version", out var versionValue) && versionValue.ValueKind == JsonValueKind.String ? versionValue.GetString() ?? "" : "";
                var error = row.TryGetProperty("error", out var errorValue) && errorValue.ValueKind == JsonValueKind.String ? errorValue.GetString() ?? "" : "";
                var level = row.TryGetProperty("level", out var levelValue) && levelValue.ValueKind == JsonValueKind.String ? levelValue.GetString() ?? "INFO" : "INFO";
                if (level is not ("DEBUG" or "INFO" or "WARN" or "ERROR")) level = "INFO";
                rows[component.Name] = new DiagnosticsRow(status, lastOk, version, error, level);
            }
            // Only replace the rendered snapshot with a complete one:
            // a partial components map (old service) must not blank
            // rows the view already shows.
            var complete = true;
            foreach (var key in DiagnosticsKeys)
                if (!rows.ContainsKey(key)) complete = false;
            if (!complete) return;
            _diagnosticsRows.Clear();
            foreach (var pair in rows) _diagnosticsRows[pair.Key] = pair.Value;
            RenderDiagnostics();
        }
        catch (Exception)
        {
            // Last good text stays; the next poll retries.
        }
        finally { _diagnosticsPolling = false; }
    }

    // 100.017 (Worker B) — forward new runtime_stream events to the
    // phone (spec §3.2). One loopback op call per tick, since = the
    // engine's oldest subscribed cursor; the engine fans the result out
    // (≤100 events per sealed frame, seq order; resynced → exactly one
    // stream.resynced, no partial catch-up). No-op when no phone is
    // subscribed. Never throws, never blocks the UI thread; a failed
    // tick leaves the cursors untouched so the next tick re-fetches
    // from the op — the service ring is the store (spec §7.6/§7.7).
    // Coalescing (spec §3.2 step 4, ≤100 ms per frame) is subsumed by
    // the 1s cadence: a burst lands in a single op batch, split only
    // by the 100-event frame cap. Backpressure: DROP — the in-flight
    // guard skips a tick while one is outstanding, and nothing is
    // queued anywhere on the host.
    private async Task ForwardStreamAsync()
    {
        if (_chat is null || _streamPolling || IsDisposed || !IsHandleCreated) return;
        if (!_engine.AnyStreamSubscriber) return;
        _streamPolling = true;
        try
        {
            var since = _engine.StreamSince();
            var data = await _chat.CallAsync(new { op = "runtime_stream", since = since, limit = 100 });
            if (IsDisposed || !IsHandleCreated) return;
            _engine.PublishStreamPoll(data);
        }
        catch (Exception)
        {
            // Last cursors stay; the next tick retries.
        }
        finally { _streamPolling = false; }
    }

    // 100.016 (Worker B) — repaint the Diagnostics GroupBox from the
    // last good `health` snapshot. Runs at the end of every RefreshView
    // (so ages count up with no traffic) and after every good poll.
    // Text/ForeColor update every call; Font (bold) changes only on a
    // stale transition, so no GDI font handle leaks (same rule as
    // UpdateActivityAge). Never throws; no snapshot yet renders
    // "not checked yet".
    private void RenderDiagnostics()
    {
        if (_diagnosticsGroupBox is null || _diagnosticsDots[0] is null || _diagnosticsAge is null || _diagnosticsSummary is null || _diagnosticsSummaryDot is null) return;
        if (IsDisposed || !IsHandleCreated) return;
        var now = DateTimeOffset.UtcNow;
        var down = 0;
        var degraded = 0;
        // 100.040: all-ok means every rendered key is present and OK —
        // the raw row count is not the test, because the service still
        // serves its own tunnel row (UNKNOWN) in the merged snapshot.
        var allOk = DiagnosticsKeys.All(key => _diagnosticsRows.ContainsKey(key));
        for (var i = 0; i < 12; i++)
        {
            var key = DiagnosticsKeys[i];
            if (!_diagnosticsRows.TryGetValue(key, out var row))
            {
                _diagnosticsDots[i].ForeColor = Color.FromArgb(91, 107, 122);
                _diagnosticsStates[i].Text = "UNKNOWN";
                _diagnosticsStates[i].ForeColor = Color.FromArgb(91, 107, 122);
                _diagnosticsAges[i].Text = "never";
                _diagnosticsAges[i].ForeColor = Color.FromArgb(91, 107, 122);
                if (_diagnosticsRowStale[i])
                {
                    _diagnosticsRowStale[i] = false;
                    _diagnosticsAges[i].Font = new Font(_diagnosticsAges[i].Font, FontStyle.Regular);
                }
                _diagnosticsVersions[i].Text = "";
                _diagnosticsErrors[i].Text = "not checked yet";
                allOk = false;
                continue;
            }
            var color = row.Status switch
            {
                "OK" => Color.FromArgb(46, 125, 79),
                "DEGRADED" => Color.FromArgb(138, 90, 0),
                "DOWN" => Color.FromArgb(179, 38, 30),
                _ => Color.FromArgb(91, 107, 122),
            };
            _diagnosticsDots[i].ForeColor = color;
            _diagnosticsStates[i].Text = row.Status;
            _diagnosticsStates[i].ForeColor = color;
            if (row.Status == "DOWN") down++;
            else if (row.Status == "DEGRADED") degraded++;
            if (row.Status != "OK") allOk = false;
            var rowStale = false;
            if (row.LastOkEpoch <= 0)
            {
                _diagnosticsAges[i].Text = "never";
                _diagnosticsAges[i].ForeColor = Color.FromArgb(91, 107, 122);
            }
            else
            {
                var age = now - DateTimeOffset.FromUnixTimeSeconds(row.LastOkEpoch);
                if (age < TimeSpan.Zero) age = TimeSpan.Zero;
                rowStale = age > TimeSpan.FromSeconds(6);
                // A row whose last_ok is >6s old shows its age in red
                // bold with the same STALE wording as the footer (§5.3).
                _diagnosticsAges[i].Text = rowStale ? "STALE — no update for " + FormatAge(age) : FormatAge(age) + " ago";
                _diagnosticsAges[i].ForeColor = rowStale ? Color.FromArgb(179, 38, 30) : Color.FromArgb(91, 107, 122);
            }
            if (rowStale != _diagnosticsRowStale[i])
            {
                _diagnosticsRowStale[i] = rowStale;
                _diagnosticsAges[i].Font = new Font(_diagnosticsAges[i].Font, rowStale ? FontStyle.Bold : FontStyle.Regular);
            }
            _diagnosticsVersions[i].Text = row.Version;
            _diagnosticsErrors[i].Text = row.Error;
        }
        if (_lastDiagnosticsAsOfUtc == DateTimeOffset.MinValue)
        {
            _diagnosticsAge.Text = "age unknown";
            _diagnosticsAge.ForeColor = Color.FromArgb(91, 107, 122);
        }
        else
        {
            var pollAge = now - _lastDiagnosticsAsOfUtc;
            if (pollAge < TimeSpan.Zero) pollAge = TimeSpan.Zero;
            var stale = pollAge > TimeSpan.FromSeconds(6);
            _diagnosticsAge.Text = stale ? "STALE — no update for " + FormatAge(pollAge) : "snapshot updated " + FormatAge(pollAge) + " ago";
            _diagnosticsAge.ForeColor = stale ? Color.FromArgb(179, 38, 30) : Color.FromArgb(91, 107, 122);
            if (stale != _diagnosticsStale)
            {
                _diagnosticsStale = stale;
                _diagnosticsAge.Font = new Font(_diagnosticsAge.Font, stale ? FontStyle.Bold : FontStyle.Regular);
            }
        }
        if (_diagnosticsRows.Count == 0)
        {
            _diagnosticsSummary.Text = "Diagnostics — not checked yet";
            _diagnosticsSummaryDot.ForeColor = Color.FromArgb(91, 107, 122);
        }
        else
        {
            _diagnosticsSummary.Text = $"Diagnostics — {down} down, {degraded} degraded";
            _diagnosticsSummaryDot.ForeColor = down > 0 ? Color.FromArgb(179, 38, 30) : degraded > 0 ? Color.FromArgb(138, 90, 0) : allOk ? Color.FromArgb(46, 125, 79) : Color.FromArgb(91, 107, 122);
        }
    }

    // 100.016 (Worker B) — expand/collapse the Diagnostics GroupBox.
    // Collapsed shows only the summary line; expanded shows the rows,
    // footer, and Approvals section. The state is not persisted (no E22
    // mechanism exists — decision 6 fallback), so every start is
    // collapsed. Heights: collapsed 44, expanded 404 with the Approvals
    // section open, 384 with it closed.
    private void SetDiagnosticsExpanded(bool expanded)
    {
        if (_diagnosticsGroupBox is null || _diagnosticsRoot is null || _diagnosticsRowIndex < 0 || _diagnosticsBody is null || _diagnosticsToggle is null) return;
        _diagnosticsExpanded = expanded;
        _diagnosticsBody.Visible = expanded;
        _diagnosticsToggle.Text = expanded ? "\u25be" : "\u25b8";
        _diagnosticsRoot.RowStyles[_diagnosticsRowIndex].Height = expanded ? (_approvalsExpanded ? 404 : 384) : 44;
    }

    private void SetApprovalsExpanded(bool expanded)
    {
        if (_diagnosticsBody is null || _approvalsEmpty is null || _approvalsToggle is null || _diagnosticsRoot is null || _diagnosticsRowIndex < 0) return;
        _approvalsExpanded = expanded;
        _approvalsEmpty.Visible = expanded;
        _approvalsToggle.Text = expanded ? "\u25be" : "\u25b8";
        _diagnosticsBody.RowStyles[3].Height = expanded ? 20 : 0;
        if (_diagnosticsExpanded)
            _diagnosticsRoot.RowStyles[_diagnosticsRowIndex].Height = expanded ? 404 : 384;
    }

    // 100.016 (Worker B) — "Copy diagnostics" (spec §5.4): the view's
    // only action. The scrubbed export is built by the service's
    // `diagnostics_export` op (Worker A) — the Windows path and the
    // Android path share that one scrubber in the service, so C# never
    // writes a second scrubber and never reads a log file itself. The
    // button saves the text to Documents and puts the same text on the
    // clipboard. Never throws out of the click handler.
    // 100.029 (Worker C) — the call passes the export options E41 §6.1
    // adds: level_filter ("all" | "warn_error", from the WARN/ERROR
    // button) and include_debug (from the Windows-only checkbox). An
    // older service ignores the extra fields, so the call stays
    // compatible with the 100.026 service.
    // 100.029 (Worker C) — reflect the WARN/ERROR-first export state
    // on its button: the label names the tail the next export asks
    // for, so the button never silently disagrees with the state.
    private void UpdateDiagnosticsExportOptions()
    {
        if (_diagnosticsWarnErrorButton is null) return;
        _diagnosticsWarnErrorButton.Text = _diagnosticsWarnErrorOnly ? "WARN/ERROR only" : "All levels";
    }

    private async Task CopyDiagnosticsAsync()
    {
        if (_chat is null || _diagnosticsCopying || IsDisposed || !IsHandleCreated) return;
        if (_diagnosticsCopyButton is null) return;
        _diagnosticsCopying = true;
        _diagnosticsCopyButton.Enabled = false;
        try
        {
            var data = await _chat.CallAsync(new { op = "diagnostics_export", level_filter = _diagnosticsWarnErrorOnly ? "warn_error" : "all", include_debug = _diagnosticsIncludeDebug });
            if (IsDisposed || !IsHandleCreated) return;
            string? text = null;
            string? filename = null;
            if (data.ValueKind == JsonValueKind.String) text = data.GetString();
            else if (data.ValueKind == JsonValueKind.Object)
            {
                if (data.TryGetProperty("text", out var textValue) && textValue.ValueKind == JsonValueKind.String) text = textValue.GetString();
                if (data.TryGetProperty("filename", out var fileValue) && fileValue.ValueKind == JsonValueKind.String) filename = fileValue.GetString();
            }
            if (string.IsNullOrEmpty(text))
            {
                MessageBox.Show(this, "Diagnostics export is unavailable with this service version.", "Deskhand");
                return;
            }
            if (string.IsNullOrWhiteSpace(filename))
                filename = "deskhand-diagnostics-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".txt";
            var documents = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
            using var dialog = new System.Windows.Forms.SaveFileDialog { FileName = filename, InitialDirectory = documents, Filter = "Text files (*.txt)|*.txt|All files (*.*)|*.*", DefaultExt = "txt" };
            if (dialog.ShowDialog(this) != DialogResult.OK) return;
            File.WriteAllText(dialog.FileName, text);
            Clipboard.SetText(text);
        }
        catch (Exception ex)
        {
            if (!IsDisposed && IsHandleCreated)
                MessageBox.Show(this, "Could not save diagnostics: " + ex.Message, "Deskhand");
        }
        finally
        {
            _diagnosticsCopying = false;
            if (!IsDisposed && _diagnosticsCopyButton is not null) _diagnosticsCopyButton.Enabled = true;
        }
    }
    private async Task ApplySettings()
    {
        if (_embeddedNode is not null && (int)_portBox.Value != _config.RelayPort)
        {
            MessageBox.Show(this, "Close and restart Deskhand before changing the relay port while the embedded connection is active.", "Deskhand");
            return;
        }
        var relayUrl = _relayBox.Text.Trim().TrimEnd('/');
        if (!Uri.TryCreate(relayUrl, UriKind.Absolute, out var uri) || (uri.Scheme != "ws" && uri.Scheme != "wss"))
        {
            MessageBox.Show(this, "The relay address must start with ws:// or wss://", "Deskhand");
            return;
        }
        // 100.040 — the Cloudflare tunnel is removed: a tunnel address
        // can never be saved here (Config.Load would migrate it away
        // on the next start, so accepting it would strand the setting).
        if (AppConfig.IsTunnelUrl(relayUrl))
        {
            MessageBox.Show(this, "The relay address must not be a Cloudflare tunnel address. Use the Tailscale or LAN address instead.", "Deskhand");
            return;
        }

        var oldDeviceName = _config.DeviceName;
        var oldRelayUrl = _config.RelayUrl;
        var oldRelayPort = _config.RelayPort;
        var oldRunBuiltInRelay = _config.RunBuiltInRelay;
        var oldStartWithWindows = _config.StartWithWindows;
        _config.DeviceName = string.IsNullOrWhiteSpace(_nameBox.Text) ? Environment.MachineName : _nameBox.Text.Trim();
        _config.RelayUrl = relayUrl;
        _config.RunBuiltInRelay = _builtInBox.Checked;
        _config.RelayPort = (int)_portBox.Value;
        _config.StartWithWindows = _startupBox.Checked;
        // B2-A B13: Save() used to throw out of this async void handler
        // (disk full / AV lock / permissions) and crash the app after the
        // in-memory config was already mutated. On failure, restore the
        // old values and report the error instead of claiming success.
        try
        {
            _config.Save();
            SetStartup(_config.StartWithWindows);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.SecurityException)
        {
            _config.DeviceName = oldDeviceName;
            _config.RelayUrl = oldRelayUrl;
            _config.RelayPort = oldRelayPort;
            _config.RunBuiltInRelay = oldRunBuiltInRelay;
            _config.StartWithWindows = oldStartWithWindows;
            MessageBox.Show(this, "Settings could not be saved: " + ex.Message, "Deskhand");
            return;
        }

        if (_config.RunBuiltInRelay)
        {
            await _relay.StartAsync(_config.RelayPort);
            if (_relay.Error is not null)
            {
                RefreshView();
                MessageBox.Show(this, _relay.Error, "Deskhand");
                return;
            }
        }
        else await _relay.StopAsync();
        if (relayUrl != oldRelayUrl || _config.RelayPort != oldRelayPort || _config.RunBuiltInRelay != oldRunBuiltInRelay || _config.StartWithWindows != oldStartWithWindows) _engine.Restart();
        RefreshView();
        MessageBox.Show(this, "Settings applied. Phones paired with a different relay address must be paired again.", "Deskhand");
    }

    private void ReviewAssistantApproval()
    {
        var request = _engine.Approvals.Pending().FirstOrDefault();
        if (request is null) return;
        using var dialog = new Form { Text = "Deskhand AI approval", StartPosition = FormStartPosition.CenterParent, Padding = new Padding(16),
            FormBorderStyle = FormBorderStyle.SizableToolWindow, ClientSize = new Size(650, 400), MinimumSize = new Size(450, 300) };
        var details = new TextBox { Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both,
            WordWrap = false, Text = $"AI requests: {request.Operation}\r\n\r\nTarget: {request.Target}\r\n\r\nPurpose: {request.Purpose}", Font = new Font("Segoe UI", 9) };
        var buttons = new FlowLayoutPanel { Dock = DockStyle.Bottom, FlowDirection = FlowDirection.RightToLeft, Height = 44, Padding = new Padding(4) };
        var deny = new Button { Text = "Deny", DialogResult = DialogResult.No, AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(255, 255, 255), ForeColor = Color.FromArgb(179, 38, 30), FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
        deny.FlatAppearance.BorderColor = Color.FromArgb(179, 38, 30);
        var approve = new Button { Text = "Approve", DialogResult = DialogResult.Yes, AutoSize = true, Padding = new Padding(10, 4, 10, 4), MinimumSize = new Size(0, 30), BackColor = Color.FromArgb(47, 107, 102), ForeColor = Color.White, FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
        approve.FlatAppearance.BorderColor = Color.FromArgb(47, 107, 102);
        approve.FlatAppearance.MouseOverBackColor = Color.FromArgb(38, 89, 84);
        buttons.Controls.Add(deny); buttons.Controls.Add(approve);
        dialog.Controls.Add(details); dialog.Controls.Add(buttons); dialog.AcceptButton = deny; dialog.CancelButton = deny;
        var choice = dialog.ShowDialog(this);
        if (!_engine.Approvals.Resolve(request.Id, choice == DialogResult.Yes))
            MessageBox.Show(this, "This request has already expired, been cancelled, or been answered.", "Deskhand AI approval");
        RefreshView();
    }

    public static void SetStartup(bool enabled)
    {
        using var key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true);
        if (key is null) return;
        if (enabled) key.SetValue("Deskhand", $"\"{Environment.ProcessPath}\" --minimized");
        else key.DeleteValue("Deskhand", false);
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        _embeddedLifetime.Cancel();
        _embeddedNode?.Dispose();
        _tray.Visible = false;
        base.OnFormClosing(e);
    }

}
