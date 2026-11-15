using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using Deskhand.Core;
using Deskhand.Contracts;
using Deskhand.Windows.App;

// End-to-end test: built-in relay + host engine + a simulated phone, all in-process.
// It moves the mouse cursor briefly during the input check and restores it afterwards.
var configDir = Path.Combine(Path.GetTempPath(), "deskhand-it-" + Guid.NewGuid().ToString("N"));
Environment.SetEnvironmentVariable("DESKHAND_CONFIG_DIR", configDir);
var Port = int.TryParse(Environment.GetEnvironmentVariable("DESKHAND_TEST_PORT"), out var testPort) ? testPort : 47911;
var Relay = $"ws://127.0.0.1:{Port}";
var jettCapability = RandomBytes(32);

var failures = 0;
void Check(string name, bool ok)
{
    Console.WriteLine($"{(ok ? "PASS" : "FAIL")} {name}");
    if (!ok) failures++;
}

if (args.Contains("--jetstream-settings-check"))
{
    Exception? failure = null;
    var thread = new Thread(() => {
        try {
            var cfg = new AppConfig { DeviceName = "Fixture PC", RelayUrl = Relay, RelayPort = Port, StartWithWindows = false, PersistentDesktopAccess = false };
            using var engine = new HostEngine(cfg, jettCapability);
            using var chat = new AssistantClient(Path.Combine(configDir, "assistant"), fixture: true);
            using var form = new MainForm(engine, cfg, new BuiltInRelay(), chat);
            var state = JsonSerializer.SerializeToElement(form.JetstreamFeatures());
            Check("embedded settings reuse standalone PC name", state.GetProperty("settings").GetProperty("deviceName").GetString() == "Fixture PC");
            Check("all standalone diagnostics components are present", state.GetProperty("runtime").GetProperty("diagnostics").GetArrayLength() == 12);
            Check("paired device secrets are excluded", !state.GetRawText().Contains("ProtectedSecret"));
            var hostOnly = JsonSerializer.SerializeToElement(new { cloud_ready = true, delivery_ready = true,
                https_connection = new { ready = true }, bridge = new { enabled = true, connected = false,
                    https_session_available = true, https_actions_available = false, shared_actions_host_only = true } });
            Check("host-only action routing does not require duplicate HTTPS actions", MuseReadiness.Inspect(hostOnly).Ready);
            var signedOut = JsonDocument.Parse(hostOnly.GetRawText().Replace("\"cloud_ready\":true", "\"cloud_ready\":false"));
            Check("host-only routing still requires verified cloud sign-in", !MuseReadiness.Inspect(signedOut.RootElement).Ready);
            var endpointDown = JsonDocument.Parse(hostOnly.GetRawText().Replace("\"ready\":true", "\"ready\":false"));
            Check("host-only routing still requires a live endpoint", !MuseReadiness.Inspect(endpointDown.RootElement).Ready);
            foreach (var op in new[] { "fs.write", "exec.run", "jetstream_action" }) {
                using var command = JsonDocument.Parse(JsonSerializer.Serialize(new { op }));
                try { form.JetstreamAssistant(command.RootElement).GetAwaiter().GetResult(); Check("reject direct operation " + op, false); }
                catch (InvalidOperationException) { Check("reject direct operation " + op, true); }
            }
            using var invalidMuse = JsonDocument.Parse("""{"op":"muse_action","action":"execute","payload":{}}""");
            try { form.JetstreamAssistant(invalidMuse.RootElement).GetAwaiter().GetResult(); Check("reject non-UI Muse action", false); }
            catch (InvalidOperationException) { Check("reject non-UI Muse action", true); }
            foreach (var invalid in new[] {
                new {deviceName="Changed",relayUrl="https://example.com",relayPort=Port,builtInRelay=true},
                new {deviceName="Changed",relayUrl="ws://example.trycloudflare.com",relayPort=Port,builtInRelay=true},
                new {deviceName="Changed",relayUrl=Relay,relayPort=0,builtInRelay=true}
            }) {
                var command = JsonSerializer.SerializeToElement(invalid);
                try { form.JetstreamSettings(command).GetAwaiter().GetResult(); Check("reject invalid connection settings", false); }
                catch (InvalidOperationException) { Check("reject invalid connection settings", cfg.DeviceName == "Fixture PC" && cfg.RelayUrl == Relay && cfg.RelayPort == Port); }
            }
        } catch (Exception ex) { failure = ex; }
    });
    thread.SetApartmentState(ApartmentState.STA); thread.Start(); thread.Join();
    if (failure is not null) throw failure;
    return failures;
}







if (args.Contains("--persistent-grant-check"))
{
    var initial = new AppConfig { PersistentDesktopAccess = true };
    initial.Save();
    using (var first = new HostEngine(initial, jettCapability))
    {
        first.GrantPersistentDesktopLocally();
        Check("persistent grant remains active until revoked", first.DesktopGrantExpiresUtc == DateTimeOffset.MaxValue);
    }
    var reopenedConfig = AppConfig.Load();
    using (var reopened = new HostEngine(reopenedConfig, jettCapability))
    {
        if (reopenedConfig.PersistentDesktopAccess) reopened.GrantPersistentDesktopLocally();
        Check("persistent grant survives restart", reopened.DesktopGrantExpiresUtc == DateTimeOffset.MaxValue);
    }
    using (var restored = new HostEngine(AppConfig.Load(), jettCapability))
    {
        restored.GrantPersistentDesktopLocally();
        Check("persistent grant is active after restore", restored.DesktopGrantExpiresUtc == DateTimeOffset.MaxValue);
        restored.RevokeJettDesktopLocally();
        Check("revocation persists", !AppConfig.Load().PersistentDesktopAccess && restored.DesktopGrantExpiresUtc is null);
    }
    return failures;
}

var relay = new BuiltInRelay();
await relay.StartAsync(Port);
Check("built-in relay starts", relay.Running);
// Relay-startup failure mode (spec gap B7): a second relay on the same
// port must fail loudly (Running false + Error set), not silently
// share the port or kill the first relay.
var secondRelay = new BuiltInRelay();
await secondRelay.StartAsync(Port);
Check("second relay on the same port fails loudly instead of silently sharing it", !secondRelay.Running && secondRelay.Error is not null);
await secondRelay.StopAsync();
Check("failed second relay leaves the first relay running", relay.Running);
if (args.Contains("--relay-callback-recovery"))
{
    var room = Guid.NewGuid().ToString("N");
    using var host = new RelayLink(Relay, room);
    using var client = new RelayLink(Relay, room, "client");
    var connectedCount = 0;
    var reconnected = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
    host.ConnectionChanged += () => { if (host.Connected && Interlocked.Increment(ref connectedCount) >= 2) reconnected.TrySetResult(); };
    host.PeerJoined += () => throw new InvalidOperationException("Injected receive callback failure");
    host.Start();
    var deadline = DateTime.UtcNow.AddSeconds(5);
    while (!host.Connected && DateTime.UtcNow < deadline) await Task.Delay(20);
    client.Start();
    Check("relay reconnects after receive callback failure", await Task.WhenAny(reconnected.Task, Task.Delay(10000)) == reconnected.Task);
    await relay.StopAsync();
    return failures;
}
if (args.Contains("--relay-stop-check"))
{
    using var socket = new System.Net.WebSockets.ClientWebSocket();
    await socket.ConnectAsync(new Uri($"{Relay}/ws/{new string('a', 32)}?role=host"), CancellationToken.None);
    var stop = relay.StopAsync();
    var completed = await Task.WhenAny(stop, Task.Delay(TimeSpan.FromSeconds(5))) == stop;
    Check("relay stops with a connected websocket", completed);
    if (!completed) { socket.Abort(); await stop.WaitAsync(TimeSpan.FromSeconds(10)); }
    return failures;
}

var originalDisplay = System.Windows.Forms.Screen.PrimaryScreen!.Bounds;
var config = new AppConfig { RelayPort = Port, RunBuiltInRelay = true, RelayUrl = Relay, DeviceName = "IT-PC", PhoneFriendlyDisplay = args.Contains("--viewer-display") || args.Contains("--viewer-buttons") };
var desktopRoot = Path.Combine(configDir, "DesktopFixture");
Directory.CreateDirectory(desktopRoot);
File.WriteAllText(Path.Combine(desktopRoot, "fixture.txt"), "integration fixture");
var desktopOpener = new TestDesktopOpener();
var fixture = args.Contains("--adaptive-fixture") || args.Contains("--hybrid-video") ? new SyntheticCapture() : null;
var updatesDir = Path.Combine(configDir, "updates");
using var engine = new HostEngine(config, jettCapability, () => new DesktopBrowser(desktopRoot, desktopOpener), fixture is null ? null : () => fixture, androidUpdateDirectory: updatesDir);
using var chat = new AssistantClient(Path.Combine(configDir, "assistant"), !args.Contains("--live-chat"),
    args.Contains("--real-phone-model") ? TimeSpan.FromMinutes(3) : TimeSpan.FromSeconds(45));
