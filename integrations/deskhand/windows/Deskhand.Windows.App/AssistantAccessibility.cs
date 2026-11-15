using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows.Automation;

namespace Deskhand.Windows.App;

internal sealed record AssistantAccessibleItem(string Role, string Name, int Depth, bool Enabled, string? Token = null);
internal sealed record AssistantAccessibilityResult(AssistantAccessibleItem[] Items, bool Truncated, string? Error);
internal sealed record AssistantAccessibleCandidate(string Token, AutomationElement Element, int[] RuntimeId, string Role, string Name, TextWindowActivator.Window Window);
// Cursor is the post-op cursor read-back (move/drag/scroll evidence);
// DragStartCursor is the read-back taken at the drag start. Both are null
// when no read-back was taken or it failed — the evidence then downgrades.
internal sealed record AssistantControlResult(string Status, string? Error, (int X, int Y)? Cursor = null, (int X, int Y)? DragStartCursor = null);

internal sealed class AssistantAccessibilitySession
{
    private readonly object _gate = new();
    private readonly Dictionary<string, (AssistantAccessibleCandidate Candidate, DateTimeOffset Expiry)> _controls = [];
    internal void Replace(IEnumerable<AssistantAccessibleCandidate> candidates)
    {
        lock (_gate)
        {
            _controls.Clear();
            foreach (var candidate in candidates) _controls.Add(candidate.Token, (candidate, DateTimeOffset.UtcNow.AddMinutes(2)));
        }
    }
    internal AssistantAccessibleCandidate? Take(string token)
    {
        lock (_gate)
            return _controls.Remove(token, out var candidate) && candidate.Expiry > DateTimeOffset.UtcNow ? candidate.Candidate : null;
    }
    internal void Clear() { lock (_gate) _controls.Clear(); }
}

/// <summary>Bounded read-only UI Automation inspection. Never reads control values.</summary>
internal static class AssistantAccessibility
{
    private static readonly SemaphoreSlim ProviderGate = new(1, 1);

