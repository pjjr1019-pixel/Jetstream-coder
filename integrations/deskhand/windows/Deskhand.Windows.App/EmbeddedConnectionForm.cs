using System.Diagnostics;

namespace Deskhand.Windows.App;

internal sealed class EmbeddedConnectionForm : Form
{
    private readonly EmbeddedWindowsNode _node;
    private readonly HostEngine _engine;
    private readonly int _port;
    private readonly Func<Task> _ensureListening;
    private readonly Label _status = new() { AutoSize = true, MaximumSize = new Size(450, 0), Text = "Checking connection…" };
    private readonly Button _login = new() { AutoSize = true, Text = "Sign in", Enabled = false };
    private readonly Button _pair = new() { AutoSize = true, Text = "Pair phone", Enabled = false };
    private readonly System.Windows.Forms.Timer _timer = new() { Interval = 3000 };
    private string? _loginUrl, _address;
    private bool _busy;
    private static void TraceTest(string message)
    { if (Environment.GetEnvironmentVariable("DESKHAND_EMBEDDED_TEST") == "1") Console.Error.WriteLine("Embedded test: " + message); }

    internal EmbeddedConnectionForm(EmbeddedWindowsNode node, HostEngine engine, int port, Func<Task> ensureListening)
    {
        _node = node; _engine = engine; _port = port; _ensureListening = ensureListening;
        Text = "Embedded connection"; ClientSize = new Size(500, 165); StartPosition = FormStartPosition.CenterParent;
        var layout = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown, Padding = new Padding(12), WrapContents = false };
        layout.Controls.Add(_status); layout.Controls.Add(_login); layout.Controls.Add(_pair); Controls.Add(layout);
        _login.Click += async (_, _) => await SignIn();
        // B2-A B3: the old handler silently did nothing when _address was
        // null. The button is normally disabled then, but the handler is
        // the last line of defence and must say why pairing did nothing.
        _pair.Click += (_, _) => { if (_address is not null) { using var pair = new PairForm(_engine, _address); pair.ShowDialog(this); } else { _status.Text = "Connect this PC first — the embedded connection has no address yet."; } };
        _timer.Tick += async (_, _) => await RefreshStatus();
        Shown += async (_, _) => { _timer.Start(); await RefreshStatus(); };
        FormClosed += (_, _) => _timer.Dispose();
    }

    private async Task SignIn()
    {
        try
        {
            _login.Enabled = false;
            TraceTest("login clicked");
            // B2-A B4: the post-login refresh used to be skipped whenever
            // the 3 s timer poll was in flight (_busy), so clicking Sign
            // in could end with no browser and no message. Force the
            // refresh past the in-flight poll; RefreshStatus always sets
            // the status label, so the outcome is visible either way.
            if (_loginUrl is null) { await _node.LoginAsync(); await RefreshStatus(force: true); }
            if (_loginUrl is not null) Process.Start(new ProcessStartInfo(_loginUrl) { UseShellExecute = true });
            TraceTest("login handler finished");
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception)
        { _status.Text = ex.Message; }
    }

    private async Task RefreshStatus(bool force = false)
    {
        // B2-A B4: a forced refresh (post-login) runs even while a timer
        // poll is in flight; the node serialises native calls, and the
        // last writer wins on the status label.
        if ((_busy && !force) || IsDisposed) return;
        _busy = true;
        try
        {
            var state = await _node.StatusAsync();
            TraceTest("state=" + state.State + " address=" + !string.IsNullOrEmpty(state.IPv4));
            if (IsDisposed) return;
            _loginUrl = string.IsNullOrEmpty(state.AuthURL) ? null : state.AuthURL;
            if (state.State == "Running" && !string.IsNullOrEmpty(state.IPv4))
            {
                TraceTest("starting listener");
                await _ensureListening();
                TraceTest("listener ready");
                _address = TailscaleAddress.RelayUrl(state.IPv4, _port);
                _status.Text = "Connected. Pair your phone to use the embedded connection.";
                _pair.Enabled = true; _login.Enabled = false;
            }
            else
            {
                _address = null; _pair.Enabled = false;
                _status.Text = state.State == "NeedsLogin" ? "Sign in to connect this PC." : "Connection status: " + state.State;
                _login.Enabled = state.State == "NeedsLogin" || _loginUrl is not null;
            }
        }
        // B2-A (area-05 B1, adjacent): StatusAsync can also throw
        // JsonException on a malformed native payload; in this async void
        // timer handler that used to be an unhandled UI-thread crash.
        catch (Exception ex) when (ex is InvalidOperationException or IOException or ObjectDisposedException or System.Text.Json.JsonException)
        { if (!IsDisposed) { _status.Text = ex.Message; _pair.Enabled = false; } }
        finally { _busy = false; }
    }
}