engine.Chat = chat;
chat.HistoryChanged += engine.PublishChatHistory;
engine.Notice += message => Console.WriteLine($"HOST: {message}");
engine.ApprovalHandler = _ => Task.FromResult(args.Contains("--stop-removed-only") ? PairDecision.ViewOnly : PairDecision.Control);
var touchSamples = new List<(bool Portrait, bool Mapped)>();
if (args.Contains("--viewer-precise"))
    engine.InputMoveObservedForFixture = (x, y) =>
    {
        if (!GetCursorPos(out var actual)) return;
        var bounds = System.Windows.Forms.Screen.PrimaryScreen!.Bounds;
        var expectedX = bounds.Left + x * (bounds.Width - 1);
        var expectedY = bounds.Top + y * (bounds.Height - 1);
        lock (touchSamples) touchSamples.Add((bounds.Height > bounds.Width, Math.Abs(actual.X - expectedX) <= 6 && Math.Abs(actual.Y - expectedY) <= 6));
    };
engine.Start();

// ---- Pairing ----
var pairing = engine.StartPairing();
// Pairing failure mode (spec gap B7): starting a second pairing while
// one is in progress replaces it instead of running two pairings.
var firstPairing = pairing;
pairing = engine.StartPairing();
Check("starting a second pairing replaces the first instead of running two", engine.CurrentPairing is not null && engine.CurrentPairing.Serial == pairing.Serial && pairing.Serial != firstPairing.Serial);
if (args.Contains("--real-phone"))
{
    var pairFile = Path.Combine(Directory.GetCurrentDirectory(), "real-phone-verification-pairing.txt");
    using (var stream = new FileStream(pairFile, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
    using (var writer = new StreamWriter(stream)) writer.Write(pairing.QrPayload);
    Console.WriteLine("REAL PHONE: Pairing details prepared for the separate verification app.");
    try
    {
        var deadline = DateTime.UtcNow.AddMinutes(3);
        while (DateTime.UtcNow < deadline && config.Devices.Count == 0) await Task.Delay(250);
        Check("actual phone pairs with isolated host", config.Devices.Count == 1);
        if (config.Devices.Count == 0) return 1;
        try { File.Delete(pairFile); } catch (IOException) { }
        Console.WriteLine("REAL PHONE: Open the paired PC in the verification app.");
        deadline = DateTime.UtcNow.AddMinutes(3);
        while (DateTime.UtcNow < deadline && engine.ActiveDevices.Count == 0) await Task.Delay(250);
        Check("actual phone starts a viewer session", engine.ActiveDevices.Count > 0);
        if (engine.ActiveDevices.Count == 0) return 1;
        if (args.Contains("--viewer-buttons"))
        {
            using var ready = new ManualResetEventSlim();
            System.Windows.Forms.Form? testWindow = null;
            IntPtr testHandle = IntPtr.Zero;
            var thread = new Thread(() =>
            {
                testWindow = new System.Windows.Forms.Form { Text = "Deskhand window controls test", Width = 500, Height = 300 };
                testWindow.Shown += (_, _) => { testHandle = testWindow.Handle; ready.Set(); };
                System.Windows.Forms.Application.Run(testWindow);
            }) { IsBackground = true };
            thread.SetApartmentState(ApartmentState.STA); thread.Start();
            if (!ready.Wait(TimeSpan.FromSeconds(5))) { Check("real window-button fixture starts", false); return 1; }
            desktopOpener.RealForegroundFixture = testHandle;
            try
            {
                TextWindowActivator.Activate(testHandle, false);
                Console.WriteLine("REAL PHONE: Tap minimize in the viewer for the Deskhand window-controls test window.");
                deadline = DateTime.UtcNow.AddSeconds(50);
                while (DateTime.UtcNow < deadline && !IsIconic(testHandle)) await Task.Delay(200);
                var minimized = IsIconic(testHandle);
                Check("actual phone minimizes the real fixture window", minimized);
                if (minimized)
                {
                    TextWindowActivator.Activate(testHandle, false);
                    Console.WriteLine("REAL PHONE: Tap X in the viewer for the restored test window.");
                    deadline = DateTime.UtcNow.AddSeconds(50);
                    while (DateTime.UtcNow < deadline && thread.IsAlive) await Task.Delay(200);
                    Check("actual phone closes the real fixture window", !thread.IsAlive);
                }
                return failures;
            }
            finally
            {
                desktopOpener.RealForegroundFixture = IntPtr.Zero;
                if (testWindow is { IsDisposed: false, IsHandleCreated: true }) testWindow.BeginInvoke((Action)(() => testWindow.Close()));
                thread.Join(2000);
            }
        }
        if (args.Contains("--viewer-display"))
        {
            Console.WriteLine("REAL PHONE: Rotate to portrait, touch the stream, then rotate to landscape and touch the stream.");
            var sawPortrait = false; var sawLandscape = false;
            deadline = DateTime.UtcNow.AddSeconds(75);
            while (DateTime.UtcNow < deadline)
            {
                var bounds = System.Windows.Forms.Screen.PrimaryScreen!.Bounds;
                sawPortrait |= bounds.Height > bounds.Width;
                sawLandscape |= sawPortrait && bounds.Width > bounds.Height;
                bool gotSamples; lock (touchSamples) gotSamples = touchSamples.Any(s => s.Portrait && s.Mapped) && touchSamples.Any(s => !s.Portrait && s.Mapped);
                if (sawPortrait && sawLandscape && (!args.Contains("--viewer-precise") || gotSamples)) break;
                await Task.Delay(500);
            }
            Check("actual phone switches laptop portrait and landscape", sawPortrait && sawLandscape);
            if (args.Contains("--viewer-precise"))
            {
                lock (touchSamples)
                {
                    Check("portrait phone input maps to the reported desktop position", touchSamples.Any(s => s.Portrait && s.Mapped));
                    Check("landscape phone input maps to the reported desktop position", touchSamples.Any(s => !s.Portrait && s.Mapped));
                    Check("all observed phone moves map within tolerance", touchSamples.Count > 0 && touchSamples.All(s => s.Mapped));
                }
            }
            engine.SetPhoneFriendlyDisplay(false);
            await Task.Delay(2000);
            var restored = System.Windows.Forms.Screen.PrimaryScreen!.Bounds;
            Check("laptop display returns to its original size", restored.Width == originalDisplay.Width && restored.Height == originalDisplay.Height);
            return failures;
        }
        if (args.Contains("--viewer-input"))
        {
            GetCursorPos(out var startCursor);
            Console.WriteLine("REAL PHONE: Rotate the phone, then touch and move on the streamed desktop in the verification app.");
            var moved = false; deadline = DateTime.UtcNow.AddSeconds(45);
            while (DateTime.UtcNow < deadline && !moved)
            {
                await Task.Delay(250); GetCursorPos(out var position);
                moved = Math.Abs(position.X - startCursor.X) > 8 || Math.Abs(position.Y - startCursor.Y) > 8;
            }
            Check("actual phone viewer input moves the cursor", moved);
        }
        engine.GrantJettDesktopLocally();
        if (args.Contains("--real-phone-model"))
        {
            var modelTask = Guid.NewGuid(); var modelTurn = Guid.NewGuid();
            Check("real-model fixture starts under local grant", engine.BeginAssistantTask(modelTask, modelTurn, "local", "fixture", "", ""));
            var modelResult = await chat.CallAsync(new { op = "probe", task_id = modelTask.ToString("D"), turn_id = modelTurn.ToString("D") });
            var modelText = modelResult.GetProperty("text").GetString() ?? "";
            Check("local model reads disposable file through Windows host", modelResult.GetProperty("live").GetBoolean() && modelText.Contains("integration fixture", StringComparison.OrdinalIgnoreCase));
            engine.AssistantActionsForFixture.End(modelTask, modelTurn, "completed");
        }
        var task = Guid.NewGuid(); var turn = Guid.NewGuid();
        Check("isolated host grants a local AI task", engine.BeginAssistantTask(task, turn, "local", "fixture", "", ""));
        var list = await engine.ExecuteAssistantAction(task, turn, Guid.NewGuid(), "desktop.list", "{\"folder_token\":null}");
        var item = (list.Data as DesktopListResponse)?.Items.SingleOrDefault(x => x.Name == "fixture.txt");
        if (item is null) { Check("real phone fixture is listed", false); return 1; }
        var pending = engine.ExecuteAssistantAction(task, turn, Guid.NewGuid(), "filesystem.launch", JsonSerializer.Serialize(new { item_token = item.Token, purpose = "Verify approval on the phone; opens only a test fixture" }));
        Console.WriteLine("REAL PHONE: Approve the test action in the verification app.");
        var completed = await Task.WhenAny(pending, Task.Delay(TimeSpan.FromMinutes(2)));
        if (completed != pending) { Check("actual phone approves fixture action", false); return 1; }
        Check("actual phone approves fixture action", (await pending).Status == "uncertain" && desktopOpener.Count == 1);
        Console.WriteLine("REAL PHONE: Press STOP in the verification app (the host acknowledges it as not enabled; nothing is stopped).");
        await Task.Delay(TimeSpan.FromSeconds(30));
        Check("phone stop request is a no-op for AI authority", (await engine.ExecuteAssistantAction(task, turn, Guid.NewGuid(), "desktop.list", "{\"folder_token\":null}")).Status != "rejected");
        return failures;
    }
    finally
    {
        try { File.Delete(pairFile); } catch (IOException) { }
        engine.Dispose();
        await relay.StopAsync();
    }
}
await Task.Delay(500);
var serial = Serial.Normalize(pairing.Serial)!;
var qr = new Uri(pairing.QrPayload);
Check("QR payload carries serial and relay", qr.Scheme == "deskhand" && pairing.QrPayload.Contains(serial) && Uri.UnescapeDataString(pairing.QrPayload).Contains(Relay));

var pairClient = new TestPhone(Relay, Serial.ToSecret(serial));
pairClient.Connect();
var confirm = await pairClient.WaitSession(TimeSpan.FromSeconds(10));
Check("pairing handshake completes", confirm is not null);
if (confirm is null)
{
    pairClient.Dispose();
    await relay.StopAsync();
    Console.WriteLine("Cannot continue without pairing. Check local networking; temporary config: " + configDir);
    return failures;
}
pairClient.Send(Msg.UpdateCheck, JsonSerializer.SerializeToUtf8Bytes(new { requestId = Guid.NewGuid().ToString() }));
Check("pairing session cannot access APK updates", await pairClient.WaitMessage(Msg.UpdateInfo, TimeSpan.FromSeconds(1)) is null);
pairClient.Send(Msg.PairRequest, JsonSerializer.SerializeToUtf8Bytes(new { name = "Test Phone" }));
var accept = await pairClient.WaitMessage(Msg.PairAccept, TimeSpan.FromSeconds(10));
Check("host approves and sends device secret", accept is not null);
var deviceSecret = Convert.FromBase64String(JsonDocument.Parse(accept!).RootElement.GetProperty("secret").GetString()!);
Check("device retains the approved permission", config.Devices.Count == 1
    && config.Devices[0].Control != args.Contains("--stop-removed-only") && config.Devices[0].Name == "Test Phone");
Check("device secret is DPAPI-protected on disk", !File.ReadAllText(Path.Combine(configDir, "config.json")).Contains(Convert.ToBase64String(deviceSecret)));
pairClient.Dispose();
await Task.Delay(2500);
Check("pairing ended and serial is burned", engine.CurrentPairing is null);

var reuse = new TestPhone(Relay, Serial.ToSecret(serial));
reuse.Connect();
Check("old serial cannot be reused", await reuse.WaitSession(TimeSpan.FromSeconds(3)) is null);
reuse.Dispose();

// ---- Wrong secret ----
var intruder = new TestPhone(Relay, RandomBytes(32), Kdf.RoomId(deviceSecret));
intruder.Connect();
Check("wrong secret cannot establish a session", await intruder.WaitSession(TimeSpan.FromSeconds(3)) is null);
intruder.Dispose();

// ---- Reconnect, stream, control ----
var phone = new TestPhone(Relay, deviceSecret);
phone.Connect();
Check("paired phone reconnects with stored secret", await phone.WaitSession(TimeSpan.FromSeconds(10)) is not null);
if (!phone.Ready)
{
    phone.Dispose();
    await relay.StopAsync();
    Console.WriteLine("Cannot continue without a paired session; check for another test using this relay port.");
    return failures;
}
if (args.Contains("--phone-chat-control-only"))
{
    async Task<JsonElement> CallPhone(string op)
    {
        var rpc = Guid.NewGuid().ToString("D");
        phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc, op }));
        return await WaitChat(phone, rpc);
    }
    var stopped = await CallPhone("stop_chat");
    Check("paired phone can request chat stop", stopped.GetProperty("ok").GetBoolean());
    var phoneResume = await CallPhone("resume_chat");
    Check("paired phone receives authoritative resumed epoch", phoneResume.GetProperty("ok").GetBoolean()
        && phoneResume.GetProperty("result").GetProperty("admission_epoch").GetInt64() >= 0
        && !phoneResume.GetProperty("result").GetProperty("stopped").GetBoolean());
    var restarted = await CallPhone("restart_chat");
    Check("paired phone can request confirmed chat restart", restarted.GetProperty("ok").GetBoolean());
    var forbidden = await CallPhone("local_configure");
    Check("unlisted phone chat operation remains rejected", !forbidden.GetProperty("ok").GetBoolean());
    foreach (var op in new[] { "stop_chat", "restart_chat", "resume_chat" })
    {
        if (op == "resume_chat") await chat.CallAsync(new { op = "stop_chat" });
        var controlHistoryBefore = await chat.CallAsync(new { op = "history" });
        var beforeEpoch = controlHistoryBefore.GetProperty("chatAdmission").GetProperty("epoch").GetInt64();
        var beforeStopped = controlHistoryBefore.GetProperty("chatAdmission").GetProperty("stopped").GetBoolean();
        var writeGate = (SemaphoreSlim)typeof(AssistantClient).GetField("_write", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!.GetValue(chat)!;
        var waiting = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        chat.PhoneControlWaitingForFixture = () => waiting.TrySetResult();
        await writeGate.WaitAsync();
        var rpc = Guid.NewGuid().ToString("D");
        phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc, op }));
        var reached = await Task.WhenAny(waiting.Task, Task.Delay(TimeSpan.FromSeconds(5))) == waiting.Task;
        if (reached) config.Paused = true;
        writeGate.Release();
        Check($"{op} reaches blocked serial writer", reached);
        if (!reached) { chat.PhoneControlWaitingForFixture = null; continue; }
        var denied = await WaitChat(phone, rpc);
        config.Paused = false;
        chat.PhoneControlWaitingForFixture = null;
        var controlHistoryAfter = await chat.CallAsync(new { op = "history" });
        Check($"paused phone cannot admit queued {op}", !denied.GetProperty("ok").GetBoolean()
            && controlHistoryAfter.GetProperty("chatAdmission").GetProperty("epoch").GetInt64() == beforeEpoch
            && controlHistoryAfter.GetProperty("chatAdmission").GetProperty("stopped").GetBoolean() == beforeStopped);
    }
    {
        var beforeRevoke = await chat.CallAsync(new { op = "history" });
        var beforeEpoch = beforeRevoke.GetProperty("chatAdmission").GetProperty("epoch").GetInt64();
        var writeGate = (SemaphoreSlim)typeof(AssistantClient).GetField("_write", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!.GetValue(chat)!;
        var waiting = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var denied = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        chat.PhoneControlWaitingForFixture = () => waiting.TrySetResult();
        chat.PhoneControlDeniedForFixture = () => denied.TrySetResult();
        await writeGate.WaitAsync();
        phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc = Guid.NewGuid().ToString("D"), op = "restart_chat" }));
        var reached = await Task.WhenAny(waiting.Task, Task.Delay(TimeSpan.FromSeconds(5))) == waiting.Task;
        if (reached) engine.Revoke(config.Devices[0]);
        writeGate.Release();
        var rejected = reached && await Task.WhenAny(denied.Task, Task.Delay(TimeSpan.FromSeconds(5))) == denied.Task;
        chat.PhoneControlWaitingForFixture = null; chat.PhoneControlDeniedForFixture = null;
        var afterRevoke = await chat.CallAsync(new { op = "history" });
        Check("revoked phone cannot admit queued restart", rejected
            && afterRevoke.GetProperty("chatAdmission").GetProperty("epoch").GetInt64() == beforeEpoch);
    }
    phone.Dispose(); engine.Dispose(); await relay.StopAsync(); return failures;
}
var info = await phone.WaitMessage(Msg.Info, TimeSpan.FromSeconds(10));
var infoObject = info is null ? default : JsonDocument.Parse(info).RootElement.Clone();
Check("host sends screen info", info is not null && infoObject.GetProperty("control").GetBoolean() == !args.Contains("--stop-removed-only"));
// Host side of the 100.011 zoom/fit contract: the phone's fit/zoom math
// consumes the w/h the host advertises here, so pin them to the real
// screen size. (The zoom/fit behaviour itself lives in Android
// DesktopZoomState and is B2-G's spec to add.)
Check("host advertises its real screen size (the dimensions the phone fit/zoom math consumes)",
    info is not null && infoObject.GetProperty("w").GetInt32() == originalDisplay.Width
    && infoObject.GetProperty("h").GetInt32() == originalDisplay.Height);
