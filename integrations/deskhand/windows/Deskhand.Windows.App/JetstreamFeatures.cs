using System.Text.Json;

namespace Deskhand.Windows.App;

// Shares the standalone form's live models, renderers and service. No second
// runtime feed, credential store or parallel implementation of phone behavior.
public sealed partial class MainForm
{
    internal object JetstreamFeatures() => new {
        settings = new { deviceName = _config.DeviceName, relayUrl = _config.RelayUrl, relayPort = _config.RelayPort, builtInRelay = _config.RunBuiltInRelay },
        runtime = new {
            connection = _status.Text, relay = _relayStatus.Text, worker = _workerStatus.Text,
            activity = _activityTask.Text, age = _activityAge.Text, files = _activityFiles.Items.Cast<object>().Select(x => x.ToString()).ToArray(),
            eventsHealth = _eventsHealth.Text, events = _eventsList.Items.Cast<object>().Select(x => x.ToString()).ToArray(),
            boardHealth = BoardFooterText(), board = _boardPosts.Select(p => new { id = p.Id, author = p.Author, kind = p.Kind, text = p.Text, detail = p.Detail, time = p.Ts }).ToArray(),
            authority = _authorityStatus.Text,
            diagnostics = DiagnosticsKeys.Select((key, index) => new { key, name = DiagnosticsLabels[index],
                status = _diagnosticsRows.TryGetValue(key, out var row) ? row.Status : "UNKNOWN",
                lastOk = row?.LastOkEpoch ?? 0, version = row?.Version ?? "", error = row?.Error ?? "", level = row?.Level ?? "INFO" }).ToArray(),
            turns = _timingTurns.Items.Cast<object>().Select(x => x.ToString()).ToArray()
        }
    };

    internal async Task<object> JetstreamSettings(JsonElement request)
    {
        var name = request.GetProperty("deviceName").GetString()?.Trim() ?? "";
        var url = request.GetProperty("relayUrl").GetString()?.Trim().TrimEnd('/') ?? "";
        var port = request.GetProperty("relayPort").GetInt32();
        var builtIn = request.GetProperty("builtInRelay").GetBoolean();
        if (name.Length > 200) throw new InvalidOperationException("PC name is too long.");
        if (port is < 1024 or > 65535) throw new InvalidOperationException("Relay port must be 1024–65535.");
        if (_embeddedNode is not null && port != _config.RelayPort) throw new InvalidOperationException("Restart the embedded host before changing its active relay port.");
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || uri.Scheme is not ("ws" or "wss") || uri.UserInfo.Length != 0 || AppConfig.IsTunnelUrl(url))
            throw new InvalidOperationException("Use a ws:// or wss:// Tailscale or LAN relay address.");
        var previous = (_config.DeviceName, _config.RelayUrl, _config.RelayPort, _config.RunBuiltInRelay);
        _config.DeviceName = string.IsNullOrEmpty(name) ? Environment.MachineName : name;
        _config.RelayUrl = url; _config.RelayPort = port; _config.RunBuiltInRelay = builtIn;
        try { _config.Save(); }
        catch { (_config.DeviceName, _config.RelayUrl, _config.RelayPort, _config.RunBuiltInRelay) = previous; throw; }
        _nameBox.Text = _config.DeviceName; _relayBox.Text = url; _portBox.Value = port; _builtInBox.Checked = builtIn;
        if (builtIn) { await _relay.StartAsync(port); if (_relay.Error is not null) throw new IOException(_relay.Error); }
        else await _relay.StopAsync();
        if (previous.Item2 != url || previous.Item3 != port || previous.Item4 != builtIn) _engine.Restart();
        RefreshView(); return JetstreamFeatures();
    }

    internal async Task<object> JetstreamAssistant(JsonElement command)
    {
        if (_chat is null) throw new InvalidOperationException("Deskhand assistant service is unavailable.");
        var op = command.GetProperty("op").GetString();
        // Expose only the operations used by the original local UI. File and
        // execution RPCs are not a renderer escape hatch around Coder's gate.
        if (op is not ("catalog" or "history" or "health" or "activity" or "timing" or "diagnostics_export" or
            "route_suggest" or "router_settings" or "submit" or "stop_chat" or "muse_status" or "muse_signin" or
            "muse_signin_done" or "bridge_start" or "worker_reconnect" or "local_configure" or "local_runtime_start" or
            "access_status" or "access_set" or "muse_action")) throw new InvalidOperationException("Unsupported Deskhand UI operation.");
        if (op == "muse_action" && command.GetProperty("action").GetString() is not ("configure" or "stop" or "session" or "connection_details"))
            throw new InvalidOperationException("Unsupported Muse connection action.");
        return await _chat.CallAsync(command);
    }
}
