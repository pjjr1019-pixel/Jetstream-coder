using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace Deskhand.Windows.App;

internal sealed class JettProtocolException(string error) : Exception(error)
{
    public string Error { get; } = error;
}

internal sealed record JettActionRequest(Guid ActionId, string Name, string? ItemToken);
internal sealed record JettReplyRequest(Guid ReplyId, Guid PromptId, DateTimeOffset CreatedAt, string Text,
    IReadOnlyList<JettActionRequest> Actions);
internal sealed record JettConversationReply(
    [property: JsonPropertyName("reply_id")] Guid ReplyId,
    [property: JsonPropertyName("prompt_id")] Guid PromptId,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("created_at")] string CreatedAt,
    [property: JsonPropertyName("error")] string? Error);
internal sealed record JettConversationAction(
    [property: JsonPropertyName("action_id")] Guid ActionId,
    [property: JsonPropertyName("reply_id")] Guid ReplyId,
    [property: JsonPropertyName("prompt_id")] Guid PromptId,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("error")] string? Error);

internal static partial class JettProtocol
{
    internal const int MaxRecordBytes = 64 * 1024;
    internal const string Protocol = "deskhand.jett/1";

    [GeneratedRegex("^[0-9a-f]{32}$")]
    private static partial Regex TokenPattern();

    public static string DecodeUtf8(byte[] payload)
    {
        if (payload.Length > MaxRecordBytes) throw new JettProtocolException("record_too_large");
        try { return new UTF8Encoding(false, true).GetString(payload); }
        catch (DecoderFallbackException) { throw new JettProtocolException("invalid_utf8"); }
    }

    public static JsonDocument ParseStrict(byte[] payload)
    {
        var json = DecodeUtf8(payload);
        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(json, new JsonDocumentOptions { AllowTrailingCommas = false, CommentHandling = JsonCommentHandling.Disallow, MaxDepth = 12 });
        }
        catch (JsonException)
        {
            throw new JettProtocolException("invalid_schema");
        }
        try
        {
            RejectDuplicateProperties(document.RootElement);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
                throw new JettProtocolException("invalid_schema");
            return document;
        }
        catch
        {
            document.Dispose();
            throw;
        }
    }

    public static JettReplyRequest ParseReply(byte[] payload)
    {
        using var document = ParseStrict(payload);
        var root = document.RootElement;
        ExactFields(root, "protocol", "source", "reply_id", "correlated_prompt_id", "created_at", "text", "actions");
        CheckProtocol(root);
        var replyId = UuidV4(root, "reply_id");
        var promptId = UuidV4(root, "correlated_prompt_id");
        var createdAtText = String(root, "created_at");
        if (!DateTimeOffset.TryParseExact(createdAtText,
                ["yyyy-MM-dd'T'HH:mm:ss'Z'", "yyyy-MM-dd'T'HH:mm:ss.FFFFFFF'Z'"],
                CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var createdAt) ||
            createdAt.Offset != TimeSpan.Zero)
            throw new JettProtocolException("invalid_schema");
        var now = DateTimeOffset.UtcNow;
        if (createdAt < now.AddMinutes(-10) || createdAt > now.AddMinutes(2))
            throw new JettProtocolException("stale_prompt");

        var text = String(root, "text");
        try { _ = new UTF8Encoding(false, true).GetByteCount(text); }
        catch (EncoderFallbackException) { throw new JettProtocolException("invalid_schema"); }
        if (text.Length == 0 || text.EnumerateRunes().Take(24_001).Count() > 24_000)
            throw new JettProtocolException("invalid_schema");
        var actionArray = root.GetProperty("actions");
        if (actionArray.ValueKind != JsonValueKind.Array || actionArray.GetArrayLength() > 4)
            throw new JettProtocolException("invalid_schema");
        var actions = new List<JettActionRequest>();
        var ids = new HashSet<Guid>();
        foreach (var action in actionArray.EnumerateArray())
        {
            if (action.ValueKind != JsonValueKind.Object) throw new JettProtocolException("invalid_schema");
            ExactFields(action, "action_id", "name", "arguments");
            var actionId = UuidV4(action, "action_id");
            if (!ids.Add(actionId)) throw new JettProtocolException("duplicate_action_id");
            var name = String(action, "name");
            var arguments = action.GetProperty("arguments");
            if (arguments.ValueKind != JsonValueKind.Object) throw new JettProtocolException("invalid_schema");
            string? token;
            switch (name)
            {
                case "desktop.list":
                    ExactFields(arguments, "folder_token");
                    token = OptionalToken(arguments, "folder_token");
                    break;
                case "desktop.open":
                    ExactFields(arguments, "item_token");
                    token = String(arguments, "item_token");
                    if (!TokenPattern().IsMatch(token)) throw new JettProtocolException("invalid_schema");
                    break;
                default:
                    throw new JettProtocolException("unsupported_action");
            }
            actions.Add(new(actionId, name, token));
        }
        return new(replyId, promptId, createdAt, text, actions);
    }

    private static void CheckProtocol(JsonElement root)
    {
        if (String(root, "protocol") != Protocol) throw new JettProtocolException("unsupported_protocol");
        if (String(root, "source") != "jett") throw new JettProtocolException("wrong_source");
    }

    private static Guid UuidV4(JsonElement root, string name)
    {
        var value = String(root, name);
        if (!Guid.TryParseExact(value, "D", out var id) || id == Guid.Empty || id.ToString("D") != value
            || value[14] != '4' || value[19] is not ('8' or '9' or 'a' or 'b'))
            throw new JettProtocolException("invalid_schema");
        return id;
    }

    private static string String(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.String)
            throw new JettProtocolException("invalid_schema");
        return value.GetString()!;
    }

    private static string? OptionalToken(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out var value)) throw new JettProtocolException("invalid_schema");
        if (value.ValueKind == JsonValueKind.Null) return null;
        if (value.ValueKind != JsonValueKind.String || !TokenPattern().IsMatch(value.GetString()!))
            throw new JettProtocolException("invalid_schema");
        return value.GetString();
    }

    private static void ExactFields(JsonElement element, params string[] expected)
    {
        if (element.ValueKind != JsonValueKind.Object) throw new JettProtocolException("invalid_schema");
        var names = expected.ToHashSet(StringComparer.Ordinal);
        var count = 0;
        foreach (var property in element.EnumerateObject())
        {
            if (!names.Contains(property.Name)) throw new JettProtocolException("invalid_schema");
            count++;
        }
        if (count != expected.Length) throw new JettProtocolException("invalid_schema");
    }

    private static void RejectDuplicateProperties(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var property in element.EnumerateObject())
            {
                if (!names.Add(property.Name)) throw new JettProtocolException("invalid_schema");
                RejectDuplicateProperties(property.Value);
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in element.EnumerateArray()) RejectDuplicateProperties(item);
        }
    }
}