Check("host advertises foreground controls", info is not null && infoObject.GetProperty("foregroundWindow").GetInt32() == 1);
Check("host advertises full file browser", info is not null && infoObject.GetProperty("fileBrowser").GetInt32() >= 3);
if (args.Contains("--full-access-only"))
{
    engine.InitializeFullPcAccess();
    await chat.CallAsync(new { op = "local_configure", model = "fixture-tool-model", endpoint = "http://127.0.0.1:1" });
    async Task<JsonElement> Access(bool enabled)
    {
        var rpc = Guid.NewGuid().ToString("D");
        phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc, op = "access_set", assistant = "local", account = "", enabled }));
        return await WaitChat(phone, rpc);
    }
    var enabled = await Access(true);
    Check("control-authorized phone can enable full access", enabled.GetProperty("ok").GetBoolean() && enabled.GetProperty("result").GetProperty("state").GetString() == "On");
    engine.SetControl(config.Devices[0], false);
    var denied = await Access(true);
    Check("view-only phone cannot enable full access", !denied.GetProperty("ok").GetBoolean());
    var disabled = await Access(false);
    Check("view-only phone can revoke full access", disabled.GetProperty("ok").GetBoolean() && !engine.AssistantActionsForFixture.HasActiveGrant());
    engine.SetControl(config.Devices[0], true);
    var accessResumed = await Access(true);
    Check("restored phone control can explicitly enable", accessResumed.GetProperty("ok").GetBoolean());
    // STOP latch removed (Phil 2026-11-09): a phone stop request is a
    // no-op ack (muse_stop=not_enabled); full access is unaffected.
    phone.Send(Msg.StopRequest, JsonSerializer.SerializeToUtf8Bytes(new { stop_id = Guid.NewGuid().ToString("D") }));
    var stopReceipt = await phone.WaitMessage(Msg.StopStatus, TimeSpan.FromSeconds(5));
    var stopReceiptValue = stopReceipt is null ? null : JsonDocument.Parse(stopReceipt).RootElement.GetProperty("muse_stop").GetString();
    Check("phone stop request is answered not_enabled and leaves full access on", stopReceiptValue == "not_enabled" && engine.AssistantActionsForFixture.HasActiveGrant());
    phone.Dispose(); await relay.StopAsync();
    return failures;
}
phone.Send(Msg.DesktopList, JsonSerializer.SerializeToUtf8Bytes(new DesktopListRequest(Guid.NewGuid(), null, ThisPc: true, Paged: true), DesktopWire.Json));
var rootsPayload = await phone.WaitMessage(Msg.DesktopListResult, TimeSpan.FromSeconds(10));
var roots = rootsPayload is null ? null : JsonSerializer.Deserialize<DesktopListResponse>(rootsPayload, DesktopWire.Json);
var desktopRootItem = roots?.Items.FirstOrDefault(item => item.Name == "Desktop");
Check("encrypted file browser exposes PC drives", roots?.Error is null && desktopRootItem is not null && roots.Items.Any(item => item.Name.EndsWith(@":\")));
if (desktopRootItem is not null)
{
    phone.Send(Msg.DesktopList, JsonSerializer.SerializeToUtf8Bytes(new DesktopListRequest(Guid.NewGuid(), desktopRootItem.Token, Paged: true), DesktopWire.Json));
    var folderPayload = await phone.WaitMessage(Msg.DesktopListResult, TimeSpan.FromSeconds(10));
    var folder = folderPayload is null ? null : JsonSerializer.Deserialize<DesktopListResponse>(folderPayload, DesktopWire.Json);
    var item = folder?.Items.FirstOrDefault(entry => entry.Name == "fixture.txt");
    if (item is not null)
    {
        phone.Send(Msg.DesktopOpen, JsonSerializer.SerializeToUtf8Bytes(new DesktopOpenRequest(Guid.NewGuid(), item.Token, true, RevealLocation: true), DesktopWire.Json));
        var revealPayload = await phone.WaitMessage(Msg.DesktopOpenResult, TimeSpan.FromSeconds(10));
        var reveal = revealPayload is null ? null : JsonSerializer.Deserialize<DesktopOpenResult>(revealPayload, DesktopWire.Json);
        Check("encrypted reveal respects control permission", args.Contains("--stop-removed-only") ? reveal?.Error == "denied" && desktopOpener.RevealCount == 0 : reveal?.Error is null && desktopOpener.RevealCount == 1);
    }
    else Check("fixture file available for reveal", false);
}
var foregroundId = Guid.NewGuid();
var foregroundPayload = JsonSerializer.SerializeToUtf8Bytes(new ForegroundWindowRequest(foregroundId, "minimize"), DesktopWire.Json);
// B2: foreground work now travels on its own type (24). The reply still
// arrives on 19 — the legacy value shipped phones listen on — until
// Android adopts Msg.ForegroundWindowResult (25) (B2-G).
phone.Send(Msg.ForegroundWindow, foregroundPayload);
var foregroundResponse = await phone.WaitMessage(Msg.StopStatus, TimeSpan.FromSeconds(10));
var foreground = foregroundResponse is null ? null : JsonSerializer.Deserialize<ForegroundWindowResult>(foregroundResponse, DesktopWire.Json);
Check("foreground request on its own type routes separately from a stop request", foreground?.RequestId == foregroundId &&
    (args.Contains("--stop-removed-only") ? foreground.Error == "denied" && desktopOpener.ForegroundCount == 0 : foreground.Error is null && desktopOpener.ForegroundCount == 1));
// Legacy path: the same request sent on 18 (old phone builds) must
// also dedupe, not re-execute.
phone.Send(Msg.StopRequest, foregroundPayload);
_ = await phone.WaitMessage(Msg.StopStatus, TimeSpan.FromSeconds(10));
Check("duplicate foreground request is not executed again", desktopOpener.ForegroundCount == (args.Contains("--stop-removed-only") ? 0 : 1));
if (args.Contains("--foreground-only"))
{
    phone.Dispose();
    engine.Dispose();
    await relay.StopAsync();
    return failures;
}

if (args.Contains("--assistant-authority"))
{
    var taskId = Guid.NewGuid(); var authorityTurn = Guid.NewGuid();
    Check("host refuses AI task without local grant", !engine.BeginAssistantTask(taskId, authorityTurn, "local", "fixture", "", ""));
    await TestPhoneLocalTask(engine, chat, phone, Check);
    engine.GrantJettDesktopLocally();
    Check("local grant allows host-owned AI task", engine.BeginAssistantTask(taskId, authorityTurn, "local", "fixture", "", ""));
    var firstId = Guid.NewGuid();
    var listed = await engine.ExecuteAssistantAction(taskId, authorityTurn, firstId, "desktop.list", "{\"folder_token\":null}");
    Check("AI task lists only test Desktop", listed.Status == "completed" && listed.Data is DesktopListResponse response && response.Items.Any(x => x.Name == "fixture.txt"));
    var duplicateAction = await engine.ExecuteAssistantAction(taskId, authorityTurn, firstId, "desktop.list", "{\"folder_token\":null}");
    Check("AI action ID cannot be executed twice", duplicateAction.Error == "duplicate_action");
    var fixtureToken = ((DesktopListResponse)listed.Data!).Items.Single(item => item.Name == "fixture.txt").Token;
    var launchArgs = JsonSerializer.Serialize(new { item_token = fixtureToken, purpose = "Review fixture" });
    var phoneDeniedLaunch = engine.ExecuteAssistantAction(taskId, authorityTurn, Guid.NewGuid(), "filesystem.launch", launchArgs);
    var approvalRpc = Guid.NewGuid().ToString("D");
    phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc = approvalRpc, op = "approval_list" }));
    var approvalResponse = await WaitChat(phone, approvalRpc);
    var phoneApproval = approvalResponse.GetProperty("result").GetProperty("approvals").EnumerateArray().Single();
    Check("phone receives approval bound to the active task", phoneApproval.GetProperty("taskId").GetString() == taskId.ToString("D"));
    var approvalId = phoneApproval.GetProperty("id").GetString();
    var denyRpc = Guid.NewGuid().ToString("D");
    phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc = denyRpc, op = "approval_resolve", approval_id = approvalId, approved = false }));
    var denyResponse = await WaitChat(phone, denyRpc);
    Check("phone can deny a pending AI action", denyResponse.GetProperty("ok").GetBoolean() && (await phoneDeniedLaunch).Status == "cancelled");
    var longContent = new string('x', 3072) + " END";
    var pendingEdit = engine.ExecuteAssistantAction(taskId, authorityTurn, Guid.NewGuid(), "filesystem.replace", JsonSerializer.Serialize(new { item_token = fixtureToken, purpose = "Review longer edit", content = longContent }));
    var editRpc = Guid.NewGuid().ToString("D");
    phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc = editRpc, op = "approval_list" }));
    var editResponse = await WaitChat(phone, editRpc);
    var editApproval = editResponse.GetProperty("result").GetProperty("approvals").EnumerateArray().Single();
    Check("phone receives full longer edit before approval", editApproval.GetProperty("target").GetString()!.EndsWith(longContent, StringComparison.Ordinal));
    var denyEditRpc = Guid.NewGuid().ToString("D");
    phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc = denyEditRpc, op = "approval_resolve", approval_id = editApproval.GetProperty("id").GetString(), approved = false }));
    var denyEdit = await WaitChat(phone, denyEditRpc);
    Check("phone can deny longer edit without modifying file", denyEdit.GetProperty("ok").GetBoolean() && (await pendingEdit).Status == "cancelled" && File.ReadAllText(Path.Combine(desktopRoot, "fixture.txt")) == "integration fixture");
    var phoneAllowedLaunch = engine.ExecuteAssistantAction(taskId, authorityTurn, Guid.NewGuid(), "filesystem.launch", launchArgs);
    var allowedId = engine.Approvals.Pending().Single().Id.ToString("D");
    var allowRpc = Guid.NewGuid().ToString("D");
    phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc = allowRpc, op = "approval_resolve", approval_id = allowedId, approved = true }));
    var allowResponse = await WaitChat(phone, allowRpc);
    Check("phone can approve one fixture launch", allowResponse.GetProperty("ok").GetBoolean() && (await phoneAllowedLaunch).Status == "uncertain" && desktopOpener.Count == 1);
    var probeTask = Guid.NewGuid(); var probeTurn = Guid.NewGuid();
    Check("separate task starts for phone stop test", engine.BeginAssistantTask(probeTask, probeTurn, "local", "fixture", "", ""));
    var probeListing = await engine.ExecuteAssistantAction(probeTask, probeTurn, Guid.NewGuid(), "desktop.list", "{\"folder_token\":null}");
    var probeToken = ((DesktopListResponse)probeListing.Data!).Items.Single(item => item.Name == "fixture.txt").Token;
    _ = engine.ExecuteAssistantAction(probeTask, probeTurn, Guid.NewGuid(), "filesystem.launch", JsonSerializer.Serialize(new { item_token = probeToken, purpose = "Verify a phone stop request leaves this approval alone" }));
    Check("phone stop fixture has a pending approval", engine.Approvals.Pending().Any(approval => approval.TaskId == probeTask));
    // STOP latch removed (Phil 2026-11-09): the stop request is answered
    // not_enabled and must leave the task and its approval untouched.
    phone.Send(Msg.StopRequest, JsonSerializer.SerializeToUtf8Bytes(new { stop_id = Guid.NewGuid().ToString("D") }));
    var probeReceipt = await phone.WaitMessage(Msg.StopStatus, TimeSpan.FromSeconds(5));
    var probeReceiptValue = probeReceipt is null ? null : JsonDocument.Parse(probeReceipt).RootElement.GetProperty("muse_stop").GetString();
    Check("phone stop request is answered not_enabled", probeReceiptValue == "not_enabled");
    var afterProbe = await engine.ExecuteAssistantAction(probeTask, probeTurn, Guid.NewGuid(), "desktop.list", "{\"folder_token\":null}");
    Check("phone stop request does not block host AI action", afterProbe.Status != "rejected");
    Check("phone stop request leaves a pending approval alone", engine.Approvals.Pending().Any(approval => approval.TaskId == probeTask));
    var chatProbeTask = Guid.NewGuid(); var chatProbeTurn = Guid.NewGuid();
    Check("new task starts while the grant stays active", engine.BeginAssistantTask(chatProbeTask, chatProbeTurn, "local", "fixture", "", ""));
    var resumeRpc = Guid.NewGuid().ToString("D");
    phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc = resumeRpc, op = "resume_chat" }));
    var chatResumeReply = await WaitChat(phone, resumeRpc);
    Check("phone resumes ordinary chat", chatResumeReply.GetProperty("ok").GetBoolean());
    phone.Dispose(); engine.Dispose(); await relay.StopAsync(); return failures;
}