    internal static async Task<AssistantControlResult> PerformAsync(AssistantAccessibleCandidate candidate, string operation, string? text, Func<bool> authorized, Func<IntPtr>? foreground = null)
    {
        foreground ??= GetForegroundWindow;
        var inspection = await InspectAsync(candidate.Window, authorized);
        if (inspection.Error is not null) return new("rejected", inspection.Error);
        if (!authorized()) return new("cancelled", "authorization_revoked");
        if (!await ProviderGate.WaitAsync(0)) return new("rejected", "accessibility_busy");
        var worker = Task.Run(() =>
        {
            var dispatched = false;
            try
            {
                if (!authorized() || foreground() != candidate.Window.Handle ||
                    !TextWindowActivator.Snapshot().Any(w => w.Handle == candidate.Window.Handle && w.ProcessId == candidate.Window.ProcessId && w.ClassName == candidate.Window.ClassName && w.Title == candidate.Window.Title && w.ProcessStartTicks == candidate.Window.ProcessStartTicks))
                    return new AssistantControlResult("rejected", "window_changed_or_not_foreground");
                var current = candidate.Element.Current;
                if (current.ProcessId != candidate.Window.ProcessId || current.IsPassword || !current.IsEnabled ||
                    current.ControlType.ProgrammaticName != candidate.Role || !candidate.Element.GetRuntimeId().SequenceEqual(candidate.RuntimeId) ||
                    (candidate.Role is not ("ControlType.Edit" or "ControlType.Document") && current.Name != candidate.Name))
                    return new AssistantControlResult("rejected", "control_changed");
                if (!authorized() || foreground() != candidate.Window.Handle) return new AssistantControlResult("cancelled", "authorization_revoked");
                if (operation == "invoke" && candidate.Role == ControlType.Button.ProgrammaticName && candidate.Element.TryGetCurrentPattern(InvokePattern.Pattern, out var invoke))
                {
                    if (!authorized() || foreground() != candidate.Window.Handle) return new AssistantControlResult("cancelled", "authorization_revoked");
                    dispatched = true;
                    ((InvokePattern)invoke).Invoke();
                    // Batch 2 (100.030, H6): Invoke() returning without an
                    // exception proves the invocation was dispatched to the
                    // verified control; that is the completed action. The
                    // app's resulting state is still not verified.
                    return new AssistantControlResult("completed", null);
                }
                if (operation == "type" && (candidate.Role is "ControlType.Edit" or "ControlType.Document") && candidate.Element.TryGetCurrentPattern(ValuePattern.Pattern, out var value))
                {
                    var pattern = (ValuePattern)value;
                    if (pattern.Current.IsReadOnly) return new AssistantControlResult("rejected", "control_read_only");
                    if (!authorized() || foreground() != candidate.Window.Handle) return new AssistantControlResult("cancelled", "authorization_revoked");
                    dispatched = true;
                    pattern.SetValue(text!);
                    // Some UI providers publish the value asynchronously. Poll
                    // only the readback; never repeat the write.
                    for (var attempt = 0; attempt < 10; attempt++)
                    {
                        if (!authorized()) return new AssistantControlResult("uncertain", "authorization_revoked");
                        if (pattern.Current.Value == text) return new AssistantControlResult("completed", null);
                        Thread.Sleep(50);
                    }
                    return new AssistantControlResult("uncertain", "verification_failed");
                }
                if (operation == "key" && text is not null && InputInjector.IsSupportedKey(text))
                {
                    if (!authorized() || foreground() != candidate.Window.Handle) return new AssistantControlResult("cancelled", "authorization_revoked");
                    dispatched = true;
                    candidate.Element.SetFocus();
                    var focusedCorrectly = false;
                    for (var attempt = 0; attempt < 6; attempt++)
                    {
                        if (!authorized() || foreground() != candidate.Window.Handle) return new AssistantControlResult("uncertain", "focus_unconfirmed");
                        var focused = AutomationElement.FocusedElement;
                        if (focused?.Current.IsPassword == true) return new AssistantControlResult("uncertain", "focus_unconfirmed");
                        if (focused is not null && focused.GetRuntimeId().SequenceEqual(candidate.RuntimeId)) { focusedCorrectly = true; break; }
                        Thread.Sleep(40);
                    }
                    if (!focusedCorrectly || !authorized() || foreground() != candidate.Window.Handle) return new AssistantControlResult("uncertain", "focus_unconfirmed");
                    InputInjector.PressSupportedKey(text);
                    // Batch 2 (100.030, H6): PressSupportedKey throws on a
                    // partial send (caught below as uncertain when dispatched),
                    // so a clean return proves the key events were dispatched
                    // to the verified focused control.
                    return new AssistantControlResult("completed", null);
                }
                // G3 (100.030): key hold / release. Same focus gate and
                // supported-key check as "key" above — the check is
                // InputInjector.IsSupportedKey, the single source of truth
                // shared with the protocol parser and InputInjector's
                // Keys/AssistantShortcuts tables (D3 lockstep). Every
                // key_down must be paired with a key_up in the same task;
                // the host releases all held keys on task end, STOP latch,
                // approval expiry, and any error (InputInjector
                // .ReleaseAllHeldKeys, called by the engine). A key_down
                // whose KeyDown throws is caught below as uncertain (the
                // partial-send release inside KeyDown already freed
                // whatever went down), never as a silent hold.
                if ((operation == "key_down" || operation == "key_up") && text is not null && InputInjector.IsSupportedKey(text))
                {
                    if (!authorized() || foreground() != candidate.Window.Handle) return new AssistantControlResult("cancelled", "authorization_revoked");
                    dispatched = true;
                    candidate.Element.SetFocus();
                    var focusedCorrectly = false;
                    for (var attempt = 0; attempt < 6; attempt++)
                    {
                        if (!authorized() || foreground() != candidate.Window.Handle) return new AssistantControlResult("uncertain", "focus_unconfirmed");
                        var focused = AutomationElement.FocusedElement;
                        if (focused?.Current.IsPassword == true) return new AssistantControlResult("uncertain", "focus_unconfirmed");
                        if (focused is not null && focused.GetRuntimeId().SequenceEqual(candidate.RuntimeId)) { focusedCorrectly = true; break; }
                        Thread.Sleep(40);
                    }
                    if (!focusedCorrectly || !authorized() || foreground() != candidate.Window.Handle) return new AssistantControlResult("uncertain", "focus_unconfirmed");
                    if (operation == "key_down") InputInjector.KeyDown(text); else InputInjector.KeyUp(text);
                    return new AssistantControlResult("completed", null);
                }
                return new AssistantControlResult("rejected", "unsupported_control");
            }
            catch (Exception ex) when (ex is ElementNotAvailableException or InvalidOperationException or COMException or ArgumentException or System.ComponentModel.Win32Exception)
            { return new AssistantControlResult(dispatched ? "uncertain" : "rejected", dispatched ? "outcome_unconfirmed" : "control_unavailable"); }
            finally { ProviderGate.Release(); }
        });
        try { return await worker.WaitAsync(TimeSpan.FromSeconds(5)); }
        catch (TimeoutException) { return new("uncertain", "accessibility_timeout"); }
    }

    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();

    internal static async Task<AssistantAccessibilityResult> InspectAsync(TextWindowActivator.Window window, Func<bool>? authorized = null, AssistantAccessibilitySession? session = null)
    {
        // A new inspection invalidates all prior control observations even if
        // the new provider call fails or times out.
        session?.Clear();
        authorized ??= () => true;
        if (!authorized()) return new([], false, "authorization_revoked");
        if (!await ProviderGate.WaitAsync(0)) return new([], false, "accessibility_busy");
        var worker = Task.Run(() =>
        {
            try { return authorized() ? Inspect(window, session is not null) : (new AssistantAccessibilityResult([], false, "authorization_revoked"), Array.Empty<AssistantAccessibleCandidate>()); }
            finally { ProviderGate.Release(); }
        });
        try
        {
            var (result, candidates) = await worker.WaitAsync(TimeSpan.FromSeconds(5));
            if (!authorized()) return new([], false, "authorization_revoked");
            if (result.Error is null) session?.Replace(candidates);
            else session?.Clear();
            return result;
        }
        catch (TimeoutException) { return new([], false, "accessibility_timeout"); }
    }

