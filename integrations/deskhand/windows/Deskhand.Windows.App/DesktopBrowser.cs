using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using Deskhand.Contracts;
using Microsoft.Win32.SafeHandles;

namespace Deskhand.Windows.App;

internal sealed record AssistantFileRead(string? Text, string? Error);
internal sealed record AssistantFileChunk(string? Text, long? NextOffset, bool EndOfFile, string? Error);
internal sealed record AssistantFileCreate(string? Error, bool Uncertain);
internal sealed record AssistantFileDelete(string? Error, bool Uncertain);
internal sealed record AssistantFileEdit(string? Error, bool Uncertain);
internal sealed record AssistantFileMove(string? Error, bool Uncertain);
internal sealed record AssistantFileCopy(string? Error, bool Uncertain, string? Destination = null);

public interface IDesktopOpener { void Open(string path); }
public interface IForegroundWindowControl { string? ActOnForeground(string action); }
public interface IDesktopLocationOpener { void Reveal(string path); }
public interface IDesktopBackOpener : IDisposable { (string? Error, string? Title) CloseLatest(); }
public sealed class WindowsDesktopOpener : IDesktopOpener, IDesktopBackOpener, IForegroundWindowControl, IDesktopLocationOpener
{
    private readonly Lazy<WindowLaunchHistory> _history = new(() => new WindowLaunchHistory(new NativeLaunchWindows()));
    private readonly CancellationTokenSource _activationLifetime = new();
    private CancellationTokenSource? _activation;
    private CancellationToken BeginActivation()
    {
        var next = CancellationTokenSource.CreateLinkedTokenSource(_activationLifetime.Token);
        var previous = Interlocked.Exchange(ref _activation, next);
        previous?.Cancel();
        previous?.Dispose();
        return next.Token;
    }
    public (string? Error, string? Title) CloseLatest() => _history.IsValueCreated ? _history.Value.CloseLatest() : ("empty", null);
    public void Dispose()
    {
        _activationLifetime.Cancel();
        if (_history.IsValueCreated) _history.Value.Dispose();
    }
    // Shell associations remain in charge of launching. Some editors reuse an existing
    // window and can ignore the requested show state or Windows foreground rules.
    public static ProcessStartInfo CreateStartInfo(string path) => new()
    {
        FileName = DesktopBrowser.ShellPath(path), UseShellExecute = true, Verb = "open", ErrorDialog = true,
        WindowStyle = string.Equals(System.IO.Path.GetExtension(path), ".txt", StringComparison.OrdinalIgnoreCase)
            ? ProcessWindowStyle.Maximized : ProcessWindowStyle.Normal
    };
    public string? ActOnForeground(string action) => ForegroundWindows.Act(action);
    public void Reveal(string path)
    {
        var cancellation = BeginActivation();
        var before = TextWindowActivator.Snapshot();
        var folder = Directory.Exists(path) ? path : Path.GetDirectoryName(path)!;
        var initialized = CoInitializeEx(IntPtr.Zero, 2);
        if (initialized < 0 && initialized != unchecked((int)0x80010106)) Marshal.ThrowExceptionForHR(initialized);
        try
        {
            Marshal.ThrowExceptionForHR(SHParseDisplayName(DesktopBrowser.ShellPath(path), IntPtr.Zero, out var item, 0, out _));
            try
            {
                Marshal.ThrowExceptionForHR(SHOpenFolderAndSelectItems(item, 0, IntPtr.Zero, 0));
                _ = TextWindowActivator.ActivateLaunchWhenReady(folder, before, null, true, false, cancellation);
            }
            finally { Marshal.FreeCoTaskMem(item); }
        }
        finally { if (initialized >= 0) CoUninitialize(); }
    }
    [DllImport("ole32.dll")] private static extern int CoInitializeEx(IntPtr reserved, uint flags);
    [DllImport("ole32.dll")] private static extern void CoUninitialize();
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)] private static extern int SHParseDisplayName(string name, IntPtr bindContext, out IntPtr item, uint attributes, out uint attributesOut);
    [DllImport("shell32.dll")] private static extern int SHOpenFolderAndSelectItems(IntPtr folder, uint count, IntPtr items, uint flags);
    public void Open(string path)
    {
        var launch = _history.Value.Begin(path);
        var cancellation = BeginActivation();
        var text = string.Equals(System.IO.Path.GetExtension(path), ".txt", StringComparison.OrdinalIgnoreCase);
        var previous = TextWindowActivator.Snapshot();
        try
        {
            using var process = Process.Start(CreateStartInfo(path));
            _history.Value.ResolveWhenReady(launch, process?.Id);
            _ = TextWindowActivator.ActivateLaunchWhenReady(launch.Target, previous, process?.Id,
                Directory.Exists(launch.Target), text, cancellation);
        }
        catch { _history.Value.Failed(launch); throw; }
    }
}

/// <summary>One instance per authenticated session. No client paths enter this service.</summary>
public sealed class DesktopBrowser
{
    private const int MaxItems = 1000, MaxTokens = 262144, MaxRequests = 2048;
    private readonly string _root;
    private readonly IDesktopOpener _opener;
    private readonly Dictionary<string, Entry> _tokens = new(StringComparer.Ordinal);
    private readonly Dictionary<(string Path, string Identity, string? Parent), Entry> _entries = [];
    private readonly Dictionary<string, (string Folder, string[] Paths, int Offset)> _cursors = new(StringComparer.Ordinal);
    private readonly Dictionary<Guid, (string Fingerprint, object Result)> _requests = [];
    private readonly object _gate = new();
    private volatile bool _ended;
    private Entry? _rootEntry;
    private readonly string _thisPcToken = Guid.NewGuid().ToString("N");
    private readonly Dictionary<string, (string Root, string Label, string Identity)> _scopes = new(StringComparer.Ordinal);
    private Guid? _lastListId;
    private sealed record Entry(string Token, string Path, string Identity, bool Directory, string? Parent);

