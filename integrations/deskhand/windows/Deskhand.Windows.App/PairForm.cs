using System.Drawing;
using QRCoder;

namespace Deskhand.Windows.App;

public sealed class PairForm : Form
{
    private readonly HostEngine _engine;
    private readonly Label _status = new() { AutoSize = false, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter };
    private readonly System.Windows.Forms.Timer _tick = new() { Interval = 1000 };
    private PairingInfo? _info;

    public PairForm(HostEngine engine, string? phoneRelayUrl = null, string? tailscaleRelayUrl = null)
    {
        _engine = engine;
        Text = "Pair a new phone";
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(420, 560);

        _info = engine.StartPairing(phoneRelayUrl, tailscaleRelayUrl);
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5, Padding = new Padding(12) };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 40));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 320));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 50));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 40));

        layout.Controls.Add(new Label
        {
            Text = "In the Deskhand app on your phone, tap \"Scan QR code\".",
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleCenter,
        }, 0, 0);
        layout.Controls.Add(new PictureBox { Dock = DockStyle.Fill, SizeMode = PictureBoxSizeMode.Zoom, Image = RenderQr(_info.QrPayload) }, 0, 1);
        layout.Controls.Add(new Label
        {
            Text = _info.Serial,
            Dock = DockStyle.Fill,
            Font = new Font("Consolas", 14, FontStyle.Bold),
            TextAlign = ContentAlignment.MiddleCenter,
        }, 0, 2);
        layout.Controls.Add(_status, 0, 3);
        var cancel = new Button { Text = "Cancel", Dock = DockStyle.Fill };
        cancel.Click += (_, _) => Close();
        layout.Controls.Add(cancel, 0, 4);
        Controls.Add(layout);

        _engine.Changed += OnChanged;
        _tick.Tick += (_, _) => Refresh_();
        _tick.Start();
        Refresh_();
        FormClosed += (_, _) =>
        {
            _tick.Stop();
            _engine.Changed -= OnChanged;
            if (_engine.CurrentPairing is not null) _engine.CancelPairing();
        };
    }

    private void OnChanged()
    {
        if (IsHandleCreated && !IsDisposed) BeginInvoke(Refresh_);
    }

    private bool _finished;

    private void Refresh_()
    {
        if (_finished) return;
        if (_engine.CurrentPairing is null)
        {
            _finished = true;
            _tick.Stop();
            if (!string.IsNullOrEmpty(_engine.PairingStatus)) _engine.RaiseNotice(_engine.PairingStatus);
            Close();
            return;
        }
        var left = _info!.ExpiresUtc - DateTime.UtcNow;
        _status.Text = $"{_engine.PairingStatus}\r\nThis code works once and expires in {Math.Max(0, (int)left.TotalMinutes)}:{Math.Max(0, left.Seconds):D2}.";
    }

    private static Image RenderQr(string payload)
    {
        using var generator = new QRCodeGenerator();
        var data = generator.CreateQrCode(payload, QRCodeGenerator.ECCLevel.M);
        var png = new PngByteQRCode(data).GetGraphic(8);
        // B2-A B10: Image.FromStream needs its stream open for the image's
        // lifetime, so the old code leaked one MemoryStream per pairing
        // dialog. Copy into a Bitmap (which owns its own pixels) and let
        // the stream be disposed; the PictureBox disposes the Bitmap.
        using var stream = new MemoryStream(png);
        using var image = Image.FromStream(stream);
        return new Bitmap(image);
    }
}

public sealed class ApproveForm : Form
{
    public PairDecision Decision { get; private set; } = PairDecision.Reject;

    // B2-A B9: true when the dialog closed on its 120 s timer (or the user
    // closed it without choosing) rather than by an explicit decision.
    // Decision still reads Reject in that case (fail safe), but the caller
    // can now tell a timeout apart from a deliberate rejection.
    public bool TimedOut { get; private set; }

    public ApproveForm(PairRequestInfo info)
    {
        Text = "Approve new phone";
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        TopMost = true;
        ShowInTaskbar = true;
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(460, 230);

        Controls.Add(new Label
        {
            Text = $"\"{info.DeviceName}\" wants to pair with this PC.\r\n\r\nConfirm this code matches the one on your phone:\r\n\r\n{info.ConfirmCode}\r\n\r\nOnly approve if you started pairing yourself.",
            Dock = DockStyle.Top,
            Height = 160,
            TextAlign = ContentAlignment.MiddleCenter,
        });

        var buttons = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 60, FlowDirection = FlowDirection.RightToLeft, Padding = new Padding(8) };
        buttons.Controls.Add(MakeButton("Reject", PairDecision.Reject));
        buttons.Controls.Add(MakeButton("View only", PairDecision.ViewOnly));
        buttons.Controls.Add(MakeButton("Approve + control", PairDecision.Control));
        Controls.Add(buttons);

        var timeout = new System.Windows.Forms.Timer { Interval = 120_000 };
        timeout.Tick += (_, _) => { TimedOut = true; Close(); };
        timeout.Start();
        FormClosed += (_, _) => timeout.Dispose();
    }

    private Button MakeButton(string text, PairDecision decision)
    {
        var b = new Button { Text = text, AutoSize = true, Padding = new Padding(6, 2, 6, 2) };
        b.Click += (_, _) =>
        {
            Decision = decision;
            Close();
        };
        return b;
    }
}
