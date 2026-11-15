using Deskhand.Windows.App;

internal static class Program
{
    [System.Runtime.InteropServices.DllImport("winmm.dll")]
    private static extern uint timeBeginPeriod(uint ms);

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct PowerThrottlingState { public uint Version, ControlMask, StateMask; }

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern bool SetProcessInformation(IntPtr process, int infoClass, ref PowerThrottlingState info, int size);

    // Windows 11 lowers timer resolution for hidden or background windows; opt out so streaming stays at full frame rate.
    private static void DisablePowerThrottling()
    {
        var state = new PowerThrottlingState { Version = 1, ControlMask = 0x1 | 0x4, StateMask = 0 };
        SetProcessInformation(System.Diagnostics.Process.GetCurrentProcess().Handle, 4, ref state, System.Runtime.InteropServices.Marshal.SizeOf<PowerThrottlingState>());
    }

    [STAThread]
    private static void Main(string[] args)
    {
        if (args.Contains("--chat-preview"))
        {
            ApplicationConfiguration.Initialize();
            using var previewChat = new AssistantClient(Path.Combine(Environment.GetEnvironmentVariable("DESKHAND_CONFIG_DIR") ?? AppContext.BaseDirectory, "assistant"));
            using var preview = new Form { Text = "Deskhand chat preview", ClientSize = new System.Drawing.Size(900, 620), StartPosition = FormStartPosition.CenterScreen };
            preview.Controls.Add(new ChatPanel(previewChat));
            if (args.Contains("--verify-shutdown") && Environment.GetEnvironmentVariable("DESKHAND_CONFIG_DIR") is not null)
            {
                preview.Load += async (_, _) =>
                {
                    try { await previewChat.CallAsync(new { op = "history" }); }
                    catch (Exception) { Environment.ExitCode = 1; }
                    finally { preview.Close(); }
                };
            }
            Application.Run(preview);
            return;
        }
        // The default 15.6 ms timer tick rounds frame pacing delays up and caps the stream near 21 fps.
        timeBeginPeriod(1);
        DisablePowerThrottling();
        var isolatedConfig = Environment.GetEnvironmentVariable("DESKHAND_CONFIG_DIR");
        var mutexName = "Deskhand.Windows.App.SingleInstance";
        if (isolatedConfig is not null)
            mutexName += "." + Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(Path.GetFullPath(isolatedConfig).ToUpperInvariant())))[..16];
        using var mutex = new Mutex(true, mutexName, out var first);
        var pairFromJetstream = isolatedConfig is not null && args.Contains("--pair-phone");
        using var pairSignal = isolatedConfig is null ? null : new EventWaitHandle(false, EventResetMode.AutoReset, mutexName + ".PairPhone");
        if (!first)
        {
            if (pairFromJetstream) { pairSignal!.Set(); return; }
            MessageBox.Show("Deskhand is already running. Look for its icon in the system tray.", "Deskhand");
            return;
        }

        ApplicationConfiguration.Initialize();
        var config = AppConfig.Load();
        var ui = SynchronizationContext.Current ?? new WindowsFormsSynchronizationContext();
        if (Environment.GetEnvironmentVariable("DESKHAND_CONFIG_DIR") is null) MainForm.SetStartup(config.StartWithWindows);
        var relay = new BuiltInRelay();
        if (config.RunBuiltInRelay)
        {
            relay.StartAsync(config.RelayPort).GetAwaiter().GetResult();
            if (relay.Error is not null)
            {
                // One retry: a previous instance's socket can still hold
                // the port briefly after shutdown.
                Task.Delay(1000).GetAwaiter().GetResult();
                relay.StartAsync(config.RelayPort).GetAwaiter().GetResult();
            }
            if (relay.Error is not null)
            {
                // Fail loudly at startup, like the already-running check
                // above: never continue with a dead relay and hand out a
                // pairing QR for it.
                MessageBox.Show($"Deskhand could not start its built-in relay, so the app will close.\n\n{relay.Error}\n\nFree port {config.RelayPort} (another program may be using it) or change the relay port in %LOCALAPPDATA%\\Deskhand\\config.json, then start Deskhand again.", "Deskhand");
                return;
            }
        }

        // Isolated verification can preserve the user's phone pairing while
        // restricting file actions to a disposable directory. Never honor this
        // override for the installed host's normal data directory.
        var fixtureRoot = Environment.GetEnvironmentVariable("DESKHAND_TEST_DESKTOP_ROOT");
        if (fixtureRoot is not null && isolatedConfig is null)
            throw new InvalidOperationException("A test desktop requires an isolated configuration.");
        using var engine = new HostEngine(config, desktopFactory: fixtureRoot is null ? null : () => new DesktopBrowser(fixtureRoot));
        using var chat = new AssistantClient(Environment.GetEnvironmentVariable("DESKHAND_ASSISTANT_DATA") ?? Path.Combine(Environment.GetEnvironmentVariable("DESKHAND_CONFIG_DIR") ??
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Deskhand"), "assistant"));
        engine.Chat = chat;
        engine.InitializeFullPcAccess();
        chat.HistoryChanged += engine.PublishChatHistory;
        // B2-A B14/B9: the approval dialog is owned by the main form once it
        // exists, so it cannot hide behind other windows; a timeout is still
        // a Reject (fail safe) but is now told apart via a tray notice.
        MainForm? mainForm = null;
        engine.ApprovalHandler = info =>
        {
            var tcs = new TaskCompletionSource<PairDecision>();
            ui.Post(_ =>
            {
                using var form = new ApproveForm(info);
                if (mainForm is not null) form.ShowDialog(mainForm);
                else form.ShowDialog();
                if (form.TimedOut) engine.RaiseNotice("Phone approval timed out — no decision was made. Ask the phone to pair again.");
                tcs.TrySetResult(form.Decision);
            }, null);
            return tcs.Task;
        };
        engine.Start();

        var main = new MainForm(engine, config, relay, chat) { StartHidden = args.Contains("--minimized") };
        mainForm = main;
        var integrated = args.Contains("--jetstream");
        using var desktopBridge = integrated ? new JetstreamDesktopBridge(mutexName + ".Desktop", ui, main, engine, config) : null;
        main.StartEmbeddedOnLaunch();
        // B2-A B5: RestoreFullPcAccess was gated on Shown, which never fires
        // on the normal --minimized autostart path. Restore explicitly when
        // starting hidden, exactly like StartEmbeddedOnLaunch above; the
        // Shown handler stays for the visible path (and for a window first
        // shown later from the tray, which does raise Shown).
        if (main.StartHidden) _ = engine.RestoreFullPcAccess();
        main.Shown += async (_, _) => await engine.RestoreFullPcAccess();
        using var pairTimer = new System.Windows.Forms.Timer { Interval = 250 };
        pairTimer.Tick += (_, _) => { if (pairSignal?.WaitOne(0) == true) main.PrepareJetstreamPairing(); };
        if (pairFromJetstream && !integrated) pairSignal!.Set();
        if (pairSignal is not null) pairTimer.Start();
        Application.Run(new ApplicationContext(main));
        // Drain Muse and remove its HTTPS endpoint before tearing down the
        // host authority; otherwise normal exit looks like emergency STOP.
        chat.Dispose();
        engine.Dispose();
        Task.WhenAny(relay.StopAsync(), Task.Delay(2000)).GetAwaiter().GetResult();
        // Background loops and open sockets must not keep the process alive after the user closes the app.
        Environment.Exit(0);
    }
}