System.Security.Cryptography.CryptographicOperations.ZeroMemory(jettCapability);

if (args.Contains("--stop-removed-only"))
{
    // STOP latch removed (Phil 2026-11-09): the always-on grant model wins
    // over the old 30-minute local grant pin. A phone stop request is a
    // no-op ack (muse_stop=not_enabled): nothing stops, no latch is set,
    // and revoking the grant never re-latches -- it can be created again.
    var stopId = Guid.NewGuid().ToString("D");
    phone.Send(Msg.StopRequest, JsonSerializer.SerializeToUtf8Bytes(new { stop_id = stopId }));
    var receipt = await phone.WaitMessage(Msg.StopStatus, TimeSpan.FromSeconds(5));
    var receiptStop = receipt is null ? null : JsonDocument.Parse(receipt).RootElement.GetProperty("muse_stop").GetString();
    Check("phone stop request is answered not_enabled, not a latch", receiptStop == "not_enabled");
    // B2: the receipt must route through a branch Android actually handles —
    // it must carry "latched" and echo the request's "stop_id", or the phone
    // silently drops it and only the 10-second timeout clears the banner.
    var receiptObject = receipt is null ? default : JsonDocument.Parse(receipt).RootElement.Clone();
    Check("stop receipt carries latched=false and echoes stop_id", receipt is not null
        && receiptObject.GetProperty("latched").GetBoolean() == false
        && receiptObject.GetProperty("stop_id").GetString() == stopId);
    engine.GrantPersistentDesktopLocally();
    Check("full access grant is always-on, not a 30-minute grant", engine.DesktopGrantExpiresUtc == DateTimeOffset.MaxValue && engine.AssistantActionsForFixture.HasActiveGrant());
    phone.Send(Msg.StopRequest, JsonSerializer.SerializeToUtf8Bytes(new { stop_id = Guid.NewGuid().ToString("D") }));
    var duplicateReceipt = await phone.WaitMessage(Msg.StopStatus, TimeSpan.FromSeconds(5));
    var duplicateStop = duplicateReceipt is null ? null : JsonDocument.Parse(duplicateReceipt).RootElement.GetProperty("muse_stop").GetString();
    Check("duplicate phone stop request is answered not_enabled and does not revoke the grant",
        duplicateStop == "not_enabled" && engine.DesktopGrantExpiresUtc == DateTimeOffset.MaxValue && engine.AssistantActionsForFixture.HasActiveGrant());
    var stopRemovedTask = Guid.NewGuid(); var stopRemovedTurn = Guid.NewGuid();
    Check("AI task still starts after phone stop requests", engine.BeginAssistantTask(stopRemovedTask, stopRemovedTurn, "local", "fixture", "", ""));
    var afterProbe = await engine.ExecuteAssistantAction(stopRemovedTask, stopRemovedTurn, Guid.NewGuid(), "desktop.list", "{\"folder_token\":null}");
    Check("host AI action is not rejected after phone stop requests", afterProbe.Status != "rejected");
    engine.RevokeJettDesktopLocally();
    Check("revoking the grant leaves no latch behind", engine.DesktopGrantExpiresUtc is null && !engine.AssistantActionsForFixture.HasActiveGrant());
    engine.GrantPersistentDesktopLocally();
    var revivedTask = Guid.NewGuid(); var revivedTurn = Guid.NewGuid();
    Check("grant can be created again after revoke", engine.DesktopGrantExpiresUtc == DateTimeOffset.MaxValue && engine.BeginAssistantTask(revivedTask, revivedTurn, "local", "fixture", "", ""));

    phone.Dispose();
    engine.Dispose();
    chat.Dispose();
    await relay.StopAsync();
    Directory.Delete(configDir, true);
    Console.WriteLine(failures == 0 ? "STOP-removal integration specs passed." : $"{failures} STOP-removal integration spec(s) failed.");
    return failures;
}
var listId = Guid.NewGuid();
phone.Send(Msg.DesktopList, JsonSerializer.SerializeToUtf8Bytes(new DesktopListRequest(listId, null), DesktopWire.Json));
var listPayload = await phone.WaitMessage(Msg.DesktopListResult, TimeSpan.FromSeconds(10));
var desktop = listPayload is null ? null : JsonSerializer.Deserialize<DesktopListResponse>(listPayload, DesktopWire.Json);
Check("encrypted Desktop listing", desktop is { Error: null } && desktop.RequestId == listId && desktop.Items.Single().Name == "fixture.txt");
if (desktop is { Error: null })
{
    var request = new DesktopOpenRequest(Guid.NewGuid(), desktop.Items.Single().Token, true);
    var bytes = JsonSerializer.SerializeToUtf8Bytes(request, DesktopWire.Json);
    phone.Send(Msg.DesktopOpen, bytes);
    var result = await phone.WaitMessage(Msg.DesktopOpenResult, TimeSpan.FromSeconds(10));
    Check("encrypted authorized open uses fake shell", result is not null && JsonSerializer.Deserialize<DesktopOpenResult>(result, DesktopWire.Json)!.Error is null && desktopOpener.Count == 1);
    phone.Send(Msg.DesktopOpen, bytes);
    await phone.WaitMessage(Msg.DesktopOpenResult, TimeSpan.FromSeconds(10));
    Check("encrypted duplicate open is idempotent", desktopOpener.Count == 1);
    phone.Send(Msg.DesktopList, Encoding.UTF8.GetBytes("{bad json"));
    var bad = await phone.WaitMessage(Msg.DesktopListResult, TimeSpan.FromSeconds(10));
    Check("malformed browser request returns safe error", bad is not null && JsonSerializer.Deserialize<DesktopListResponse>(bad, DesktopWire.Json)!.Error == "malformed");
}
// ---- OTA: actual encrypted request/response path ----
byte[] UpdateCheckRequest() => JsonSerializer.SerializeToUtf8Bytes(new { requestId = Guid.NewGuid().ToString() });
phone.Send(Msg.UpdateCheck, UpdateCheckRequest());
var missing = await phone.WaitMessage(Msg.UpdateInfo, TimeSpan.FromSeconds(5));
Check("missing APK does not break paired connection", missing is not null && !JsonDocument.Parse(missing).RootElement.GetProperty("available").GetBoolean());
Directory.CreateDirectory(updatesDir);
var updateBytes = Enumerable.Range(0, 150000).Select(i => (byte)(i % 251)).ToArray();
var hash = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(updateBytes)).ToLowerInvariant();
var release = new AndroidUpdates.Release("com.deskhand.android", 3, "0.1.3", updateBytes.Length, 28, hash);
File.WriteAllBytes(Path.Combine(updatesDir, hash + ".apk"), updateBytes);
File.WriteAllBytes(Path.Combine(updatesDir, "latest.json"), JsonSerializer.SerializeToUtf8Bytes(release));
phone.Send(Msg.UpdateCheck, UpdateCheckRequest());
var offer = await phone.WaitMessage(Msg.UpdateInfo, TimeSpan.FromSeconds(5));
Check("encrypted update metadata matches Android manifest contract", offer is not null && JsonDocument.Parse(offer).RootElement.GetProperty("release").GetProperty("sha256").GetString() == hash);
// Publishing a different latest manifest must not alter the current snapshot.
File.WriteAllText(Path.Combine(updatesDir, "latest.json"), "{}");
using (var reconstructed = new MemoryStream())
{
    for (long offset = 0; offset < updateBytes.Length; offset += AndroidUpdates.ChunkSize)
    {
        phone.Send(Msg.UpdateGet, JsonSerializer.SerializeToUtf8Bytes(new { sha256 = hash, offset }));
        var chunk = await phone.WaitMessage(Msg.UpdateChunk, TimeSpan.FromSeconds(5));
        Check("chunk has matching digest and big-endian offset " + offset, chunk is not null && Encoding.ASCII.GetString(chunk, 0, 64) == hash && System.Buffers.Binary.BinaryPrimitives.ReadInt64BigEndian(chunk.AsSpan(64, 8)) == offset);
        if (chunk is not null) reconstructed.Write(chunk, 72, chunk.Length - 72);
    }
    Check("immutable snapshot reconstructs complete APK despite new publication", reconstructed.ToArray().SequenceEqual(updateBytes));
}
phone.Send(Msg.UpdateGet, JsonSerializer.SerializeToUtf8Bytes(new { sha256 = hash, offset = -1 }));
Check("invalid update offset is rejected", await phone.WaitMessage(Msg.UpdateError, TimeSpan.FromSeconds(5)) is not null);
phone.Send(Msg.UpdateGet, JsonSerializer.SerializeToUtf8Bytes(new { sha256 = "../outside", offset = 0 }));
Check("wrong snapshot is rejected", await phone.WaitMessage(Msg.UpdateError, TimeSpan.FromSeconds(5)) is not null);
File.WriteAllBytes(Path.Combine(updatesDir, "latest.json"), JsonSerializer.SerializeToUtf8Bytes(release));
phone.Send(Msg.UpdateCheck, UpdateCheckRequest());
await phone.WaitMessage(Msg.UpdateInfo, TimeSpan.FromSeconds(5));
// Delete sharing permits replacing the path while the earlier file handle stays immutable.
File.Move(Path.Combine(updatesDir, hash + ".apk"), Path.Combine(updatesDir, "old.apk"));
File.WriteAllBytes(Path.Combine(updatesDir, hash + ".apk"), new byte[150000]);
phone.Send(Msg.UpdateCheck, UpdateCheckRequest());
Check("corrupt staged APK is rejected", await phone.WaitMessage(Msg.UpdateError, TimeSpan.FromSeconds(5)) is not null);
Check("wire message IDs match Kotlin contract", Msg.UpdateCheck == 40 && Msg.UpdateInfo == 41 && Msg.UpdateGet == 42 && Msg.UpdateChunk == 43 && Msg.UpdateError == 44);
if (args.Contains("--hybrid-video"))
{
    Check("host advertises available adaptive video", info is not null && JsonDocument.Parse(info).RootElement.GetProperty("adaptiveVideo").GetInt32() == 1);
    using var motion = new CancellationTokenSource();
    var animate = Task.Run(async () => { while (!motion.IsCancellationRequested) { fixture!.Change(); await Task.Delay(40); } });
    phone.Send(Msg.Start, [0, 1]);
    var videos = 0;
    var deadline = DateTime.UtcNow.AddSeconds(10);
    while (videos < 30 && DateTime.UtcNow < deadline)
    {
        var video = await phone.WaitMessage(Msg.VideoFrame, TimeSpan.FromSeconds(2));
        if (video is null) continue;
        Check("video packet carries bounded dimensions and Annex B", video.Length > 20 && System.Buffers.Binary.BinaryPrimitives.ReadInt32BigEndian(video.AsSpan(8)) is >= 2 and <= 1280 && video[16] == 0 && video[17] == 0);
        phone.Send(Msg.VideoAck, video[..8]); videos++;
    }
    Check("negotiated session receives GPU video", videos == 30);
    motion.Cancel(); await animate;
    byte[]? sharp = null;
    var sharpDeadline = DateTime.UtcNow.AddSeconds(5);
    while (DateTime.UtcNow < sharpDeadline && await phone.WaitAny(TimeSpan.FromSeconds(2)) is { } message)
    {
        if (message.Type == Msg.VideoFrame) phone.Send(Msg.VideoAck, message.Payload[..8]);
        if (message.Type == Msg.Frame) { sharp = message.Payload; break; }
    }
    Check("settled video switches to native sharp JPEG", sharp is not null && SyntheticCapture.IsLatest(sharp, fixture!.Version));
    if (sharp is not null)
    {
        using var imageStream = new MemoryStream(sharp);
        using var bitmap = new System.Drawing.Bitmap(imageStream);
        Check("sharp refinement retains native resolution", bitmap.Width == 1920 && bitmap.Height == 1080);
        phone.Send(Msg.Ack, []);
    }
    Check("settled session does not continuously resend sharp images", await phone.WaitMessage(Msg.Frame, TimeSpan.FromMilliseconds(800)) is null);
    phone.Send(Msg.VideoFailed, []);
    fixture!.Change();
    Check("decoder failure falls back to compatible JPEG", await phone.WaitMessage(Msg.Frame, TimeSpan.FromSeconds(5)) is not null);
    Console.WriteLine($"Hybrid integration: {failures} failures");
    return failures;
}
phone.Send(Msg.Start, []);
Check("host advertises Files window back capability", info is not null && JsonDocument.Parse(info).RootElement.GetProperty("desktopBack").GetInt32() == 1);
var backRequest = new DesktopBackRequest(Guid.NewGuid());
phone.Send(Msg.DesktopBack, JsonSerializer.SerializeToUtf8Bytes(backRequest, DesktopWire.Json));
var backReply = await phone.WaitMessage(Msg.DesktopBackResult, TimeSpan.FromSeconds(10));
Check("encrypted window back closes fake target", backReply is not null && JsonSerializer.Deserialize<DesktopBackResult>(backReply, DesktopWire.Json)!.Error is null && desktopOpener.CloseCount == 1);
phone.Send(Msg.DesktopBack, JsonSerializer.SerializeToUtf8Bytes(backRequest, DesktopWire.Json));
backReply = await phone.WaitMessage(Msg.DesktopBackResult, TimeSpan.FromSeconds(10));
Check("duplicate encrypted back never closes next window", backReply is not null && desktopOpener.CloseCount == 1);
phone.Send(Msg.DesktopBack, Encoding.UTF8.GetBytes("{\"requestId\":\"" + Guid.NewGuid() + "\",\"hwnd\":123}"));
backReply = await phone.WaitMessage(Msg.DesktopBackResult, TimeSpan.FromSeconds(10));
Check("client cannot choose a window handle", backReply is not null && JsonSerializer.Deserialize<DesktopBackResult>(backReply, DesktopWire.Json)!.Error == "malformed" && desktopOpener.CloseCount == 1);
var frames = 0;
var validJpeg = true;
var started = DateTime.UtcNow;
while (frames < 5 && DateTime.UtcNow - started < TimeSpan.FromSeconds(15))
{
    // Adaptive streaming deliberately suppresses idle duplicates. Generate a visible cursor change.
    if (fixture is not null) fixture.Change();
    else phone.Send(Msg.Input, Encoding.UTF8.GetBytes($"{{\"t\":\"move\",\"x\":{0.2 + frames * .03},\"y\":0.4}}"));
    var f = await phone.WaitMessage(Msg.Frame, TimeSpan.FromSeconds(5));
    if (f is null) break;
    frames++;
    validJpeg &= f.Length > 1000 && f[0] == 0xFF && f[1] == 0xD8;
    phone.Send(Msg.Ack, []);
}
Check("receives decrypted JPEG frames", frames >= 5 && validJpeg);
Console.WriteLine($"Frame diagnostics: received={frames}, validJpeg={validJpeg}");
Check("engine reports active streaming", engine.ActiveDevices.Contains("Test Phone"));
if (fixture is not null)
{
    // Drain the final motion frame/refinement, acknowledging each, then require silence.
    for (var i = 0; i < 5; i++)
    {
        var extra = await phone.WaitMessage(Msg.Frame, TimeSpan.FromMilliseconds(800));
        if (extra is null) break;
        phone.Send(Msg.Ack, []);
    }
    Check("unchanged desktop stops transmitting after refinement", await phone.WaitMessage(Msg.Frame, TimeSpan.FromMilliseconds(700)) is null);
    fixture.Localized = true;
    fixture.EditDuringEncode = true;
    fixture.Change();
    var local = await phone.WaitMessage(Msg.Frame, TimeSpan.FromSeconds(1));
    if (local is not null) phone.Send(Msg.Ack, []);
    var newerLocal = await phone.WaitMessage(Msg.Frame, TimeSpan.FromSeconds(1));
    Check("localized edit during encoding neither starves nor loses latest image", local is not null && newerLocal is not null && SyntheticCapture.IsLatest(newerLocal, fixture.Version));
    if (newerLocal is not null) phone.Send(Msg.Ack, []);
    fixture.Localized = false;
    var renewedAt = System.Diagnostics.Stopwatch.StartNew();
    fixture.Change();
    var renewed = await phone.WaitMessage(Msg.Frame, TimeSpan.FromMilliseconds(1000));
    Check("settled polling detects renewed motion", renewed is not null && renewedAt.ElapsedMilliseconds < 300);
    Console.WriteLine($"Adaptive renewed-frame delay: {renewedAt.ElapsedMilliseconds}ms");
    // Do not acknowledge: at most one further frame may enter the two-frame window.
    fixture.Change();
    var second = await phone.WaitMessage(Msg.Frame, TimeSpan.FromSeconds(1));
    fixture.Change();
    Check("slow ACKs bound window to two", second is not null && await phone.WaitMessage(Msg.Frame, TimeSpan.FromMilliseconds(800)) is null);
    // A waiting refinement is overtaken by a newer substantial change, before credits return.
    fixture.Change();
    await Task.Delay(150);
    phone.Send(Msg.Ack, []);
    var latest = await phone.WaitMessage(Msg.Frame, TimeSpan.FromSeconds(1));
    Check("latest pending motion replaces obsolete refinement", latest is not null && SyntheticCapture.IsLatest(latest, fixture.Version));
    phone.Send(Msg.Ack, []); phone.Send(Msg.Ack, []);
}

