using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.Json;

namespace Deskhand.Windows.App;

// Native embedded-network wrapper; MainForm owns startup, UI and disposal.
internal sealed class EmbeddedWindowsNode : IDisposable
{
    private const string Library = "deskhand_tsnode.dll";
    private readonly SemaphoreSlim _gate = new(1, 1);
    private bool _started, _disposed;
    internal sealed record Status(string State, string? AuthURL, string? IPv4);

    [DllImport(Library, CallingConvention = CallingConvention.Cdecl, EntryPoint = "DeskhandNodeStart")]
    private static extern int NativeStart([MarshalAs(UnmanagedType.LPUTF8Str)] string stateDirectory);
    [DllImport(Library, CallingConvention = CallingConvention.Cdecl, EntryPoint = "DeskhandNodeStatus")]
    private static extern int NativeStatus(byte[] output, int capacity);
    [DllImport(Library, CallingConvention = CallingConvention.Cdecl, EntryPoint = "DeskhandNodeLogin")]
    private static extern int NativeLogin();
    [DllImport(Library, CallingConvention = CallingConvention.Cdecl, EntryPoint = "DeskhandNodeListen")]
    private static extern int NativeListen(int port);
    [DllImport(Library, CallingConvention = CallingConvention.Cdecl, EntryPoint = "DeskhandNodeStop")]
    private static extern int NativeStop();

