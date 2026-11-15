using System.Text.Json;
using System.Text.Json.Nodes;

namespace Deskhand.Windows.App;

// The staged connection controls do not expose clearing STOP or enabling the
// desktop. Those require the final host/bridge authorization handshake.
internal sealed class MuseConnectionForm : Form
{
    private readonly AssistantClient _chat;
    private readonly TextBox _address = new() { Width = 220 };
    private readonly NumericUpDown _port = new() { Minimum = 1, Maximum = 65535, Value = 8767, Width = 90 };
    private readonly Label _status = new() { AutoSize = true, MaximumSize = new Size(520, 0) };
    private JsonObject? _bridgeState;

    public MuseConnectionForm(AssistantClient chat)
    {
        _chat = chat;
        Text = "Muse connection";
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(560, 300);
        var layout = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(12), AutoScroll = true };
        var save = new Button { Text = "Save connection", AutoSize = true };
        var refresh = new Button { Text = "Refresh status", AutoSize = true };
        var stop = new Button { Text = "Stop connection", AutoSize = true };
        var copy = new Button { Text = "Copy bridge token", AutoSize = true };
        var addFolder = new Button { Text = "Add folder access", AutoSize = true };
        var removeFolder = new Button { Text = "Remove folder access", AutoSize = true };
        var permissions = new Button { Text = "Desktop permissions", AutoSize = true };
        var session = new Button { Text = "Timed access", AutoSize = true };
        layout.Controls.AddRange([new Label { Text = "Tailscale IP", AutoSize = true }, _address,
            new Label { Text = "Port", AutoSize = true }, _port, save, refresh, stop, copy, addFolder, removeFolder, permissions, session, _status]);
        Controls.Add(layout);
        save.Click += async (_, _) => await Action("configure", new { bind = _address.Text.Trim(), port = decimal.ToInt32(_port.Value) });
        stop.Click += async (_, _) => await Action("stop", new { });
        refresh.Click += async (_, _) => await RefreshStatus();
        addFolder.Click += async (_, _) => await AddFolder();
        removeFolder.Click += async (_, _) => await RemoveFolder();
        permissions.Click += async (_, _) => await ConfigurePermissions();
        session.Click += async (_, _) => await ConfigureSession();
        copy.Click += async (_, _) =>
        {
            try
            {
                var response = await _chat.CallAsync(new { op = "muse_action", action = "connection_details", payload = new { } });
                var token = response.GetProperty("result").GetProperty("token").GetString();
                if (string.IsNullOrWhiteSpace(token)) throw new InvalidOperationException("Bridge token is unavailable.");
                Clipboard.SetText(token);
                _status.Text = "Token copied. Paste it only into Muse's secure connector form.";
            }
            catch (Exception ex) { _status.Text = ex.Message; }
        };
        Shown += async (_, _) => await RefreshStatus();
    }

    private async Task Action(string action, object payload)
    {
        try
        {
            await _chat.CallAsync(new { op = "muse_action", action, payload });
            await RefreshStatus();
        }
        catch (Exception ex) { _status.Text = ex.Message; }
    }

    private async Task RefreshStatus()
    {
        try
        {
            var response = await _chat.CallAsync(new { op = "muse_status" });
            var bridge = response.GetProperty("bridge");
            _bridgeState = JsonNode.Parse(bridge.GetRawText())!.AsObject();
            _address.Text = bridge.GetProperty("bind").GetString() ?? "";
            _port.Value = bridge.GetProperty("port").GetInt32();
            var readiness = MuseReadiness.Inspect(response);
            _status.Text = "Muse: " + readiness.Detail;
        }
        catch (Exception ex) { _status.Text = ex.Message; }
    }

    private async Task AddFolder()
    {
        if (_bridgeState is null) { await RefreshStatus(); if (_bridgeState is null) return; }
        using var picker = new FolderBrowserDialog { Description = "Choose a folder Muse may read and edit" };
        if (picker.ShowDialog(this) != DialogResult.OK) return;
        using var options = new Form { Text = "Folder access", ClientSize = new Size(400, 160), StartPosition = FormStartPosition.CenterParent };
        var layout = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(12), FlowDirection = FlowDirection.TopDown };
        var read = new CheckBox { Text = "Read files", Checked = true, AutoSize = true };
        var write = new CheckBox { Text = "Edit files", AutoSize = true };
        var execute = new CheckBox { Text = "Run approved commands in this folder", AutoSize = true };
        layout.Controls.AddRange([read, write, execute, new Button { Text = "Save", DialogResult = DialogResult.OK, AutoSize = true }]);
        options.Controls.Add(layout);
        if (options.ShowDialog(this) != DialogResult.OK) return;
        if (execute.Checked && !ConfirmPhrase("ENABLE UNSANDBOXED EXECUTION")) return;
        var alias = "folder_" + Guid.NewGuid().ToString("N")[..12];
        var scopes = _bridgeState!["scopes"]!.DeepClone().AsObject();
        var rights = new JsonArray();
        if (read.Checked) rights.Add("read");
        if (write.Checked) rights.Add("write");
        if (execute.Checked) rights.Add("exec");
        scopes[alias] = new JsonObject { ["path"] = picker.SelectedPath,
            ["permissions"] = rights };
        await Action("configure", new JsonObject { ["scopes"] = scopes,
            ["confirmations"] = new JsonObject { ["exec_scope"] = execute.Checked ? "ENABLE UNSANDBOXED EXECUTION" : "" } });
    }

    private async Task RemoveFolder()
    {
        if (_bridgeState is null) { await RefreshStatus(); if (_bridgeState is null) return; }
        var scopes = _bridgeState!["scopes"]!.DeepClone().AsObject();
        using var dialog = new Form { Text = "Remove folder access", ClientSize = new Size(500, 180), StartPosition = FormStartPosition.CenterParent };
        var list = new ListBox { Dock = DockStyle.Fill, DisplayMember = "Value" };
        foreach (var scope in scopes) list.Items.Add(new KeyValuePair<string, string>(scope.Key, scope.Value?["path"]?.GetValue<string>() ?? scope.Key));
        var remove = new Button { Text = "Remove selected", Dock = DockStyle.Bottom, DialogResult = DialogResult.OK };
        dialog.Controls.Add(list); dialog.Controls.Add(remove);
        if (dialog.ShowDialog(this) != DialogResult.OK || list.SelectedItem is not KeyValuePair<string, string> selected) return;
        scopes.Remove(selected.Key);
        await Action("configure", new JsonObject { ["scopes"] = scopes });
    }

    private async Task ConfigurePermissions()
    {
        if (_bridgeState is null) { await RefreshStatus(); if (_bridgeState is null) return; }
        using var dialog = new Form { Text = "Muse desktop permissions", ClientSize = new Size(470, 240), StartPosition = FormStartPosition.CenterParent };
        var panel = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(12), FlowDirection = FlowDirection.TopDown };
        var features = _bridgeState!["features"]!.DeepClone().AsObject();
        var desktop = new CheckBox { Text = "Allow desktop control", Checked = features["desktop_control"]?.GetValue<bool>() == true, AutoSize = true };
        var screenshots = new CheckBox { Text = "Allow screenshots", Checked = features["screenshots"]?.GetValue<bool>() == true, AutoSize = true };
        var execution = new CheckBox { Text = "Allow approved commands", Checked = features["exec"]?.GetValue<bool>() == true, AutoSize = true };
        var apps = new TextBox { Width = 400, Text = string.Join(", ", _bridgeState["desktop_apps"]!.AsArray().Select(x => x!.GetValue<string>())) };
        var save = new Button { Text = "Save", DialogResult = DialogResult.OK, AutoSize = true };
        panel.Controls.AddRange([desktop, screenshots, execution, new Label { Text = "Trusted application names (comma separated)", AutoSize = true }, apps, save]);
        dialog.Controls.Add(panel);
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        var typed = new Dictionary<string, string>();
        foreach (var (key, selected, phrase) in new[] { ("desktop_control", desktop.Checked, "ENABLE DESKTOP CONTROL"),
            ("screenshots", screenshots.Checked, "ENABLE SCREENSHOTS"), ("exec", execution.Checked, "ENABLE UNSANDBOXED EXECUTION") })
        {
            if (selected && features[key]?.GetValue<bool>() != true && !ConfirmPhrase(phrase)) return;
            if (selected) typed[key] = phrase;
            features[key] = selected;
        }
        var appNames = apps.Text.Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries).Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        var oldApps = _bridgeState["desktop_apps"]!.AsArray().Select(x => x!.GetValue<string>()).ToHashSet(StringComparer.OrdinalIgnoreCase);
        if (appNames.Any(name => !oldApps.Contains(name)))
        {
            if (!ConfirmPhrase("TRUST SCREEN CONTENT")) return;
            typed["desktop_apps"] = "TRUST SCREEN CONTENT";
        }
        await Action("configure", new { features, desktop_apps = appNames, confirmations = typed });
    }

    private bool ConfirmPhrase(string phrase)
    {
        using var dialog = new Form { Text = "Confirm Muse access", ClientSize = new Size(440, 120), StartPosition = FormStartPosition.CenterParent };
        var text = new TextBox { Dock = DockStyle.Top };
        var label = new Label { Text = "Type " + phrase + " to confirm:", Dock = DockStyle.Top, Height = 40 };
        var confirm = new Button { Text = "Confirm", DialogResult = DialogResult.OK, Dock = DockStyle.Bottom };
        dialog.Controls.Add(text); dialog.Controls.Add(label); dialog.Controls.Add(confirm);
        return dialog.ShowDialog(this) == DialogResult.OK && text.Text == phrase;
    }

    private async Task ConfigureSession()
    {
        using var dialog = new Form { Text = "Timed Muse access", ClientSize = new Size(420, 150), StartPosition = FormStartPosition.CenterParent };
        var layout = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(12), FlowDirection = FlowDirection.TopDown };
        var profiles = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 250 };
        profiles.Items.AddRange(["Configured permissions", "Read files only", "Read and edit files", "All configured assistance"]);
        profiles.SelectedIndex = 0;
        var minutes = new NumericUpDown { Minimum = 1, Maximum = 480, Value = 60 };
        layout.Controls.AddRange([profiles, new Label { Text = "Minutes", AutoSize = true }, minutes,
            new Button { Text = "Apply", DialogResult = DialogResult.OK, AutoSize = true }]);
        dialog.Controls.Add(layout);
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        string? profile = profiles.SelectedIndex switch { 1 => "files_read", 2 => "files_edit", 3 => "assistant", _ => null };
        await Action("session", new { profile, minutes = decimal.ToInt32(minutes.Value) });
    }
}