Check("can read interactive cursor", GetCursorPos(out var before));
var b = System.Windows.Forms.Screen.PrimaryScreen!.Bounds;
phone.Send(Msg.Input, Encoding.UTF8.GetBytes("{\"t\":\"move\",\"x\":0.25,\"y\":0.75}"));
await Task.Delay(600);
Check("can read cursor after input", GetCursorPos(out var after));
Check("can restore cursor", SetCursorPos(before.X, before.Y));
Console.WriteLine($"Cursor diagnostics: before=({before.X},{before.Y}), after=({after.X},{after.Y}), screen={b}");
Check("touch input moves the cursor", Math.Abs(after.X - (b.X + b.Width * 0.25)) < 4 && Math.Abs(after.Y - (b.Y + b.Height * 0.75)) < 4);

engine.SetControl(config.Devices[0], false);
var permissionInfo = await phone.WaitMessage(Msg.Info, TimeSpan.FromSeconds(10));
Check("control changes pushed to phone", permissionInfo is not null && !JsonDocument.Parse(permissionInfo).RootElement.GetProperty("control").GetBoolean());
phone.Send(Msg.DesktopBack, JsonSerializer.SerializeToUtf8Bytes(new DesktopBackRequest(Guid.NewGuid()), DesktopWire.Json));
backReply = await phone.WaitMessage(Msg.DesktopBackResult, TimeSpan.FromSeconds(10));
Check("view-only window close denied", backReply is not null && JsonSerializer.Deserialize<DesktopBackResult>(backReply, DesktopWire.Json)!.Error == "denied" && desktopOpener.CloseCount == 1);
if (desktop is { Error: null })
{
    phone.Send(Msg.DesktopOpen, JsonSerializer.SerializeToUtf8Bytes(new DesktopOpenRequest(Guid.NewGuid(), desktop.Items.Single().Token, true), DesktopWire.Json));
    var denied = await phone.WaitMessage(Msg.DesktopOpenResult, TimeSpan.FromSeconds(10));
    Check("view-only open denied by host", denied is not null && JsonSerializer.Deserialize<DesktopOpenResult>(denied, DesktopWire.Json)!.Error == "denied" && desktopOpener.Count == 1);
    phone.Send(Msg.DesktopList, JsonSerializer.SerializeToUtf8Bytes(new DesktopListRequest(Guid.NewGuid(), null), DesktopWire.Json));
    var allowed = await phone.WaitMessage(Msg.DesktopListResult, TimeSpan.FromSeconds(10));
    Check("view-only listing allowed by host", allowed is not null && JsonSerializer.Deserialize<DesktopListResponse>(allowed, DesktopWire.Json)!.Error is null);
}
phone.Send(Msg.Input, Encoding.UTF8.GetBytes("{\"t\":\"move\",\"x\":0.9,\"y\":0.1}"));
await Task.Delay(600);
GetCursorPos(out var blocked);
Check("input ignored when control is blocked", Math.Abs(blocked.X - (b.X + b.Width * 0.9)) > 20);
SetCursorPos(before.X, before.Y);

