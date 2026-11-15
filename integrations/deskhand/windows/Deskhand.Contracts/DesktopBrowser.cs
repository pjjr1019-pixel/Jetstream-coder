using System.Text.Json;
using System.Text.Json.Serialization;

namespace Deskhand.Contracts;

public sealed record DesktopListRequest(Guid RequestId, string? FolderToken, bool ThisPc = false, string? Cursor = null, bool Paged = false);
public sealed record DesktopOpenRequest(Guid RequestId, string ItemToken, bool Confirmed, bool RevealLocation = false, bool OpenFolder = false);
public sealed record DesktopItem(string Token, string Name, string Kind, bool CanBrowse, bool CanOpen, string? Error);
public sealed record DesktopListResponse(Guid RequestId, string? FolderToken, string Path, string? ParentToken,
    IReadOnlyList<DesktopItem> Items, string? Error, string? NextCursor = null);
public sealed record DesktopOpenResult(Guid RequestId, string? Error);
public sealed record ForegroundWindowRequest(Guid RequestId, string Action);
public sealed record ForegroundWindowResult(Guid RequestId, string? Error);
public sealed record DesktopBackRequest(Guid RequestId);
public sealed record DesktopBackResult(Guid RequestId, string? Error, string? WindowTitle);

public static class DesktopWire
{
    public static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        PropertyNameCaseInsensitive = false,
        MaxDepth = 8
    };
}