    public DesktopBrowser(string? root = null, IDesktopOpener? opener = null)
    {
        _root = System.IO.Path.GetFullPath(root ?? Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory, Environment.SpecialFolderOption.DoNotVerify));
        _opener = opener ?? new WindowsDesktopOpener();
    }

    public void End()
    {
        _ended = true;
        (_opener as IDesktopBackOpener)?.Dispose();
        // Do not block disconnect on a slow redirected Desktop. The worker owns remaining state.
        if (!Monitor.TryEnter(_gate)) return;
        try { _tokens.Clear(); _entries.Clear(); _cursors.Clear(); _requests.Clear(); }
        finally { Monitor.Exit(_gate); }
    }

    public bool SupportsBack => _opener is IDesktopBackOpener;
    public bool SupportsForeground => _opener is IForegroundWindowControl;

    public ForegroundWindowResult Foreground(ForegroundWindowRequest request, Func<bool> authorized)
    {
        lock (_gate)
        {
            ForegroundWindowResult Fail(string error) => new(request.RequestId, error);
            if (_ended || !authorized()) return Fail("denied");
            if (request.RequestId == Guid.Empty || request.Action is not ("minimize" or "close")) return Fail("malformed");
            var fingerprint = "foreground:" + request.Action;
            if (_requests.TryGetValue(request.RequestId, out var old))
                return old.Fingerprint == fingerprint && old.Result is ForegroundWindowResult result ? result : Fail("malformed");
            if (_requests.Count >= MaxRequests) return Fail("busy");
            _requests.Add(request.RequestId, (fingerprint, Fail("unknown")));
            var error = _opener is IForegroundWindowControl controller ? (authorized() ? controller.ActOnForeground(request.Action) : "denied") : "unsupported";
            var answer = new ForegroundWindowResult(request.RequestId, error);
            _requests[request.RequestId] = (fingerprint, answer);
            return answer;
        }
    }


    public DesktopBackResult Back(DesktopBackRequest request, Func<bool> authorized)
    {
        lock (_gate)
        {
            DesktopBackResult Fail(string error) => new(request.RequestId, error, null);
            if (_ended || !authorized()) return Fail("denied");
            if (request.RequestId == Guid.Empty) return Fail("malformed");
            if (_requests.TryGetValue(request.RequestId, out var old))
                return old.Fingerprint == "back" && old.Result is DesktopBackResult result ? result : Fail("malformed");
            if (_requests.Count >= MaxRequests) return Fail("busy");
            _requests.Add(request.RequestId, ("back", Fail("unknown")));
            var answer = _opener is IDesktopBackOpener closer
                ? (authorized() ? closer.CloseLatest() : (Error: "denied", Title: (string?)null))
                : (Error: "unsupported", Title: (string?)null);
            var closed = new DesktopBackResult(request.RequestId, answer.Error, answer.Title);
            _requests[request.RequestId] = ("back", closed);
            return closed;
        }
    }

    public DesktopListResponse List(DesktopListRequest request, Func<bool> authorized)
    {
        lock (_gate)
        {
            var fingerprint = "list:" + request.FolderToken + ":" + request.ThisPc + ":" + request.Cursor + ":" + request.Paged;
            DesktopListResponse Fail(string error) => new(request.RequestId, null, "Desktop", null, [], error);
            if (_ended || !authorized()) return Fail("denied");
            if (!Valid(request.RequestId, request.FolderToken, true) || (request.ThisPc && (request.FolderToken is not null || request.Cursor is not null)) || (request.Cursor is not null && !request.Paged)) return Fail("malformed");
            if (_requests.TryGetValue(request.RequestId, out var old))
                return old.Fingerprint == fingerprint && old.Result is DesktopListResponse list ? list : Fail("malformed");
            if (_requests.Count >= MaxRequests) return Fail("busy");
            DesktopListResponse result;
            try
            {
                if (request.ThisPc || request.FolderToken == _thisPcToken)
                {
                    var roots = new List<DesktopItem>();
                    void AddRoot(string path, string label)
                    {
                        try
                        {
                            using var rootPins = PinRoot(path);
                            var pinned = rootPins.Last;
                            var entry = Register(pinned.Path, pinned.Identity, true, _thisPcToken);
                            _scopes[entry.Token] = (path, label, pinned.Identity);
                            roots.Add(new(entry.Token, label, "folder", true, false, null));
                        }
                        catch (Exception ex) when (SafeFailure(ex)) { roots.Add(new("", label, "unavailable", false, false, Error(ex))); }
                    }
                    AddRoot(_root, "Desktop");
                    foreach (var drive in DriveInfo.GetDrives().OrderBy(d => d.Name, StringComparer.OrdinalIgnoreCase))
                        AddRoot(drive.Name, drive.Name);
                    result = new(request.RequestId, _thisPcToken, "This PC", null, roots, null);
                }
                else
                {
                var requested = request.FolderToken is null ? null : Find(request.FolderToken);
                var scope = requested is not null && _scopes.TryGetValue(requested.Token, out var value) ? value : (_root, "Desktop", "");
                using var pins = PinRoot(scope.Item1);
                var root = pins.Last;
                if (scope.Item3.Length > 0 && root.Identity != scope.Item3) throw new BrowserFailure("stale_item");
                _rootEntry ??= request.FolderToken is null ? Register(root.Path, root.Identity, true, null) : null;
                var folder = requested ?? _rootEntry!;
                if (folder.Parent is null && folder.Identity != root.Identity) throw new BrowserFailure("stale_item");
                if (!folder.Directory) throw new BrowserFailure("unsupported");
                PinEntry(pins, folder);
                string[] allPaths;
                var offset = 0;
                if (request.Cursor is null)
                {
                    _cursors.Clear();
                    allPaths = Directory.EnumerateFileSystemEntries(folder.Path).OrderBy(p => p, StringComparer.OrdinalIgnoreCase).ThenBy(p => p, StringComparer.Ordinal).ToArray();
                }
                else
                {
                    if (!_cursors.TryGetValue(request.Cursor, out var page) || page.Folder != folder.Token) throw new BrowserFailure("stale_item");
                    allPaths = page.Paths;
                    offset = page.Offset;
                }
                if (!request.Paged && allPaths.Length > 2000) throw new BrowserFailure("too_large");
                var paths = allPaths.Skip(offset).Take(request.Paged ? MaxItems : 2000).ToArray();
                if (_tokens.Count + paths.Length + 1 > MaxTokens) throw new BrowserFailure("busy");
                var items = new List<DesktopItem>();
                foreach (var path in paths)
                {
                    if (_ended || !authorized()) throw new BrowserFailure("denied");
                    var name = System.IO.Path.GetFileName(path);
                    try
                    {
                        ValidateName(name);
                        using var child = Pin.Open(path, false);
                        EnsureWithin(child.Path, root.Path);
                        var entry = Register(child.Path, child.Identity, child.Directory, folder.Token);
                        _scopes[entry.Token] = scope;
                        var kind = child.Directory ? "folder" : Kind(path);
                        items.Add(new(entry.Token, name, kind, child.Directory, !child.Directory && kind != "unsupported", kind == "unsupported" ? "unsupported" : null));
                    }
                    catch (Exception ex) when (SafeFailure(ex))
                    {
                        items.Add(new("", name, "unavailable", false, false, Error(ex)));
                    }
                }
                var sorted = items.OrderBy(x => x.CanBrowse ? 0 : 1).ThenBy(x => x.Name, StringComparer.OrdinalIgnoreCase)
                    .ThenBy(x => x.Name, StringComparer.Ordinal).ToArray();
                var relative = System.IO.Path.GetRelativePath(root.Path, folder.Path);
                string? next = null;
                if (offset + paths.Length < allPaths.Length)
                {
                    next = Guid.NewGuid().ToString("N");
                    _cursors[next] = (folder.Token, allPaths, offset + paths.Length);
                }
                result = new(request.RequestId, folder.Token, relative == "." ? scope.Item2 : scope.Item2.TrimEnd('\\') + "\\" + relative,
                    folder.Parent, sorted, null, next);
                }
            }
            catch (Exception ex) when (SafeFailure(ex)) { result = Fail(Error(ex)); }
            // Retain only one full listing; old request IDs remain reserved without retaining large arrays.
            if (_lastListId is { } previous && _requests.TryGetValue(previous, out var cached))
                _requests[previous] = (cached.Fingerprint, new DesktopListResponse(previous, null, "Desktop", null, [], "stale_item"));
            _lastListId = request.RequestId;
            _requests.Add(request.RequestId, (fingerprint, result));
            return result;
        }
    }

    public DesktopOpenResult Open(DesktopOpenRequest request, Func<bool> authorized) => OpenCore(request, authorized, false);

    // Until per-action approvals exist, AI may open folders only. Shell opening
    // a file or shortcut can execute commands or trigger external actions.
    internal DesktopOpenResult OpenAssistantFolder(DesktopOpenRequest request, Func<bool> authorized) => OpenCore(request, authorized, true);

    private DesktopOpenResult OpenCore(DesktopOpenRequest request, Func<bool> authorized, bool directoryOnly)
    {
        lock (_gate)
        {
            DesktopOpenResult Fail(string error) => new(request.RequestId, error);
            if (_ended || !authorized()) return Fail("denied");
            if (!Valid(request.RequestId, request.ItemToken, false) || !request.Confirmed) return Fail("malformed");
            var fingerprint = "open:" + request.ItemToken + ":" + request.RevealLocation + ":" + request.OpenFolder + ":" + directoryOnly;
            if (_requests.TryGetValue(request.RequestId, out var old))
                return old.Fingerprint == fingerprint && old.Result is DesktopOpenResult opened ? opened : Fail("malformed");
            if (_requests.Count >= MaxRequests) return Fail("busy");
            // Reserve before invoking the shell: even failures must never automatically launch again.
            _requests.Add(request.RequestId, (fingerprint, Fail("unknown")));
            DesktopOpenResult result;
            try
            {
                var entry = Find(request.ItemToken);
                if (directoryOnly && (!entry.Directory || request.RevealLocation)) throw new BrowserFailure("approval_required");
                if (!request.RevealLocation && ((entry.Directory && !request.OpenFolder) || (!entry.Directory && Kind(entry.Path) == "unsupported"))) throw new BrowserFailure("unsupported");
                var scope = _scopes.TryGetValue(entry.Token, out var value) ? value : (_root, "Desktop", "");
                using var pins = PinRoot(scope.Item1);
                if (scope.Item3.Length > 0 && pins.Last.Identity != scope.Item3) throw new BrowserFailure("stale_item");
                PinEntry(pins, entry);
                // ShellExecute receives an ordinary absolute path. Verify that Win32 normalization maps
                // that path to precisely the pinned item (including redirected/long paths).
                using var shellItem = Pin.Open(ShellPath(entry.Path), false);
                if (shellItem.Identity != entry.Identity) throw new BrowserFailure("stale_item");
                if (_ended || !authorized()) throw new BrowserFailure("denied");
                if (request.RevealLocation)
                {
                    if (_opener is not IDesktopLocationOpener location) throw new BrowserFailure("unsupported");
                    location.Reveal(entry.Path);
                }
                else _opener.Open(entry.Path);
                result = new(request.RequestId, null);
            }
            catch (Exception ex) when (SafeFailure(ex)) { result = Fail(Error(ex)); }
            _requests[request.RequestId] = (fingerprint, result);
            return result;
        }
    }

    private static bool Valid(Guid id, string? token, bool nullable) => id != Guid.Empty &&
        (token is null ? nullable : Guid.TryParseExact(token, "N", out _));
    private Entry Find(string token) => _tokens.TryGetValue(token, out var entry) ? entry : throw new BrowserFailure("stale_item");

    // Model-facing file access uses only a previously issued token and reads
    // from the same pinned handle whose identity was checked during listing.
    internal AssistantFileRead ReadAssistantText(string token, Func<bool> authorized)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return new(null, "denied");
            try
            {
                if (!Guid.TryParseExact(token, "N", out _)) throw new BrowserFailure("malformed");
                var entry = Find(token);
                if (entry.Directory) throw new BrowserFailure("unsupported");
                var scope = _scopes.TryGetValue(entry.Token, out var value) ? value : (_root, "Desktop", "");
                using var pins = PinRoot(scope.Item1);
                if (scope.Item3.Length > 0 && pins.Last.Identity != scope.Item3) throw new BrowserFailure("stale_item");
                PinEntry(pins, entry);
                var pin = pins.Last;
                const int limit = 65536;
                var size = RandomAccess.GetLength(pin.Handle);
                if (size > limit) throw new BrowserFailure("too_large");
                var bytes = new byte[(int)size];
                var offset = 0;
                while (offset < bytes.Length)
                {
                    if (_ended || !authorized()) throw new BrowserFailure("denied");
                    var read = RandomAccess.Read(pin.Handle, bytes.AsSpan(offset), offset);
                    if (read == 0) throw new BrowserFailure("stale_item");
                    offset += read;
                }
                if (_ended || !authorized()) throw new BrowserFailure("denied");
                var text = new UTF8Encoding(false, true).GetString(bytes).TrimStart('\uFEFF');
                if (text.Contains('\0')) throw new BrowserFailure("unsupported");
                return new(text, null);
            }
            catch (DecoderFallbackException) { return new(null, "unsupported"); }
            catch (Exception ex) when (SafeFailure(ex)) { return new(null, Error(ex)); }
        }
    }

    // Read bounded UTF-8 chunks without losing a split multibyte character.
    // Every request reopens and verifies the previously listed file identity.
    internal AssistantFileChunk ReadAssistantTextChunk(string token, long offset, Func<bool> authorized)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return new(null, null, false, "denied");
            try
            {
                if (!Guid.TryParseExact(token, "N", out _) || offset < 0) throw new BrowserFailure("malformed");
                var entry = Find(token);
                if (entry.Directory) throw new BrowserFailure("unsupported");
                var scope = _scopes.TryGetValue(entry.Token, out var value) ? value : (_root, "Desktop", "");
                using var pins = PinRoot(scope.Item1);
                if (scope.Item3.Length > 0 && pins.Last.Identity != scope.Item3) throw new BrowserFailure("stale_item");
                PinEntry(pins, entry);
                var size = RandomAccess.GetLength(pins.Last.Handle);
                if (offset > size) throw new BrowserFailure("invalid_offset");
                var bytes = new byte[(int)Math.Min(8192, size - offset)];
                var read = 0;
                while (read < bytes.Length)
                {
                    if (_ended || !authorized()) throw new BrowserFailure("denied");
                    var count = RandomAccess.Read(pins.Last.Handle, bytes.AsSpan(read), offset + read);
                    if (count == 0) throw new BrowserFailure("stale_item");
                    read += count;
                }
                if (_ended || !authorized()) throw new BrowserFailure("denied");
                var strict = new UTF8Encoding(false, true);
                var length = bytes.Length;
                string? text = null;
                for (var drop = 0; drop <= 3 && drop <= bytes.Length; drop++)
                {
                    if (drop > 0 && offset + bytes.Length == size) break;
                    try { length = bytes.Length - drop; text = strict.GetString(bytes, 0, length); break; }
                    catch (DecoderFallbackException) { }
                }
                if (text is null || (bytes.Length != 0 && length == 0) || text.Contains('\0')) throw new BrowserFailure("unsupported");
                if (offset == 0) text = text.TrimStart('\uFEFF');
                var next = offset + length;
                return new(text, next < size ? next : null, next == size, null);
            }
            catch (Exception ex) when (SafeFailure(ex)) { return new(null, null, false, Error(ex)); }
        }
    }

    internal (string? Path, string? Error) DescribeAssistantItem(string token, Func<bool> authorized)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return (null, "denied");
            try
            {
                if (!Guid.TryParseExact(token, "N", out _)) throw new BrowserFailure("malformed");
                var entry = Find(token);
                var scope = _scopes.TryGetValue(entry.Token, out var value) ? value : (_root, "Desktop", "");
                using var pins = PinRoot(scope.Item1);
                if (scope.Item3.Length > 0 && pins.Last.Identity != scope.Item3) throw new BrowserFailure("stale_item");
                PinEntry(pins, entry);
                if (_ended || !authorized()) throw new BrowserFailure("denied");
                return (ShellPath(entry.Path), null);
            }
            catch (Exception ex) when (SafeFailure(ex)) { return (null, Error(ex)); }
        }
    }

    internal (string? Path, string? Error) DescribeAssistantFolderForRemoval(string token, Func<bool> authorized)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return (null, "denied");
            try
            {
                if (!Guid.TryParseExact(token, "N", out _)) throw new BrowserFailure("malformed");
                var entry = Find(token);
                if (!entry.Directory || entry.Parent is null || entry.Parent == _thisPcToken) throw new BrowserFailure("unsupported");
                return DescribeAssistantItem(token, authorized);
            }
            catch (Exception ex) when (SafeFailure(ex)) { return (null, Error(ex)); }
        }
    }

    // Windows removes only an empty directory through this disposition; it
    // never recursively deletes contents. Its listed identity is rechecked.
    internal AssistantFileDelete RemoveAssistantEmptyFolder(string token, Func<bool> authorized)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return new("denied", false);
            var submitted = false;
            try
            {
                if (!Guid.TryParseExact(token, "N", out _)) throw new BrowserFailure("malformed");
                var entry = Find(token);
                if (!entry.Directory || entry.Parent is null || entry.Parent == _thisPcToken) throw new BrowserFailure("unsupported");
                var scope = _scopes.TryGetValue(entry.Token, out var value) ? value : (_root, "Desktop", "");
                using (var pins = PinRoot(scope.Item1))
                {
                    if (scope.Item3.Length > 0 && pins.Last.Identity != scope.Item3) throw new BrowserFailure("stale_item");
                    if (entry.Path.Equals(pins.Last.Path, StringComparison.OrdinalIgnoreCase)) throw new BrowserFailure("unsupported");
                    PinEntry(pins, entry, deleteAccess: true);
                    if (_ended || !authorized()) throw new BrowserFailure("denied");
                    var disposition = new FILE_DISPOSITION_INFO { DeleteFile = true };
                    if (!SetFileInformationByHandle(pins.Last.Handle, 4, ref disposition, (uint)Marshal.SizeOf<FILE_DISPOSITION_INFO>()))
                    {
                        var error = Marshal.GetLastWin32Error();
                        if (error == 145) throw new BrowserFailure("folder_not_empty");
                        throw new Win32Exception(error);
                    }
                    submitted = true;
                }
                if (_ended || !authorized()) return new("authorization_revoked", true);
                using var probe = CreateFileW(entry.Path, 0x80000000u, 7, IntPtr.Zero, 3, 0x02000000u | 0x00200000u, IntPtr.Zero);
                if (!probe.IsInvalid) return new("verification_failed", true);
                var verifyError = Marshal.GetLastWin32Error();
                return verifyError is 2 or 3 ? new(null, false) : new("verification_unconfirmed", true);
            }
            catch (Exception ex) when (SafeFailure(ex)) { return new(submitted ? "execution_unconfirmed" : Error(ex), submitted); }
        }
    }

    // Delete only the exact listed file, not a path or a directory tree. The
    // delete disposition is applied to the verified Windows handle itself.
    internal AssistantFileDelete DeleteAssistantFile(string token, Func<bool> authorized)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return new("denied", false);
            var submitted = false;
            try
            {
                if (!Guid.TryParseExact(token, "N", out _)) throw new BrowserFailure("malformed");
                var entry = Find(token);
                if (entry.Directory) throw new BrowserFailure("unsupported");
                var scope = _scopes.TryGetValue(entry.Token, out var value) ? value : (_root, "Desktop", "");
                using (var pins = PinRoot(scope.Item1))
                {
                    if (scope.Item3.Length > 0 && pins.Last.Identity != scope.Item3) throw new BrowserFailure("stale_item");
                    PinEntry(pins, entry, deleteAccess: true);
                    if (_ended || !authorized()) throw new BrowserFailure("denied");
                    var disposition = new FILE_DISPOSITION_INFO { DeleteFile = true };
                    submitted = true;
                    if (!SetFileInformationByHandle(pins.Last.Handle, 4, ref disposition, (uint)Marshal.SizeOf<FILE_DISPOSITION_INFO>()))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                if (_ended || !authorized()) return new("authorization_revoked", true);
                // File.Exists also returns false for access failures, so it is not
                // sufficient evidence that the deletion completed.
                using var probe = CreateFileW(entry.Path, 0x80000000u, 7, IntPtr.Zero, 3, 0x02000000u | 0x00200000u, IntPtr.Zero);
                if (!probe.IsInvalid) return new("verification_failed", true);
                var verifyError = Marshal.GetLastWin32Error();
                return verifyError is 2 or 3 ? new(null, false) : new("verification_unconfirmed", true);
            }
            catch (Exception ex) when (SafeFailure(ex)) { return new(submitted ? "execution_unconfirmed" : Error(ex), submitted); }
        }
    }

    internal AssistantFileEdit ReplaceAssistantText(string token, string text, Func<bool> authorized)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return new("denied", false);
            var writing = false;
            try
            {
                if (!Guid.TryParseExact(token, "N", out _)) throw new BrowserFailure("malformed");
                var bytes = new UTF8Encoding(false, true).GetBytes(text);
                if (bytes.Length > 16000 || text.Contains('\0')) throw new BrowserFailure("too_large");
                var entry = Find(token);
                if (entry.Directory || Path.GetExtension(entry.Path).ToLowerInvariant() is not (".txt" or ".md")) throw new BrowserFailure("unsupported");
                var scope = _scopes.TryGetValue(entry.Token, out var value) ? value : (_root, "Desktop", "");
                using var pins = PinRoot(scope.Item1);
                if (scope.Item3.Length > 0 && pins.Last.Identity != scope.Item3) throw new BrowserFailure("stale_item");
                PinEntry(pins, entry, writeAccess: true);
                if (RandomAccess.GetLength(pins.Last.Handle) > 65536) throw new BrowserFailure("too_large");
                if (_ended || !authorized()) throw new BrowserFailure("denied");
                writing = true;
                RandomAccess.Write(pins.Last.Handle, bytes, 0);
                RandomAccess.SetLength(pins.Last.Handle, bytes.Length);
                RandomAccess.FlushToDisk(pins.Last.Handle);
                var verify = new byte[bytes.Length];
                var offset = 0;
                while (offset < verify.Length)
                {
                    var count = RandomAccess.Read(pins.Last.Handle, verify.AsSpan(offset), offset);
                    if (count == 0) return new("verification_failed", true);
                    offset += count;
                }
                if (_ended || !authorized()) return new("authorization_revoked", true);
                return verify.AsSpan().SequenceEqual(bytes) && RandomAccess.GetLength(pins.Last.Handle) == bytes.Length
                    ? new(null, false) : new("verification_failed", true);
            }
            catch (EncoderFallbackException) { return new("unsupported", writing); }
            catch (Exception ex) when (SafeFailure(ex)) { return new(writing ? "execution_unconfirmed" : Error(ex), writing); }
        }
    }

    // Moves one already-listed file or folder on the same volume. The destination
    // name is a single component and is never replaced.
    internal AssistantFileMove MoveAssistantFile(string token, string folderToken, string name, Func<bool> authorized, bool moveFolder = false)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return new("denied", false);
            var submitted = false;
            try
            {
                if (!Guid.TryParseExact(token, "N", out _) || !Guid.TryParseExact(folderToken, "N", out _)) throw new BrowserFailure("malformed");
                ValidateName(name);
                if (name.Length > 200 || name is "." or ".." || name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) throw new BrowserFailure("unsupported");
                var source = Find(token); var folder = Find(folderToken);
                if (source.Directory != moveFolder || !folder.Directory ||
                    (!moveFolder && !Path.GetExtension(name).Equals(Path.GetExtension(source.Path), StringComparison.OrdinalIgnoreCase)) ||
                    (moveFolder && (source.Parent is null || source.Parent == _thisPcToken))) throw new BrowserFailure("unsupported");
                if (moveFolder && (folder.Path.Equals(source.Path, StringComparison.OrdinalIgnoreCase) ||
                    folder.Path.StartsWith(source.Path.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))) throw new BrowserFailure("invalid_destination");
                var sourceScope = _scopes.TryGetValue(source.Token, out var sourceValue) ? sourceValue : (_root, "Desktop", "");
                var destScope = _scopes.TryGetValue(folder.Token, out var destValue) ? destValue : (_root, "Desktop", "");
                var destination = Path.Combine(folder.Path, name);
                if (File.Exists(destination) || Directory.Exists(destination)) throw new BrowserFailure("already_exists");
                using (var sourcePins = PinRoot(sourceScope.Item1, shareWrite: true))
                using (var destPins = PinRoot(destScope.Item1, shareWrite: true))
                {
                    if ((sourceScope.Item3.Length > 0 && sourcePins.Last.Identity != sourceScope.Item3) ||
                        (destScope.Item3.Length > 0 && destPins.Last.Identity != destScope.Item3)) throw new BrowserFailure("stale_item");
                    if (moveFolder && source.Path.Equals(sourcePins.Last.Path, StringComparison.OrdinalIgnoreCase)) throw new BrowserFailure("unsupported");
                    PinEntry(sourcePins, source, deleteAccess: true);
                    PinEntry(destPins, folder, directoryShareWrite: true);
                    if (sourcePins.Last.Identity.Split(':')[0] != destPins.Last.Identity.Split(':')[0]) throw new BrowserFailure("cross_volume_unsupported");
                    if (_ended || !authorized()) throw new BrowserFailure("denied");
                    var nameBytes = Encoding.Unicode.GetBytes(name);
                    // FILE_RENAME_INFORMATION: BOOLEAN, aligned HANDLE, DWORD, UTF-16 name.
                    // RootDirectory binds the rename to the pinned destination
                    // directory even if its path changes concurrently.
                    var size = checked(24 + nameBytes.Length);
                    var buffer = Marshal.AllocHGlobal(size);
                    try
                    {
                        for (var i = 0; i < size; i++) Marshal.WriteByte(buffer, i, 0);
                        Marshal.WriteIntPtr(buffer, 8, destPins.Last.Handle.DangerousGetHandle());
                        Marshal.WriteInt32(buffer, 16, nameBytes.Length);
                        Marshal.Copy(nameBytes, 0, IntPtr.Add(buffer, 20), nameBytes.Length);
                        submitted = true;
                        var nativeStatus = NtSetInformationFile(sourcePins.Last.Handle, out _, buffer, (uint)size, 10);
                        if (nativeStatus < 0) throw new Win32Exception((int)RtlNtStatusToDosError(nativeStatus));
                    }
                    finally { Marshal.FreeHGlobal(buffer); }
                }
                if (_ended || !authorized()) return new("authorization_revoked", true);
                using var verified = Pin.Open(destination, false);
                if (verified.Identity != source.Identity || verified.Directory != source.Directory) return new("verification_failed", true);
                using var old = CreateFileW(source.Path, 0x80000000u, 7, IntPtr.Zero, 3, 0x02000000u | 0x00200000u, IntPtr.Zero);
                if (!old.IsInvalid) return new("verification_failed", true);
                var error = Marshal.GetLastWin32Error();
                return error is 2 or 3 ? new(null, false) : new("verification_unconfirmed", true);
            }
            catch (Exception ex) when (SafeFailure(ex)) { return new(submitted ? "execution_unconfirmed" : Error(ex), submitted); }
        }
    }

    // Copies from a pinned source to a newly-created destination, verifying the
    // bytes before reporting success. It never deletes or changes the source.
    internal AssistantFileCopy CopyAssistantFile(string token, string folderToken, string name, Func<bool> authorized)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return new("denied", false);
            var created = false;
            string? destination = null;
            try
            {
                if (!Guid.TryParseExact(token, "N", out _) || !Guid.TryParseExact(folderToken, "N", out _)) throw new BrowserFailure("malformed");
                ValidateName(name);
                if (name.Length > 200 || name is "." or ".." || name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) throw new BrowserFailure("unsupported");
                var source = Find(token); var folder = Find(folderToken);
                if (source.Directory || !folder.Directory || !Path.GetExtension(name).Equals(Path.GetExtension(source.Path), StringComparison.OrdinalIgnoreCase)) throw new BrowserFailure("unsupported");
                var sourceScope = _scopes.TryGetValue(source.Token, out var sourceValue) ? sourceValue : (_root, "Desktop", "");
                var destScope = _scopes.TryGetValue(folder.Token, out var destValue) ? destValue : (_root, "Desktop", "");
                using var sourcePins = PinRoot(sourceScope.Item1, shareWrite: true);
                using var destPins = PinRoot(destScope.Item1, shareWrite: true);
                if ((sourceScope.Item3.Length > 0 && sourcePins.Last.Identity != sourceScope.Item3) ||
                    (destScope.Item3.Length > 0 && destPins.Last.Identity != destScope.Item3)) throw new BrowserFailure("stale_item");
                PinEntry(sourcePins, source);
                PinEntry(destPins, folder, directoryShareWrite: true);
                destination = Path.Combine(folder.Path, name);
                if (File.Exists(destination) || Directory.Exists(destination)) throw new BrowserFailure("already_exists");
                if (_ended || !authorized()) throw new BrowserFailure("denied");
                using var output = new FileStream(destination, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.Read, 65536, FileOptions.SequentialScan);
                created = true;
                var size = RandomAccess.GetLength(sourcePins.Last.Handle);
                var buffer = new byte[65536];
                using var sourceHash = System.Security.Cryptography.IncrementalHash.CreateHash(System.Security.Cryptography.HashAlgorithmName.SHA256);
                long position = 0;
                while (position < size)
                {
                    if (_ended || !authorized()) return new("authorization_revoked_partial_copy", true, ShellPath(destination));
                    var count = RandomAccess.Read(sourcePins.Last.Handle, buffer.AsSpan(0, (int)Math.Min(buffer.Length, size - position)), position);
                    if (count == 0) return new("copy_unconfirmed", true, ShellPath(destination));
                    sourceHash.AppendData(buffer, 0, count); output.Write(buffer, 0, count); position += count;
                }
                output.Flush(true);
                if (_ended || !authorized()) return new("authorization_revoked_partial_copy", true, ShellPath(destination));
                output.Position = 0;
                using var destinationHash = System.Security.Cryptography.IncrementalHash.CreateHash(System.Security.Cryptography.HashAlgorithmName.SHA256);
                while (true)
                {
                    if (_ended || !authorized()) return new("authorization_revoked_partial_copy", true, ShellPath(destination));
                    var count = output.Read(buffer, 0, buffer.Length);
                    if (count == 0) break;
                    destinationHash.AppendData(buffer, 0, count);
                }
                if (_ended || !authorized()) return new("authorization_revoked_partial_copy", true, ShellPath(destination));
                return output.Length == size && System.Security.Cryptography.CryptographicOperations.FixedTimeEquals(sourceHash.GetHashAndReset(), destinationHash.GetHashAndReset())
                    ? new(null, false, ShellPath(destination)) : new("verification_failed", true, ShellPath(destination));
            }
            catch (Exception ex) when (SafeFailure(ex)) { return new(created ? "copy_unconfirmed" : Error(ex), created, created && destination is not null ? ShellPath(destination) : null); }
        }
    }

    // Create-only operation: never replace an existing item or follow a path
    // supplied by a model. File names are constrained to ordinary text files.
    internal AssistantFileCreate CreateAssistantText(string folderToken, string name, string text, Func<bool> authorized)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return new("denied", false);
            var attempted = false;
            try
            {
                if (!Guid.TryParseExact(folderToken, "N", out _)) throw new BrowserFailure("malformed");
                if (name.Length > 200 || name is "." or ".." || name.IndexOfAny(System.IO.Path.GetInvalidFileNameChars()) >= 0 ||
                    Path.GetExtension(name).ToLowerInvariant() is not (".txt" or ".md")) throw new BrowserFailure("unsupported");
                ValidateName(name);
                var bytes = new UTF8Encoding(false, true).GetBytes(text);
                if (bytes.Length > 32768) throw new BrowserFailure("too_large");
                var entry = Find(folderToken);
                if (!entry.Directory) throw new BrowserFailure("unsupported");
                var scope = _scopes.TryGetValue(entry.Token, out var value) ? value : (_root, "Desktop", "");
                using var pins = PinRoot(scope.Item1);
                if (scope.Item3.Length > 0 && pins.Last.Identity != scope.Item3) throw new BrowserFailure("stale_item");
                PinEntry(pins, entry);
                var destination = System.IO.Path.Combine(entry.Path, name);
                if (File.Exists(destination) || Directory.Exists(destination)) throw new BrowserFailure("already_exists");
                if (_ended || !authorized()) throw new BrowserFailure("denied");
                attempted = true;
                using var stream = new FileStream(destination, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.Read, 4096, FileOptions.WriteThrough);
                stream.Write(bytes); stream.Flush(true);
                if (_ended || !authorized()) return new("authorization_revoked", true);
                stream.Position = 0;
                var actual = new byte[bytes.Length];
                stream.ReadExactly(actual);
                if (!actual.AsSpan().SequenceEqual(bytes) || stream.Length != bytes.Length) return new("verification_failed", true);
                return new(null, false);
            }
            catch (EncoderFallbackException) { return new("unsupported", attempted); }
            catch (Exception ex) when (SafeFailure(ex)) { return new(attempted ? "execution_unconfirmed" : Error(ex), attempted); }
        }
    }

    // Create exactly one new directory under the pinned, previously listed parent.
    internal AssistantFileCreate CreateAssistantFolder(string folderToken, string name, Func<bool> authorized)
    {
        lock (_gate)
        {
            if (_ended || !authorized()) return new("denied", false);
            var created = false;
            try
            {
                if (!Guid.TryParseExact(folderToken, "N", out _)) throw new BrowserFailure("malformed");
                if (string.IsNullOrWhiteSpace(name) || name.Length > 200 || name is "." or ".." || name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) throw new BrowserFailure("unsupported");
                ValidateName(name);
                var entry = Find(folderToken);
                if (!entry.Directory) throw new BrowserFailure("unsupported");
                var scope = _scopes.TryGetValue(entry.Token, out var value) ? value : (_root, "Desktop", "");
                using var pins = PinRoot(scope.Item1);
                if (scope.Item3.Length > 0 && pins.Last.Identity != scope.Item3) throw new BrowserFailure("stale_item");
                PinEntry(pins, entry);
                var destination = Path.Combine(entry.Path, name);
                if (_ended || !authorized()) throw new BrowserFailure("denied");
                if (!CreateDirectoryW(destination, IntPtr.Zero))
                {
                    var code = Marshal.GetLastWin32Error();
                    if (code is 80 or 183) throw new BrowserFailure("already_exists");
                    throw new Win32Exception(code);
                }
                created = true;
                using var child = Pin.Open(destination, false);
                if (!child.Directory) return new("verification_failed", true);
                if (_ended || !authorized()) return new("authorization_revoked", true);
                return new(null, false);
            }
            catch (Exception ex) when (SafeFailure(ex)) { return new(created ? "execution_unconfirmed" : Error(ex), created); }
        }
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateDirectoryW(string path, IntPtr securityAttributes);
    private Entry Register(string path, string identity, bool directory, string? parent)
    {
        var key = (path, identity, parent);
        if (_entries.TryGetValue(key, out var existing)) return existing;
        if (_tokens.Count >= MaxTokens) throw new BrowserFailure("busy");
        var entry = new Entry(Guid.NewGuid().ToString("N"), path, identity, directory, parent);
        _tokens.Add(entry.Token, entry);
        _entries.Add(key, entry);
        return entry;
    }
    private Pins PinRoot(string? rootPath = null, bool shareWrite = false)
    {
        // Resolve the configured known folder once per operation, then pin its physical path.
        using var resolved = Pin.Open(rootPath ?? _root, true);
        var pins = new Pins();
        try
        {
            var volume = System.IO.Path.GetPathRoot(resolved.Path)!;
            var current = volume;
            pins.Add(Pin.Open(current, false, shareWrite: shareWrite));
            foreach (var part in resolved.Path[volume.Length..].Split('\\', StringSplitOptions.RemoveEmptyEntries))
            {
                current = System.IO.Path.Combine(current, part);
                pins.Add(Pin.Open(current, false, shareWrite: shareWrite));
            }
            if (pins.Last.Identity != resolved.Identity || !pins.Last.Directory) throw new BrowserFailure("stale_item");
            return pins;
        }
        catch { pins.Dispose(); throw; }
    }
    private static void EnsureWithin(string path, string root)
    {
        if (!path.Equals(root, StringComparison.OrdinalIgnoreCase) &&
            !path.StartsWith(root.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
            throw new BrowserFailure("denied");
    }
    private static void PinEntry(Pins pins, Entry entry, bool deleteAccess = false, bool writeAccess = false, bool directoryShareWrite = false)
    {
        var root = pins.Last.Path;
        EnsureWithin(entry.Path, root);
        var current = root;
        var relative = System.IO.Path.GetRelativePath(root, entry.Path);
        if (relative != ".") foreach (var part in relative.Split('\\'))
        {
            if (part is ".." or "." || part.Contains(':')) throw new BrowserFailure("denied");
            current = System.IO.Path.Combine(current, part);
            var last = current.Equals(entry.Path, StringComparison.OrdinalIgnoreCase);
            var pin = Pin.Open(current, false, deleteAccess && last, writeAccess && last, directoryShareWrite && last);
            pins.Add(pin);
            EnsureWithin(pin.Path, root);
        }
        if (pins.Last.Identity != entry.Identity || pins.Last.Directory != entry.Directory)
            throw new BrowserFailure("stale_item");
    }
    internal static string ShellPath(string path) => path.StartsWith(@"\\?\UNC\") ? @"\\" + path[8..] : path.StartsWith(@"\\?\") ? path[4..] : path;
    private static void ValidateName(string name)
    {
        var stem = name.Split('.')[0].ToUpperInvariant();
        if (name.Length == 0 || name.EndsWith('.') || name.EndsWith(' ') || name.Any(char.IsControl) || name.Contains(':') ||
            stem is "CON" or "PRN" or "AUX" or "NUL" ||
            (stem.Length == 4 && (stem.StartsWith("COM") || stem.StartsWith("LPT")) && char.IsDigit(stem[3])) )
            throw new BrowserFailure("unsupported");
    }
    private static string Kind(string path) => System.IO.Path.GetExtension(path).ToLowerInvariant() switch
    {
        ".lnk" or ".url" => "shortcut",
        ".scf" or ".search-ms" or ".library-ms" or ".appref-ms" or ".website" or ".settingcontent-ms" => "unsupported",
        ".exe" or ".com" or ".bat" or ".cmd" or ".msi" or ".ps1" => "application",
        _ => "file"
    };
    private sealed class BrowserFailure(string code) : Exception { public string Code { get; } = code; }
    private static bool SafeFailure(Exception ex) => ex is BrowserFailure or IOException or UnauthorizedAccessException
        or Win32Exception or COMException or ArgumentException or NotSupportedException;
    private static string Error(Exception ex) => ex switch
    {
        BrowserFailure b => b.Code,
        FileNotFoundException or DirectoryNotFoundException => "missing",
        UnauthorizedAccessException => "inaccessible",
        Win32Exception w when w.NativeErrorCode is 2 or 3 => "missing",
        Win32Exception w when w.NativeErrorCode is 5 or 32 => "inaccessible",
        Win32Exception w when w.NativeErrorCode is 1155 or 31 => "unsupported",
        _ => "inaccessible"
    };
    private sealed class Pins : IDisposable
    {
        private readonly List<Pin> _pins = [];
        public Pin Last => _pins[^1];
        public void Add(Pin pin) => _pins.Add(pin);
        public void Dispose() { for (var i = _pins.Count - 1; i >= 0; i--) _pins[i].Dispose(); }
    }
    private sealed class Pin(SafeFileHandle handle, string path, FileInfo info) : IDisposable
    {
        public SafeFileHandle Handle => handle;
        public string Path { get; } = path.Length == System.IO.Path.GetPathRoot(path)?.Length ? path : path.TrimEnd('\\');
        public bool Directory => (info.Attributes & 16) != 0;
        // Directories change timestamps while children change; only files include content metadata.
        public string Identity => $"{info.Volume}:{info.IndexHigh}:{info.IndexLow}:{info.CreationHigh}:{info.CreationLow}:" +
            (Directory ? "dir" : $"{info.WriteHigh}:{info.WriteLow}:{info.SizeHigh}:{info.SizeLow}");
        public void Dispose() => handle.Dispose();
        public static Pin Open(string path, bool follow, bool deleteAccess = false, bool writeAccess = false, bool shareWrite = false)
        {
            // No write/delete sharing: pin each ancestor and item until the shell call returns.
            var h = CreateFileW(path, 0x80000000u | (deleteAccess ? 0x00010000u : 0u) | (writeAccess ? 0x40000000u : 0u), shareWrite ? 3u : 1u, IntPtr.Zero, 3, 0x02000000u | (follow ? 0u : 0x00200000u), IntPtr.Zero);
            if (h.IsInvalid) { h.Dispose(); throw new Win32Exception(Marshal.GetLastWin32Error()); }
            try
            {
                if (!GetFileInformationByHandle(h, out var info)) throw new Win32Exception(Marshal.GetLastWin32Error());
                if (!follow && (info.Attributes & 0x400) != 0) throw new BrowserFailure("denied");
                var buffer = new StringBuilder(32768);
                var n = GetFinalPathNameByHandleW(h, buffer, buffer.Capacity, 0);
                if (n == 0 || n >= buffer.Capacity) throw new BrowserFailure("unsupported");
                return new Pin(h, buffer.ToString(), info);
            }
            catch { h.Dispose(); throw; }
        }
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct FileInfo
    {
        public uint Attributes, CreationLow, CreationHigh, AccessLow, AccessHigh, WriteLow, WriteHigh,
            Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct FILE_DISPOSITION_INFO { [MarshalAs(UnmanagedType.U1)] public bool DeleteFile; }
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetFileInformationByHandle(SafeFileHandle handle, int infoClass, ref FILE_DISPOSITION_INFO info, uint bufferSize);
    [StructLayout(LayoutKind.Sequential)]
    private struct IO_STATUS_BLOCK { public IntPtr Status; public UIntPtr Information; }
    [DllImport("ntdll.dll")]
    private static extern int NtSetInformationFile(SafeFileHandle handle, out IO_STATUS_BLOCK status, IntPtr info, uint length, int infoClass);
    [DllImport("ntdll.dll")]
    private static extern uint RtlNtStatusToDosError(int status);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInfo info);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder path, int length, uint flags);
}