// Both bars use the same child/store; the simulated phone uses the encrypted relay.
var catalog = await chat.CallAsync(new { op = "catalog" });
var selectedRoute = catalog.GetProperty("routes").EnumerateArray().First(x => x.GetProperty("assistant").GetString() == "codex");
var turnId = Guid.NewGuid().ToString();
var prompt = args.Contains("--live-chat") ? "Reply with exactly DESKHAND_CHAT_WIRED_OK. Do not use tools." : "hello from Windows";
await chat.CallAsync(new { op = "submit", id = turnId, text = prompt,
    assistant = "codex", model = selectedRoute.GetProperty("model").GetString(),
    account = selectedRoute.GetProperty("account").GetString(), effort = selectedRoute.GetProperty("effort").GetString() });
var terminal = await WaitTurn(chat, turnId);
Check("Windows chat receives correlated assistant reply", terminal.GetProperty("status").GetString() == "completed" &&
    terminal.GetProperty("reply").GetString() == (args.Contains("--live-chat") ? "DESKHAND_CHAT_WIRED_OK" : "Fixture reply: hello from Windows"));
var historyRpc = Guid.NewGuid().ToString();
phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc = historyRpc, op = "history" }));
var phoneHistory = await WaitChat(phone, historyRpc);
Check("phone chat sees Windows turn through encrypted relay", phoneHistory.GetProperty("ok").GetBoolean() &&
    phoneHistory.GetProperty("result").GetProperty("turns").EnumerateArray().Any(x => x.GetProperty("id").GetString() == turnId && x.GetProperty("status").GetString() == "completed"));
