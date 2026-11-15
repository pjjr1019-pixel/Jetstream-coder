using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;

namespace Deskhand.Windows.App;

/// <summary>Reachability of the phone-facing relay address, as far as this PC can tell.</summary>
public enum RelayHealthStatus
{
    /// <summary>No probe ran, or the probe cannot prove anything.</summary>
    Unknown,
    /// <summary>Something answers on the saved address (TCP connect for ws://, TLS handshake for wss://).</summary>
    Reachable,
    /// <summary>Nothing answers on the saved address within the timeout.</summary>
    Unreachable,
}

/// <summary>One assessment of the saved phone relay address (<see cref="AppConfig.RelayUrl"/>).</summary>
public sealed record RelayAssessment(
    RelayHealthStatus Health,
    bool IsStaleLanAddress,
    string? SuggestedRelayUrl,
    string Detail);

/// <summary>
/// Health checks for the saved phone relay address.
///
/// Background: <see cref="AppConfig.RelayUrl"/> is the address phones were
/// given at pairing time (it is embedded in the pairing QR). It is cached in
/// %LOCALAPPDATA%\Deskhand\config.json and was never re-checked, so a stale
/// value silently breaks phone-to-PC while the host itself keeps working:
/// when the built-in relay runs, the host connects to itself on loopback and
/// never touches this address. The Muse connection is unaffected too — it
/// uses the Tailscale funnel, not this relay.
///
/// This helper never sends credentials, room IDs, or any WebSocket handshake:
/// the probe is a bare TCP connect (ws://) or TCP + TLS handshake (wss://).
/// It cannot prove a phone can connect — only that something answers on the
/// saved host and port.
///
/// 100.040: the Cloudflare tunnel is removed. The ephemeral-tunnel
/// classification, the /healthz echo check, and the echo-failure tracker
/// are gone with it; the relay address is now a Tailscale or LAN address,
/// and the bare TCP/TLS probe above is the only check.
/// </summary>
public static class RelayHealth
{
    private static readonly TimeSpan ProbeTimeout = TimeSpan.FromSeconds(5);

    /// <summary>
    /// True when the address is a ws:// LAN address (RFC1918 IPv4 literal) that
    /// this PC no longer holds — the classic stale-cache case after a DHCP
    /// change or a move to another network. Staleness is about the IP, not
    /// the port. Loopback and the Tailscale 100.64/10 range are stable by
    /// construction and are never stale.
    /// </summary>
    public static bool IsStaleLanAddress(string relayUrl, IReadOnlyCollection<string> currentLanAddresses)
    {
        if (!Uri.TryCreate(relayUrl, UriKind.Absolute, out var uri)) return false;
        if (!uri.Scheme.Equals("ws", StringComparison.OrdinalIgnoreCase)) return false;
        if (!IPAddress.TryParse(uri.Host, out var ip) || ip.AddressFamily != AddressFamily.InterNetwork) return false;
        if (IPAddress.IsLoopback(ip)) return false;
        var b = ip.GetAddressBytes();
        var isRfc1918 = b[0] == 10
            || (b[0] == 172 && b[1] >= 16 && b[1] <= 31)
            || (b[0] == 192 && b[1] == 168);
        if (!isRfc1918) return false;
        return !currentLanAddresses.Contains(ip.ToString(), StringComparer.Ordinal);
    }

    /// <summary>Offline classification only — no network traffic. Use <see cref="CheckAsync"/> for a probed assessment.</summary>
    public static RelayAssessment Classify(string relayUrl, IReadOnlyCollection<string> currentLanAddresses, int relayPort)
    {
        if (IsStaleLanAddress(relayUrl, currentLanAddresses))
            return new RelayAssessment(RelayHealthStatus.Unreachable, true, AppConfig.DefaultRelayUrl(relayPort),
                $"This phone address is an old LAN address for this PC. Phones paired with it can't connect — pair again (the current address is {AppConfig.DefaultRelayUrl(relayPort)}).");
        if (!Uri.TryCreate(relayUrl, UriKind.Absolute, out var uri) || (uri.Scheme != "ws" && uri.Scheme != "wss"))
            return new RelayAssessment(RelayHealthStatus.Unknown, false, null,
                "The saved relay address is not a ws:// or wss:// address.");
        return new RelayAssessment(RelayHealthStatus.Unknown, false, null,
            "Saved relay address has not been probed yet.");
    }

    /// <summary>
    /// Classify + probe the saved address. Never throws: any failure is
    /// reported as <see cref="RelayHealthStatus.Unreachable"/>.
    /// A stale LAN address is not probed — it is unreachable by definition.
    /// </summary>
    public static async Task<RelayAssessment> CheckAsync(string relayUrl, IReadOnlyCollection<string> currentLanAddresses, int relayPort, CancellationToken cancellationToken = default)
    {
        var classified = Classify(relayUrl, currentLanAddresses, relayPort);
        if (classified.IsStaleLanAddress)
            return classified;
        if (!Uri.TryCreate(relayUrl, UriKind.Absolute, out var uri) || (uri.Scheme != "ws" && uri.Scheme != "wss"))
            return classified;

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(ProbeTimeout);
        try
        {
            using var client = new TcpClient();
            var port = uri.IsDefaultPort ? (uri.Scheme == "wss" ? 443 : 80) : uri.Port;
            await client.ConnectAsync(uri.Host, port, timeout.Token).ConfigureAwait(false);
            if (uri.Scheme == "wss")
            {
                using var ssl = new SslStream(client.GetStream(), leaveInnerStreamOpen: false);
                await ssl.AuthenticateAsClientAsync(new SslClientAuthenticationOptions { TargetHost = uri.Host }, timeout.Token).ConfigureAwait(false);
            }
            return classified with
            {
                Health = RelayHealthStatus.Reachable,
                Detail = "Saved relay address answers from this PC.",
            };
        }
        catch (Exception ex) when (ex is OperationCanceledException or SocketException or IOException or AuthenticationException or InvalidOperationException)
        {
            return classified with
            {
                Health = RelayHealthStatus.Unreachable,
                Detail = "Saved relay address is unreachable from this PC — check the address or pair again.",
            };
        }
    }
}