    private static (AssistantAccessibilityResult, AssistantAccessibleCandidate[]) Inspect(TextWindowActivator.Window window, bool includeActions)
    {
        try
        {
            if (!TextWindowActivator.Snapshot().Any(w => w.Handle == window.Handle && w.ProcessId == window.ProcessId && w.ClassName == window.ClassName && w.Title == window.Title && w.ProcessStartTicks == window.ProcessStartTicks))
                return (new([], false, "stale_or_denied"), []);
            var root = AutomationElement.FromHandle(window.Handle);
            if (root is null || root.Current.IsPassword || root.Current.ProcessId != window.ProcessId || root.Current.NativeWindowHandle != window.Handle.ToInt64())
                return (new([], false, "protected_or_unavailable"), []);
            var items = new List<AssistantAccessibleItem>();
            var candidates = new List<AssistantAccessibleCandidate>();
            var deadline = Stopwatch.StartNew();
            var truncated = false;
            var protectedContent = false;
            var crossProcessContent = false;
            void Visit(AutomationElement parent, int depth)
            {
                if (depth > 16 || items.Count >= 1200 || deadline.Elapsed > TimeSpan.FromSeconds(3)) { truncated = true; return; }
                var walker = TreeWalker.ControlViewWalker;
                for (var child = walker.GetFirstChild(parent); child is not null; child = walker.GetNextSibling(child))
                {
                    if (items.Count >= 1200 || deadline.Elapsed > TimeSpan.FromSeconds(3)) { truncated = true; break; }
                    var current = child.Current;
                    if (current.IsPassword || current.ClassName is "#32770" or "Credential Dialog Xaml Host" ||
                        new[] { "password", "credential", "sign in", "sign-in", "windows security" }.Any(term => (current.Name ?? "").Contains(term, StringComparison.OrdinalIgnoreCase)) ||
                        ((window.ProcessName.Equals("code", StringComparison.OrdinalIgnoreCase) || window.ProcessName.Equals("cursor", StringComparison.OrdinalIgnoreCase)) &&
                         (current.Name ?? "").Contains("terminal", StringComparison.OrdinalIgnoreCase)))
                    { protectedContent = true; return; }
                    // An uninspected cross-process subtree could contain protected
                    // fields. Never treat a partial scan as a safe window.
                    if (current.ProcessId != window.ProcessId && !AssistantWindowPolicy.EligibleChildProcess(current.ProcessId))
                    {
                        crossProcessContent = true; return;
                    }
                    var control = current.ControlType;
                    // Editable/document names can contain their entire value.
                    var name = control == ControlType.Edit || control == ControlType.Document ? "" : (current.Name ?? "");
                    if (name.Length > 160) name = name[..160];
                    string? token = null;
                    if (includeActions && current.ProcessId == window.ProcessId && current.IsEnabled &&
                        ((control == ControlType.Button && child.TryGetCurrentPattern(InvokePattern.Pattern, out _)) ||
                         ((control == ControlType.Edit || control == ControlType.Document) && child.TryGetCurrentPattern(ValuePattern.Pattern, out _))))
                    {
                        token = Guid.NewGuid().ToString("N");
                        candidates.Add(new(token, child, child.GetRuntimeId(), control.ProgrammaticName, name, window));
                    }
                    items.Add(new(control?.ProgrammaticName ?? "unknown", name, depth, current.IsEnabled, token));
                    Visit(child, depth + 1);
                }
            }
            Visit(root, 0);
            if (protectedContent) return (new([], false, "protected_or_unavailable"), []);
            if (crossProcessContent) return (new([], false, "cross_process_unavailable"), []);
            if (truncated) return (new([], true, "accessibility_incomplete"), []);
            // Chromium can omit its renderer tree entirely. The absence of
            // visible password fields then proves nothing about page content.
            if ((window.ProcessName.Equals("msedge", StringComparison.OrdinalIgnoreCase) || window.ProcessName.Equals("chrome", StringComparison.OrdinalIgnoreCase)) &&
                !items.Any(item => item.Role == ControlType.Document.ProgrammaticName))
                return (new([], false, "browser_content_unavailable"), []);
            return (new(items.ToArray(), truncated, null), candidates.ToArray());
        }
        catch (Exception ex) when (ex is ElementNotAvailableException or InvalidOperationException or COMException or ArgumentException)
        { return (new([], false, "accessibility_unavailable"), []); }
    }
}