var phoneTurn = Guid.NewGuid().ToString();
var phoneRpc = Guid.NewGuid().ToString();
var phonePrompt = args.Contains("--live-chat") ? "Reply with exactly DESKHAND_PHONE_CHAT_OK. Do not use tools." : "hello from phone";
var phoneRequest = new { rpc = phoneRpc, op = "submit", id = phoneTurn, text = phonePrompt,
    assistant = "codex", model = selectedRoute.GetProperty("model").GetString(),
    account = selectedRoute.GetProperty("account").GetString(), effort = selectedRoute.GetProperty("effort").GetString() };
phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(phoneRequest));
Check("phone chat submission acknowledged", (await WaitChat(phone, phoneRpc)).GetProperty("ok").GetBoolean());
var phoneReply = await WaitTurn(chat, phoneTurn);
Check("Windows sees correlated phone assistant reply", phoneReply.GetProperty("status").GetString() == "completed" &&
    phoneReply.GetProperty("reply").GetString() == (args.Contains("--live-chat") ? "DESKHAND_PHONE_CHAT_OK" : "Fixture reply: hello from phone"));
var duplicate = await chat.CallAsync(phoneRequest);
Check("duplicate phone turn does not dispatch again", duplicate.GetProperty("status").GetString() == "completed");

// ---- Kill switch and revoke ----
engine.SetPaused(true);
await Task.Delay(500);
Check("pause stops streaming", engine.ActiveDevices.Count == 0);
engine.SetPaused(false);
phone.Dispose();
await Task.Delay(500);
using var resumed = new TestPhone(Relay, deviceSecret);
resumed.Connect();
Check("browser session reconnects after pause", await resumed.WaitSession(TimeSpan.FromSeconds(10)) is not null);
await resumed.WaitMessage(Msg.Info, TimeSpan.FromSeconds(10));
if (desktop is { Error: null })
{
    resumed.Send(Msg.DesktopList, JsonSerializer.SerializeToUtf8Bytes(new DesktopListRequest(Guid.NewGuid(), desktop.FolderToken), DesktopWire.Json));
    var stale = await resumed.WaitMessage(Msg.DesktopListResult, TimeSpan.FromSeconds(10));
    Check("old browser tokens invalid after reconnect", stale is not null && JsonSerializer.Deserialize<DesktopListResponse>(stale, DesktopWire.Json)!.Error == "stale_item");
}
resumed.Dispose();
engine.Revoke(config.Devices[0]);
await Task.Delay(500);
var revoked = new TestPhone(Relay, deviceSecret);
revoked.Connect();
Check("revoked phone cannot connect", await revoked.WaitSession(TimeSpan.FromSeconds(3)) is null);
revoked.Dispose();
phone.Dispose();

await relay.StopAsync();
try { Directory.Delete(configDir, true); } catch (IOException) { }
Console.WriteLine(failures == 0 ? "All integration specs passed." : $"{failures} integration spec(s) failed.");
return failures;

static byte[] RandomBytes(int n) => System.Security.Cryptography.RandomNumberGenerator.GetBytes(n);

static async Task TestPhoneLocalTask(HostEngine engine, AssistantClient chat, TestPhone phone, Action<string, bool> check)
{
    var script = Path.GetFullPath(Path.Combine("..", "assistant_service", "test_local_server_fixture.py"));
    var info = new System.Diagnostics.ProcessStartInfo("python") { UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true };
    info.ArgumentList.Add("-u"); info.ArgumentList.Add(script);
    using var server = System.Diagnostics.Process.Start(info) ?? throw new InvalidOperationException("Cannot start local model fixture");
    try
    {
        var port = await server.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(10));
        if (!int.TryParse(port, out var value)) throw new InvalidOperationException("Local fixture did not start");
        await chat.CallAsync(new { op = "local_configure", model = "fixture-tool-model", endpoint = $"http://127.0.0.1:{value}" });
        await chat.CallAsync(new { op = "catalog", refresh = true });
        var deniedRpc = Guid.NewGuid().ToString("D"); var deniedTurn = Guid.NewGuid().ToString("D");
        phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc = deniedRpc, op = "submit", id = deniedTurn, text = "List files", assistant = "local", model = "fixture-tool-model", account = "", effort = "", agent = true }));
        var denied = await WaitChat(phone, deniedRpc);
        check("phone local task is refused without a PC grant", !denied.GetProperty("ok").GetBoolean());
        engine.GrantJettDesktopLocally();
        var rpc = Guid.NewGuid().ToString("D"); var turn = Guid.NewGuid().ToString("D");
        phone.Send(Msg.ChatRequest, JsonSerializer.SerializeToUtf8Bytes(new { rpc, op = "submit", id = turn, text = "List files", assistant = "local", model = "fixture-tool-model", account = "", effort = "", agent = true }));
        var queued = await WaitChat(phone, rpc);
        var row = await WaitTurn(chat, turn);
        check("phone starts a bound local model task after PC grant", queued.GetProperty("ok").GetBoolean() && row.GetProperty("status").GetString() == "completed" && row.GetProperty("reply").GetString() == "Found fixture.txt");
    }
    finally
    {
        if (!server.HasExited) { server.Kill(true); await server.WaitForExitAsync(); }
    }
}

