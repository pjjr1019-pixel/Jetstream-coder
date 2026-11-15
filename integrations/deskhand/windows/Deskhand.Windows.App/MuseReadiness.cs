using System.Text.Json;
namespace Deskhand.Windows.App;

internal static class MuseReadiness
{
    internal static (bool Ready, string Detail) Inspect(JsonElement status)
    {
        static bool IsTrue(JsonElement value, string name) => value.ValueKind == JsonValueKind.Object && value.TryGetProperty(name, out var item) && item.ValueKind == JsonValueKind.True;
        var bridge = status.TryGetProperty("bridge", out var b) ? b : default;
        var endpoint = status.TryGetProperty("https_connection", out var e) ? e : default;
        var hostOnly = IsTrue(bridge, "shared_actions_host_only");
        if (!IsTrue(bridge, "enabled")) return (false, "Muse connection stopped");
        if (!IsTrue(bridge, "https_session_available")) return (false, "waiting for HTTPS session");
        // Native action admission belongs to HostEngine in this supported mode.
        // Enabling a second HTTPS action route would bypass that ownership.
        if (!hostOnly && !IsTrue(bridge, "https_actions_available")) return (false, "waiting for Muse actions");
        if (!hostOnly && !IsTrue(bridge, "connected")) return (false, "waiting for Muse connection");
        if (!IsTrue(endpoint, "ready")) return (false, "waiting for Muse HTTPS endpoint");
        if (!IsTrue(status, "cloud_ready")) return (false, "waiting for Muse sign-in / correct chat");
        if (!IsTrue(status, "delivery_ready")) return (false, "waiting for Muse delivery");
        return (true, hostOnly ? "ready · host-only actions" : "ready");
    }
}
