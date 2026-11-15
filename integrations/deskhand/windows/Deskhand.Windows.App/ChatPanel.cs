using System.Text;
using System.Text.Json;

namespace Deskhand.Windows.App;

public sealed class ChatPanel : UserControl
{
    private readonly AssistantClient _chat;
    private readonly AppConfig? _config;
    private bool _agenticMode, _accessPolling;
    private long _accessRevision;
    private readonly Label _accessStatus = new() { AutoSize = true, MaximumSize = new Size(650, 0) };
    private readonly ComboBox _models = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 210 };
    private readonly ComboBox _effort = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 80 };
    private readonly ComboBox _route = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 80 };
    private readonly TextBox _message = new() { Dock = DockStyle.Fill, PlaceholderText = "Message your assistant" };
    private readonly TextBox _history = new() { Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical };
    private readonly Label _status = new() { AutoSize = true, MaximumSize = new Size(600, 0) };
    private readonly Button _send = new() { Text = "Send", AutoSize = true };
    private readonly Button _museSignIn = new() { Text = "Muse sign-in", AutoSize = true, Visible = false };
    private readonly Button _museSettings = new() { Text = "Advanced", AutoSize = true, Visible = false };
    private DateTimeOffset _lastAccessAttempt = DateTimeOffset.MinValue;
    private JsonElement[] _routes = [];
    private string _routeMode = "Auto";
    private string _routeReason = "";
    private bool _applyingRoute;

    public ChatPanel(AssistantClient chat, AppConfig? config = null)
    {
        _chat = chat;
        _config = config;
        _agenticMode = false;
        Dock = DockStyle.Fill;
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4 };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 150));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 24));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 34));
        var choices = new FlowLayoutPanel { Dock = DockStyle.Fill };
        var refresh = new Button { Text = "Models", AutoSize = true };
        var local = new Button { Text = "Add local model", AutoSize = true };
        choices.Controls.AddRange([_models, _effort, _route, refresh, local, _museSignIn, _museSettings]);
        choices.Controls.Add(new Label { Text = "Can view your screen, use apps, work with files, and run commands. Destructive actions still need approval.", AutoSize = true, MaximumSize = new Size(650, 0) });
        choices.Controls.Add(_accessStatus);
        layout.Controls.Add(choices, 0, 0); layout.Controls.Add(_history, 0, 1); layout.Controls.Add(_status, 0, 2);
        var compose = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2 };
        compose.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        compose.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        compose.Controls.Add(_message, 0, 0); compose.Controls.Add(_send, 1, 0);
        layout.Controls.Add(compose, 0, 3); Controls.Add(layout);
        refresh.Click += async (_, _) => await LoadModels();
        local.Click += async (_, _) => await ConfigureLocal();
        _museSettings.Click += (_, _) => { using var dialog = new MuseConnectionForm(_chat); dialog.ShowDialog(this); };
        _museSignIn.Click += async (_, _) =>
        {
            try
            {
                var result = await _chat.CallAsync(new { op = "muse_signin" });
                _status.Text = result.GetProperty("status").GetString();
                MessageBox.Show(this, "Sign in to Muse in the opened window, then return here and choose OK.", "Muse sign-in");
                await _chat.CallAsync(new { op = "muse_signin_done" });
                // The service owns auto-resume after sign-in (100.013); the
                // panel only refreshes the always-on status. No resume call
                // is made from here.
                await EnsureBridgeAsync();
                // Full PC access is always on (no toggle in the UI): enable
                // it now that sign-in is done; the access poll retries it.
                _ = EnableAccessAsync();
            }
            catch (Exception ex) { _status.Text = ex.Message; }
        };
        _send.Click += async (_, _) => await Send();
        _message.KeyDown += async (_, e) => { if (e.KeyCode == Keys.Enter) { e.SuppressKeyPress = true; await Send(); } };
        _models.SelectedIndexChanged += async (_, _) =>
        {
            _accessRevision++;
            _effort.Items.Clear();
            // B2-A B7: this is an async void handler with no try/catch, so
            // the old GetProperty calls (missing "efforts"/"effort"/
            // "assistant" in a malformed catalog route) threw straight onto
            // the UI thread. Every field is now read with TryGetProperty.
            if (_models.SelectedIndex < 0 || _models.SelectedIndex >= _routes.Length) return;
            var route = _routes[_models.SelectedIndex];
            _agenticMode = false;
            if (route.TryGetProperty("efforts", out var efforts) && efforts.ValueKind == JsonValueKind.Array)
                foreach (var value in efforts.EnumerateArray())
                    if (value.ValueKind == JsonValueKind.String) _effort.Items.Add(value.GetString()!);
            if (_effort.Items.Count == 0) _effort.Items.Add("");
            _effort.SelectedItem = route.TryGetProperty("effort", out var effort) && effort.ValueKind == JsonValueKind.String ? effort.GetString() ?? "" : "";
            if (_effort.SelectedIndex < 0) _effort.SelectedIndex = 0;
            await RefreshAccess();
            // Selecting Muse starts the MuseBridge (process only; remote
            // control stays gated by the bridge's own flag). Other models
            // leave the bridge alone. Failures only set the status line;
            // they never block selection or sending.
            if (route.TryGetProperty("assistant", out var assistant) && assistant.ValueKind == JsonValueKind.String && assistant.GetString() == "muse")
                _ = EnsureBridgeAsync();
        };
        _route.Items.AddRange(["Auto", "Fast", "Smart"]);
        _route.SelectedItem = "Auto";
        _route.SelectedIndexChanged += async (_, _) =>
        {
            if (_applyingRoute || _route.SelectedItem is not string mode) return;
            _routeMode = mode;
            _routeReason = "";
            // Batch 2 router: Fast/Smart persist the always_smart flag
            // server-side (Smart = true, Fast/Auto = false) and submit
            // normally. Failures only set the status line.
            try { await _chat.CallAsync(new { op = "router_settings", always_smart = mode == "Smart" }); }
            catch (Exception ex) { _status.Text = ex.Message; }
        };
        var accessTimer = new System.Windows.Forms.Timer { Interval = 2000 };
        accessTimer.Tick += async (_, _) => await RefreshAccess();
        accessTimer.Start();
        Disposed += (_, _) => accessTimer.Dispose();
        _chat.HistoryChanged += OnHistory;
        Disposed += (_, _) => _chat.HistoryChanged -= OnHistory;
        Load += async (_, _) => { await LoadModels(); try { OnHistory(await _chat.CallAsync(new { op = "history" })); } catch (Exception ex) { _status.Text = ex.Message; } };
    }

    // Full PC access is always on (no toggle in the UI). Failures only set the status line; the
    // periodic access refresh retries until it succeeds.
    private async Task EnableAccessAsync()
    {
        if (_models.SelectedIndex < 0 || IsDisposed) return;
        var route = _routes[_models.SelectedIndex];
        _lastAccessAttempt = DateTimeOffset.UtcNow;
        _accessStatus.Text = "Requesting access…";
        try { await _chat.CallAsync(new { op = "access_set", assistant = route.GetProperty("assistant").GetString(), account = route.GetProperty("account").GetString(), enabled = true }); }
        catch (Exception ex) { _status.Text = ex.Message; }
        finally { await RefreshAccess(); }
    }

    private async Task RefreshAccess()
    {
        if (_accessPolling || _models.SelectedIndex < 0 || IsDisposed) return;
        _accessPolling = true;
        var revision = _accessRevision;
        var route = _routes[_models.SelectedIndex];
        try
        {
            var data = await _chat.CallAsync(new { op = "access_status", assistant = route.GetProperty("assistant").GetString(), account = route.GetProperty("account").GetString() });
            if (revision != _accessRevision || _models.SelectedIndex < 0 || route.GetRawText() != _routes[_models.SelectedIndex].GetRawText()) return;
            _agenticMode = data.GetProperty("state").GetString() == "On";
            var account = route.GetProperty("account").GetString();
            if (route.TryGetProperty("accountLabel", out var label)) account = label.GetString();
            _accessStatus.Text = route.GetProperty("assistant").GetString() + (string.IsNullOrEmpty(account) ? "" : " · " + account) + " — " + data.GetProperty("state").GetString();
            if (data.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.String) _accessStatus.Text += ": " + error.GetString();
            // Retry local authority after a transport loss, regardless of the
            // selected provider. Pause still keeps it off.
            if (data.GetProperty("supported").GetBoolean()
                && data.GetProperty("state").GetString() != "On" && _config?.Paused != true
                && DateTimeOffset.UtcNow - _lastAccessAttempt > TimeSpan.FromSeconds(60))
                _ = EnableAccessAsync();
        }
        catch (Exception ex)
        {
            if (revision != _accessRevision) return;
            _agenticMode = false; _accessStatus.Text = "Off — " + ex.Message;
        }
        finally { _accessPolling = false; }
    }

    private async Task ConfigureLocal()
    {
        using var dialog = new Form { Text = "Add local model", StartPosition = FormStartPosition.CenterParent,
            FormBorderStyle = FormBorderStyle.FixedDialog, MaximizeBox = false, MinimizeBox = false,
            ClientSize = new Size(470, 220), ShowInTaskbar = false };
        var model = new TextBox { Dock = DockStyle.Fill, PlaceholderText = "Example: qwen3.5:4b" };
        var endpoint = new TextBox { Dock = DockStyle.Fill, Text = "http://127.0.0.1:11434" };
        var managed = new CheckBox { Text = "Start local runtime with Deskhand", AutoSize = true, Checked = true, Anchor = AnchorStyles.Left };
        var openai = new CheckBox { Text = "Use OpenAI-compatible local server", AutoSize = true, Anchor = AnchorStyles.Left, Enabled = false };
        managed.CheckedChanged += (_, _) => { endpoint.Enabled = !managed.Checked; openai.Enabled = !managed.Checked; };
        endpoint.Enabled = false;
        var grid = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(12), ColumnCount = 2, RowCount = 5 };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 110));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        grid.RowStyles.Add(new RowStyle(SizeType.Absolute, 36));
        grid.RowStyles.Add(new RowStyle(SizeType.Absolute, 36));
        grid.RowStyles.Add(new RowStyle(SizeType.Absolute, 36));
        grid.RowStyles.Add(new RowStyle(SizeType.Absolute, 36));
        grid.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        grid.Controls.Add(new Label { Text = "Model name", AutoSize = true, Anchor = AnchorStyles.Left }, 0, 0);
        grid.Controls.Add(model, 1, 0);
        grid.Controls.Add(new Label { Text = "Local server", AutoSize = true, Anchor = AnchorStyles.Left }, 0, 1);
        grid.Controls.Add(endpoint, 1, 1);
        grid.Controls.Add(managed, 1, 2);
        grid.Controls.Add(openai, 1, 3);
        var save = new Button { Text = "Save", DialogResult = DialogResult.OK, AutoSize = true, Anchor = AnchorStyles.Right };
        grid.Controls.Add(save, 1, 4);
        dialog.AcceptButton = save;
        dialog.Controls.Add(grid);
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        try
        {
            if (managed.Checked) await _chat.CallAsync(new { op = "local_runtime_start", model = model.Text.Trim() });
            await _chat.CallAsync(new { op = "local_configure", model = model.Text.Trim(), endpoint = managed.Checked ? "managed" : (openai.Checked ? "openai:" : "") + endpoint.Text.Trim() });
            await LoadModels();
            _status.Text = managed.Checked ? "Local runtime is ready and the selected model is available." : "Local model saved. Its server must be running when used.";
        }
        catch (Exception ex) { _status.Text = ex.Message; }
    }

    private async Task LoadModels()
    {
        try
        {
            _status.Text = "Loading assistants…";
            var data = await _chat.CallAsync(new { op = "catalog", refresh = true });
            SetRoutes(data.GetProperty("routes").EnumerateArray().Select(x => x.Clone()).ToArray());
            // Batch 2: fetch the persisted router setting once so the
            // selector reflects it (Smart when always_smart is on).
            try
            {
                var settings = await _chat.CallAsync(new { op = "router_settings" });
                _applyingRoute = true;
                try { _route.SelectedItem = settings.TryGetProperty("always_smart", out var smart) && smart.ValueKind == JsonValueKind.True ? "Smart" : "Auto"; }
                finally { _applyingRoute = false; }
                _routeMode = _route.SelectedItem?.ToString() ?? "Auto";
            }
            catch (Exception) { /* Older service: router stays on the local default (Auto). */ }
            _status.Text = data.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.String ? error.GetString() : "Ready";
            if (_models.SelectedIndex >= 0 && _routes[_models.SelectedIndex].GetProperty("assistant").GetString() == "muse")
                _ = EnsureBridgeAsync();
        }
        catch (Exception ex) { _status.Text = ex.Message; }
    }

    private async Task EnsureBridgeAsync()
    {
        try
        {
            var result = await _chat.CallAsync(new { op = "bridge_start" });
            var running = result.TryGetProperty("running", out var runningValue) && runningValue.GetBoolean();
            var health = result.TryGetProperty("health", out var healthValue) && healthValue.GetBoolean();
            if (!running)
            {
                _status.Text = "Muse: starting… " + (result.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.String ? error.GetString() : "not started");
                return;
            }
            if (!health)
            {
                _status.Text = "Muse: starting… MuseBridge health not answering yet";
                return;
            }
            // F1: bridge running+healthy says nothing about sign-in or the
            // delivery hold, so it must not imply ready on its own. Read
            // muse_status (same payload the connection form reads) and
            // require delivery_ready AND cloud_ready. delivery_ready is
            // True after ensure_chat_ready() succeeded, but that call is
            // transport-only and can succeed while signed out (the relay
            // path has no cloud_ready gate), so it is not sign-in proof
            // on its own. cloud_ready (round 2) is the payload's sign-in
            // field, read from the same browser attribute the
            // auto-release loop gates on. The https session/actions fields
            // are env-var config flags, not sign-in state. A failed
            // muse_status call falls through to "starting…" below.
            var museStatus = await _chat.CallAsync(new { op = "muse_status" });
            var readiness = MuseReadiness.Inspect(museStatus);
            _status.Text = "Muse: " + readiness.Detail;
        }
        catch (Exception ex) { _status.Text = "Muse: starting… " + ex.Message; }
    }

    internal void SetRoutes(JsonElement[] routes)
    {
        var selected = _models.SelectedItem?.ToString();
        _routes = routes;
        var hasMuse = routes.Any(route => route.GetProperty("assistant").GetString() == "muse");
        _museSignIn.Visible = hasMuse;
        _museSettings.Visible = hasMuse;
        _models.Items.Clear();
        foreach (var route in _routes) _models.Items.Add(route.GetProperty("name").GetString()!);
        _models.SelectedItem = selected;
        if (_models.SelectedIndex < 0 && _routes.Length > 0)
        {
            var museIndex = Array.FindIndex(_routes, route => route.GetProperty("assistant").GetString() == "muse");
            _models.SelectedIndex = museIndex >= 0 ? museIndex : 0;
        }
    }



    private async Task Send()
    {
        if (!_send.Enabled || string.IsNullOrWhiteSpace(_message.Text) || _models.SelectedIndex < 0) return;
        var route = _routes[_models.SelectedIndex];
        var text = _message.Text;
        var turn = Guid.NewGuid().ToString();
        _send.Enabled = false;
        try
        {
            // Auto asks the service which route fits this
            // message (route_suggest) and submits with that exact catalog
            // route, so the picked route's reason can be shown and agent
            // admission still works. A failed suggestion falls back to the
            // selected model. Fast/Smart submit normally: the always_smart
            // flag was already persisted when the mode was picked.
            _routeReason = "";
            // An explicitly selected Muse route must stay on Muse even for
            // short messages such as ping.
            if (_routeMode == "Auto" && route.GetProperty("assistant").GetString() != "muse")
            {
                try
                {
                    var suggestion = await _chat.CallAsync(new { op = "route_suggest", text });
                    if (suggestion.TryGetProperty("route", out var picked) && picked.ValueKind == JsonValueKind.Object
                        && picked.TryGetProperty("assistant", out _) && picked.TryGetProperty("model", out _) && picked.TryGetProperty("account", out _))
                    {
                        route = picked.Clone();
                        var kind = suggestion.TryGetProperty("kind", out var kindValue) && kindValue.ValueKind == JsonValueKind.String ? kindValue.GetString() ?? "" : "";
                        var reason = suggestion.TryGetProperty("reason", out var reasonValue) && reasonValue.ValueKind == JsonValueKind.String ? reasonValue.GetString() ?? "" : "";
                        _routeReason = "Auto \u2192 " + kind + ": " + reason;
                        var pickedIndex = Array.FindIndex(_routes, candidate =>
                            candidate.GetProperty("assistant").GetString() == picked.GetProperty("assistant").GetString() &&
                            candidate.GetProperty("model").GetString() == picked.GetProperty("model").GetString() &&
                            candidate.GetProperty("account").GetString() == picked.GetProperty("account").GetString());
                        if (pickedIndex >= 0) _models.SelectedIndex = pickedIndex;
                    }
                }
                catch (Exception) { /* Fall back to the selected model below. */ }
            }
            var effort = _routeMode == "Auto" && route.TryGetProperty("effort", out var pickedEffort) && pickedEffort.ValueKind == JsonValueKind.String
                ? pickedEffort.GetString()! : _effort.SelectedItem?.ToString() ?? "";
            await _chat.CallAsync(new { op = "submit", id = turn, text,
                assistant = route.GetProperty("assistant").GetString(), model = route.GetProperty("model").GetString(),
                account = route.GetProperty("account").GetString(), effort = effort,
                agent = _agenticMode });
            if (_message.Text == text) _message.Clear();
            _status.Text = "Queued" + (string.IsNullOrEmpty(_routeReason) ? "" : " \u2014 " + _routeReason);
        }
        catch (Exception ex) { _status.Text = ex.Message + " (turn " + turn + ")"; }
        finally { _send.Enabled = true; }
    }

    // B2-A B7: missing or mistyped fields read as "" instead of throwing.
    // OnHistory runs from HistoryChanged with no try/catch around the call,
    // so a malformed service payload used to crash the app from here.
    private static string JsonString(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? "" : "";

    private void OnHistory(JsonElement history)
    {
        if (!IsHandleCreated || IsDisposed) return;
        if (InvokeRequired) { BeginInvoke(() => OnHistory(history)); return; }
        var text = new StringBuilder();
        var connectorReplies = history.TryGetProperty("jettReplies", out var replies)
            ? replies.EnumerateArray().ToArray() : [];
        var connectorActions = history.TryGetProperty("jettActions", out var actions)
            ? actions.EnumerateArray().ToArray() : [];
        var assistantActions = history.TryGetProperty("assistantActions", out var recorded)
            ? recorded.EnumerateArray().ToArray() : [];
        var repliesByPrompt = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        foreach (var reply in connectorReplies)
        {
            var promptId = JsonString(reply, "prompt_id");
            if (!string.IsNullOrEmpty(promptId) && !repliesByPrompt.ContainsKey(promptId))
                repliesByPrompt.Add(promptId, reply);
        }
        var displayed = new HashSet<string>(StringComparer.Ordinal);
        var turns = history.TryGetProperty("turns", out var turnsValue) && turnsValue.ValueKind == JsonValueKind.Array
            ? turnsValue.EnumerateArray().ToArray() : [];
        foreach (var row in turns)
        {
            text.AppendLine("You: " + JsonString(row, "text"));
            text.Append(JsonString(row, "assistant")).Append(" · ").Append(JsonString(row, "model")).Append(" [").Append(JsonString(row, "status")).AppendLine("]");
            var promptId = JsonString(row, "id");
            foreach (var action in assistantActions.Where(item => JsonString(item, "TurnId") == promptId).Reverse())
            {
                text.Append("AI action ").Append(JsonString(action, "Name")).Append(" [").Append(JsonString(action, "Status")).Append(']');
                var actionError = JsonString(action, "Error");
                if (!string.IsNullOrEmpty(actionError)) text.Append(" · ").Append(actionError);
                text.AppendLine();
            }
            if (promptId is not null && repliesByPrompt.TryGetValue(promptId, out var jettReply))
            {
                text.Append("Assistant reply · Deskhand [").Append(JsonString(jettReply, "status")).AppendLine("]");
                text.AppendLine(JsonString(jettReply, "text"));
                var jettReplyId = JsonString(jettReply, "reply_id");
                AppendActions(text, connectorActions, jettReplyId);
                if (!string.IsNullOrEmpty(jettReplyId)) displayed.Add(jettReplyId);
            }
            else if (!string.IsNullOrEmpty(JsonString(row, "reply"))) text.AppendLine(JsonString(row, "reply"));
            // Batch 2: a dispatching turn with streamed partial text shows
            // the partial in place of the (absent) reply, marked
            // in-progress. The final reply replaces it (finish() clears
            // the partial server-side), never appends after it.
            else if (JsonString(row, "status") == "dispatching"
                && row.TryGetProperty("partial", out var partial) && partial.ValueKind == JsonValueKind.String
                && !string.IsNullOrEmpty(partial.GetString()))
                text.AppendLine(partial.GetString() + " \u2026");
            var rowError = JsonString(row, "error");
            if (!string.IsNullOrEmpty(rowError)) text.AppendLine(rowError);
            text.AppendLine();
        }
        foreach (var reply in connectorReplies.Where(reply => !displayed.Contains(JsonString(reply, "reply_id"))))
        {
            text.Append("Assistant reply · Deskhand [").Append(JsonString(reply, "status")).AppendLine("]");
            text.AppendLine(JsonString(reply, "text")).AppendLine();
            var orphanReplyId = JsonString(reply, "reply_id");
            AppendActions(text, connectorActions, orphanReplyId);
            if (!string.IsNullOrEmpty(orphanReplyId)) displayed.Add(orphanReplyId);
        }
        if (_history.Text != text.ToString()) { _history.Text = text.ToString(); _history.SelectionStart = _history.TextLength; _history.ScrollToCaret(); }
        foreach (var replyId in displayed)
            if (Guid.TryParseExact(replyId, "D", out var parsedReplyId)) _chat.AcknowledgeJettReplyOnWindows(parsedReplyId);
        if (turns.Length > 0)
            _status.Text = JsonString(turns[^1], "status") switch
            {
                "queued" => "Queued", "dispatching" => "Waiting for assistant…", "completed" => "Reply received",
                "unknown" => "Delivery unconfirmed; check the assistant before resending", "failed" => "Message failed", _ => "Ready"
            };
    }

    private static void AppendActions(StringBuilder text, JsonElement[] actions, string replyId)
    {
        foreach (var action in actions.Where(item => JsonString(item, "reply_id") == replyId))
        {
            text.Append("Action ").Append(JsonString(action, "name")).Append(" [")
                .Append(JsonString(action, "status")).Append(']');
            if (action.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.String)
                text.Append(" · ").Append(error.GetString());
            text.AppendLine();
        }
    }
}