static async Task<JsonElement> WaitTurn(AssistantClient chat, string id)
{
    var end = DateTime.UtcNow.AddSeconds(90);
    while (DateTime.UtcNow < end)
    {
        var row = await chat.CallAsync(new { op = "get", id });
        if (row.GetProperty("status").GetString() is "completed" or "failed" or "unknown") return row;
        await Task.Delay(100);
    }
    throw new TimeoutException("Chat reply timed out");
}

static async Task<JsonElement> WaitChat(TestPhone phone, string rpc)
{
    var end = DateTime.UtcNow.AddSeconds(45);
    while (DateTime.UtcNow < end)
    {
        var bytes = await phone.WaitMessage(Msg.ChatResult, TimeSpan.FromSeconds(5));
        if (bytes is null) continue;
        using var doc = JsonDocument.Parse(bytes);
        if (doc.RootElement.TryGetProperty("rpc", out var value) && value.GetString() == rpc) return doc.RootElement.Clone();
    }
    throw new TimeoutException("Phone chat response timed out");
}

[DllImport("user32.dll")] static extern bool GetCursorPos(out POINT p);
[DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
[StructLayout(LayoutKind.Sequential)] struct POINT { public int X, Y; }

sealed class TestPhone : IDisposable
{
    private readonly RelayLink _link;
    private readonly byte[] _secret;
    private Handshake? _hs;
    private SecureSession? _session;
    private readonly TaskCompletionSource<string> _ready = new();
    private readonly System.Collections.Concurrent.BlockingCollection<(byte type, byte[] payload)> _inbox = new();

    public TestPhone(string relay, byte[] secret, string? room = null)
    {
        _secret = secret;
        _link = new RelayLink(relay, room ?? Kdf.RoomId(secret), "client");
        _link.PeerJoined += () =>
        {
            _session = null;
            _hs = new Handshake(Role.Client, _secret);
            _ = _link.SendAsync(_hs.CreateHello());
        };
        _link.Message += data =>
        {
            if (_session is null)
            {
                var s = _hs?.Complete(data);
                if (s is null) return;
                _session = s;
                _ready.TrySetResult(s.ConfirmCode);
                return;
            }
            if (_session.TryOpen(data, out var t, out var p)) _inbox.Add((t, p));
        };
    }

    public void Connect() => _link.Start();
    public bool Ready => _session is not null;

    public async Task<string?> WaitSession(TimeSpan timeout)
    {
        var done = await Task.WhenAny(_ready.Task, Task.Delay(timeout));
        return done == _ready.Task ? _ready.Task.Result : null;
    }

    public Task<(byte Type, byte[] Payload)?> WaitAny(TimeSpan timeout) => Task.Run<(byte Type, byte[] Payload)?>(() =>
        _inbox.TryTake(out var message, timeout) ? (message.type, message.payload) : null);

    public Task<byte[]?> WaitMessage(byte type, TimeSpan timeout) => Task.Run(() =>
    {
        var end = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < end)
        {
            if (_inbox.TryTake(out var m, 100) && m.type == type) return (byte[]?)m.payload;
        }
        return null;
    });

    private readonly object _sendGate = new();

    public void Send(byte type, byte[] payload)
    {
        byte[] record;
        lock (_sendGate) record = _session!.Seal(type, payload);
        _link.SendAsync(record).GetAwaiter().GetResult();
    }

    public void Dispose() => _link.Dispose();
}

sealed class TestConnector : IDisposable
{
    private readonly object _gate = new();
    private readonly byte[] _secret;
    private readonly RelayLink _link;
    private readonly TaskCompletionSource<bool> _ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly System.Collections.Concurrent.BlockingCollection<(byte type, byte[] payload)> _inbox = new();
    private Handshake? _handshake;
    private SecureSession? _session;

    public TestConnector(string relay, string room, byte[] secret)
    {
        _secret = secret.ToArray();
        _link = new RelayLink(relay, room, "connector");
        _link.PeerJoined += BeginHandshake;
        _link.PeerLeft += EndSession;
        _link.Message += Receive;
    }

    public bool Ready { get { lock (_gate) return _session is not null; } }
    public void Connect() => _link.Start();

    private void BeginHandshake()
    {
        Handshake handshake;
        lock (_gate)
        {
            if (_session is not null) return;
            _handshake ??= new Handshake(Role.Connector, _secret, Role.Host);
            handshake = _handshake;
        }
        _ = _link.SendAsync(handshake.CreateHello());
    }

    private void Receive(byte[] data)
    {
        lock (_gate)
        {
            if (_session is null)
            {
                _handshake ??= new Handshake(Role.Connector, _secret, Role.Host);
                var session = _handshake.Complete(data);
                if (session is null) return;
                _handshake.Dispose();
                _handshake = null;
                _session = session;
                _ready.TrySetResult(true);
                return;
            }
            if (_session.TryOpen(data, out var type, out var payload)) _inbox.Add((type, payload));
        }
    }

    private void EndSession()
    {
        lock (_gate)
        {
            _session = null;
            _handshake?.Dispose();
            _handshake = null;
        }
    }

    public async Task<bool> WaitSession(TimeSpan timeout)
    {
        var done = await Task.WhenAny(_ready.Task, Task.Delay(timeout));
        return done == _ready.Task && _ready.Task.Result;
    }

    public Task<byte[]?> WaitMessage(byte type, TimeSpan timeout) => Task.Run(() =>
    {
        var end = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < end)
        {
            if (_inbox.TryTake(out var message, 100) && message.type == type) return (byte[]?)message.payload;
        }
        return null;
    });

    public void Send(byte type, byte[] payload)
    {
        byte[] record;
        lock (_gate) record = (_session ?? throw new InvalidOperationException("Connector is not authenticated.")).Seal(type, payload);
        if (!_link.SendAsync(record).GetAwaiter().GetResult()) throw new IOException("Fixture connector send failed.");
    }

    public void Dispose()
    {
        _link.Dispose();
        lock (_gate) { _session = null; _handshake?.Dispose(); _handshake = null; }
        System.Security.Cryptography.CryptographicOperations.ZeroMemory(_secret);
    }
}

sealed class TestDesktopOpener : IDesktopOpener, IDesktopBackOpener, IForegroundWindowControl, IDesktopLocationOpener
{
    public int Count;
    public int ForegroundCount;
    public IntPtr RealForegroundFixture;
    public int RevealCount;
    public void Reveal(string path) => Interlocked.Increment(ref RevealCount);
    public string? ActOnForeground(string action)
    {
        Interlocked.Increment(ref ForegroundCount);
        if (RealForegroundFixture == IntPtr.Zero) return null;
        return GetFixtureForeground() == RealForegroundFixture ? ForegroundWindows.Act(action) : "fixture_not_foreground";
    }
    [DllImport("user32.dll", EntryPoint = "GetForegroundWindow")] private static extern IntPtr GetFixtureForeground();
    public void Open(string path) => Interlocked.Increment(ref Count);
    public int CloseCount;
    public (string? Error, string? Title) CloseLatest() { Interlocked.Increment(ref CloseCount); return (null, "Fixture window"); }
    public void Dispose() { }
}

sealed class SyntheticCapture : IDesktopCapture
{
    private int _version = 1, _observed;
    public volatile bool Localized, EditDuringEncode;
    public int Version => Volatile.Read(ref _version);
    public int Width => 1920;
    public void Change() => Interlocked.Increment(ref _version);
    public DesktopChange Capture()
    {
        var version = Version;
        var changed = version != _observed;
        var first = _observed == 0;
        _observed = version;
        return new(changed, changed && !Localized, first);
    }
    public EncodedDesktop Encode(int width, long quality, int budget)
    {
        using var bitmap = new System.Drawing.Bitmap(1920, 1080);
        using (var graphics = System.Drawing.Graphics.FromImage(bitmap)) graphics.Clear(ColorFor(Version));
        var encoded = ScreenCapturer.EncodeBounded(bitmap, width, quality, budget);
        if (EditDuringEncode) { EditDuringEncode = false; Change(); }
        return encoded;
    }
    private static System.Drawing.Color ColorFor(int version) => version % 2 == 0 ? System.Drawing.Color.Red : System.Drawing.Color.Blue;
    public static bool IsLatest(byte[] bytes, int version)
    {
        using var stream = new MemoryStream(bytes);
        using var bitmap = new System.Drawing.Bitmap(stream);
        var actual = bitmap.GetPixel(10, 10); var expected = ColorFor(version);
        return Math.Abs(actual.R - expected.R) < 10 && Math.Abs(actual.B - expected.B) < 10;
    }
    public void Dispose() { }
}