    internal async Task StartAsync(string stateDirectory)
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (_started) throw new InvalidOperationException("Connection already started.");
            var path = PreparePrivateDirectory(stateDirectory);
            if (await Task.Run(() => NativeStart(path)).ConfigureAwait(false) != 0)
            {
                // B2-A B5: a non-zero start may still have left a partially
                // started native node (state written, node up, later step
                // failed). Stop it best-effort so it cannot leak and so a
                // later retry does not meet a live node. The return code
                // cannot tell "never started" from "partially started",
                // so stopping unconditionally is the safe assumption; the
                // spec asserts native stop is idempotent, and Dispose
                // already calls NativeStop without checking its result.
                try { _ = await Task.Run(NativeStop).ConfigureAwait(false); } catch { }
                throw new InvalidOperationException("Embedded connection could not start.");
            }
            _started = true;
        }
        finally { _gate.Release(); }
    }

    internal async Task<Status> StatusAsync()
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            RequireStarted();
            var bytes = new byte[32768];
            if (await Task.Run(() => NativeStatus(bytes, bytes.Length)).ConfigureAwait(false) != 0) throw new InvalidOperationException("Embedded connection status unavailable.");
            var end = Array.IndexOf(bytes, (byte)0);
            if (end < 0) throw new InvalidOperationException("Invalid embedded connection status.");
            var status = JsonSerializer.Deserialize<Status>(Encoding.UTF8.GetString(bytes, 0, end)) ?? throw new InvalidOperationException("Invalid embedded connection status.");
            if (!string.IsNullOrEmpty(status.AuthURL) && (!Uri.TryCreate(status.AuthURL, UriKind.Absolute, out var uri) || uri.Scheme != "https" || uri.Host != "login.tailscale.com" || !uri.IsDefaultPort || uri.UserInfo.Length != 0))
                throw new InvalidOperationException("Invalid sign-in address.");
            if (!string.IsNullOrEmpty(status.IPv4)) _ = TailscaleAddress.RelayUrl(status.IPv4, 1);
            return status;
        }
        finally { _gate.Release(); }
    }

    internal async Task LoginAsync()
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try { RequireStarted(); if (await Task.Run(NativeLogin).ConfigureAwait(false) != 0) throw new InvalidOperationException("Could not start sign-in."); }
        finally { _gate.Release(); }
    }

    internal async Task ListenAsync(int port)
    {
        if (port is < 1 or > 65535) throw new ArgumentOutOfRangeException(nameof(port));
        await _gate.WaitAsync().ConfigureAwait(false);
        try { RequireStarted(); if (await Task.Run(() => NativeListen(port)).ConfigureAwait(false) != 0) throw new InvalidOperationException("Could not start private relay."); }
        finally { _gate.Release(); }
    }

    private void RequireStarted()
    { ObjectDisposedException.ThrowIf(_disposed, this); if (!_started) throw new InvalidOperationException("Embedded connection is not started."); }

    internal static string PreparePrivateDirectory(string directory)
    {
        if (!Path.IsPathFullyQualified(directory)) throw new ArgumentException("An absolute state directory is required.");
        var path = Path.GetFullPath(directory);
        for (var current = new DirectoryInfo(path); current is not null; current = current.Parent)
            if (current.Exists && (current.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException("Connection state cannot be stored through a link.");
        var user = WindowsIdentity.GetCurrent().User ?? throw new InvalidOperationException("Windows user identity unavailable.");
        var system = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
        var security = new DirectorySecurity();
        security.SetOwner(user);
        security.SetAccessRuleProtection(true, false);
        var inheritance = InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit;
        security.AddAccessRule(new FileSystemAccessRule(user, FileSystemRights.FullControl, inheritance, PropagationFlags.None, AccessControlType.Allow));
        security.AddAccessRule(new FileSystemAccessRule(system, FileSystemRights.FullControl, inheritance, PropagationFlags.None, AccessControlType.Allow));
        var dir = new DirectoryInfo(path);
        // Never take ownership of an existing untrusted identity directory.
        // A compromised state file must not become trusted merely by changing
        // the permissions on its parent.
        if (!dir.Exists) dir.Create(security);
        dir.Refresh();
        VerifyPrivateDirectory(dir, user, system);
        return path;
    }

    private static void VerifyPrivateDirectory(DirectoryInfo root, SecurityIdentifier user, SecurityIdentifier system)
    {
        var pending = new Stack<DirectoryInfo>();
        pending.Push(root);
        var count = 0;
        while (pending.TryPop(out var directory))
        {
            if (++count > 10000 || (directory.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException("Invalid embedded connection state directory.");
            VerifyPrivateAcl(directory.GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner), user, system, directory == root);
            foreach (var child in directory.EnumerateFileSystemInfos())
            {
                if (++count > 10000 || (child.Attributes & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidOperationException("Invalid embedded connection state entry.");
                if (child is DirectoryInfo subdirectory) pending.Push(subdirectory);
                else if (child is FileInfo file) VerifyPrivateAcl(file.GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner), user, system, false);
                else throw new InvalidOperationException("Unknown embedded connection state entry.");
            }
        }
    }

    private static void VerifyPrivateAcl(FileSystemSecurity acl, SecurityIdentifier user, SecurityIdentifier system, bool root)
    {
        if ((root && !acl.AreAccessRulesProtected) || acl.GetOwner(typeof(SecurityIdentifier)) is not SecurityIdentifier owner || !owner.Equals(user))
            throw new InvalidOperationException("Embedded connection state owner or inheritance is not private.");
        foreach (FileSystemAccessRule rule in acl.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            if (rule.AccessControlType == AccessControlType.Allow && !rule.IdentityReference.Equals(user) && !rule.IdentityReference.Equals(system))
                throw new InvalidOperationException("Embedded connection state has unexpected access.");
    }

    public void Dispose()
    {
        // B2-A B6: the old _gate.Wait() blocked the UI thread forever when
        // a native call was stuck (native calls have no timeout). Wait at
        // most 2 s; on timeout return without stopping — the process is
        // exiting and the OS reclaims the native node, and a later Dispose
        // retries. NativeStop is also guarded so Dispose never throws.
        if (!_gate.Wait(TimeSpan.FromSeconds(2))) return;
        try { if (!_disposed) { _disposed = true; if (_started) { try { NativeStop(); } catch { } _started = false; } } }
        finally { _gate.Release(); }
    }
}
