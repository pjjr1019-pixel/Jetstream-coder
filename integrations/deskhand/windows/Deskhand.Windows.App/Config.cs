using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text.Json;

namespace Deskhand.Windows.App;

public sealed class PairedDevice
{
    public string Id { get; set; } = Guid.NewGuid().ToString("N");
    public string Name { get; set; } = "Phone";
    /// <summary>Device secret, protected with DPAPI for the current Windows user.</summary>
    public string ProtectedSecret { get; set; } = "";
    public bool Control { get; set; }
    public DateTime PairedUtc { get; set; } = DateTime.UtcNow;

    public byte[] GetSecret() =>
        ProtectedData.Unprotect(Convert.FromBase64String(ProtectedSecret), null, DataProtectionScope.CurrentUser);

    public static string Protect(byte[] secret) =>
        Convert.ToBase64String(ProtectedData.Protect(secret, null, DataProtectionScope.CurrentUser));
}

public sealed class AppConfig
{
    public string DeviceName { get; set; } = Environment.MachineName;
    /// <summary>Address phones use to reach the relay, shown in the pairing QR (ws:// or wss://).</summary>
    public string RelayUrl { get; set; } = "";
    public bool RunBuiltInRelay { get; set; } = true;
    public int RelayPort { get; set; } = 47801;
    public bool Paused { get; set; }
    public bool StartWithWindows { get; set; }
    public bool PhoneFriendlyDisplay { get; set; } = true;
    public bool PersistentDesktopAccess { get; set; } = true;
    public FullPcAccessPreference FullPcAccess { get; set; } = new();
    public bool EmbeddedNetworkEnabled { get; set; }
    public List<PairedDevice> Devices { get; set; } = [];

    private static readonly string Dir = Environment.GetEnvironmentVariable("DESKHAND_CONFIG_DIR") ?? Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Deskhand");
    private static string FilePath => Path.Combine(Dir, "config.json");
    internal static string DataDirectory => Dir;

    public static AppConfig Load()
    {
        try
        {
            if (File.Exists(FilePath))
            {
                var cfg = JsonSerializer.Deserialize<AppConfig>(File.ReadAllText(FilePath));
                if (cfg is not null)
                {
                    if (string.IsNullOrWhiteSpace(cfg.RelayUrl)) cfg.RelayUrl = DefaultRelayUrl(cfg.RelayPort);
                    else if (IsTunnelUrl(cfg.RelayUrl))
                    {
                        // 100.040 — the Cloudflare tunnel is removed: a
                        // stored tunnel URL can never be used again, so
                        // replace it with the LAN default and persist it.
                        // A failed save is swallowed (the in-memory value
                        // is still the LAN default, and the next load
                        // migrates again), never a load failure.
                        cfg.RelayUrl = DefaultRelayUrl(cfg.RelayPort);
                        try { cfg.Save(); }
                        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
                    }
                    cfg.FullPcAccess ??= new();
                    return cfg;
                }
            }
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
        }
        var fresh = new AppConfig { StartWithWindows = true };
        fresh.RelayUrl = DefaultRelayUrl(fresh.RelayPort);
        return fresh;
    }

    public void Save()
    {
        Directory.CreateDirectory(Dir);
        var tmp = FilePath + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        File.Move(tmp, FilePath, true);
    }

    /// <summary>Host connects locally when it runs the relay itself, otherwise to the configured relay.</summary>
    public string HostRelayUrl => RunBuiltInRelay ? $"ws://127.0.0.1:{RelayPort}" : RelayUrl.TrimEnd('/');

    public static string DefaultRelayUrl(int port) => $"ws://{LanAddress()}:{port}";

    /// <summary>True when the address is a Cloudflare tunnel URL (100.040: the tunnel is removed — no stored or paired address may be one).</summary>
    public static bool IsTunnelUrl(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)) return false;
        var host = uri.Host;
        return host.Equals("trycloudflare.com", StringComparison.OrdinalIgnoreCase)
            || host.EndsWith(".trycloudflare.com", StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>Every IPv4 address this PC currently holds (loopback excluded). Passed to <see cref="RelayHealth"/> so a stale-LAN check can tell a moved address from a live one.</summary>
    public static IReadOnlyCollection<string> CurrentLanAddresses()
    {
        var addresses = new List<string>();
        foreach (var nic in NetworkInterface.GetAllNetworkInterfaces())
        {
            if (nic.OperationalStatus != OperationalStatus.Up || nic.NetworkInterfaceType is NetworkInterfaceType.Loopback or NetworkInterfaceType.Tunnel) continue;
            var props = nic.GetIPProperties();
            foreach (var addr in props.UnicastAddresses)
            {
                if (addr.Address.AddressFamily == AddressFamily.InterNetwork && !IPAddress.IsLoopback(addr.Address))
                    addresses.Add(addr.Address.ToString());
            }
        }
        return addresses;
    }

    private static string LanAddress()
    {
        foreach (var nic in NetworkInterface.GetAllNetworkInterfaces())
        {
            if (nic.OperationalStatus != OperationalStatus.Up || nic.NetworkInterfaceType is NetworkInterfaceType.Loopback or NetworkInterfaceType.Tunnel) continue;
            var props = nic.GetIPProperties();
            if (props.GatewayAddresses.Count == 0) continue;
            var addr = props.UnicastAddresses.FirstOrDefault(a => a.Address.AddressFamily == AddressFamily.InterNetwork);
            if (addr is not null) return addr.Address.ToString();
        }
        return IPAddress.Loopback.ToString();
    }
}
