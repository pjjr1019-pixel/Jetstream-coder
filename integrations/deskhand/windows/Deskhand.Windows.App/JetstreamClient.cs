using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace Deskhand.Windows.App;

// Uses the existing encrypted Deskhand ChatRequest/ChatResult channel. The local
// bearer is read here and is never included in phone responses or errors.
public sealed class JetstreamClient : IDisposable
{
    private readonly string _descriptor;
    private readonly HttpClient _http;
    public JetstreamClient(string? descriptor = null, HttpMessageHandler? handler = null)
    {
        _descriptor = descriptor ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "jetstream-coder", "deskhand-job-api.json");
        _http = new HttpClient(handler ?? new HttpClientHandler { AllowAutoRedirect = false, UseProxy = false }) { Timeout = TimeSpan.FromSeconds(12) };
    }

    public async Task<JsonElement> CallAsync(JsonElement command, Func<bool> allowed)
    {
        try
        {
            using var descriptor = JsonDocument.Parse(await File.ReadAllTextAsync(_descriptor));
            var d = descriptor.RootElement;
            var endpoint = new Uri(d.GetProperty("endpoint").GetString()!);
            if (d.GetProperty("apiVersion").GetInt32() != 1 || d.GetProperty("actor").GetString() != "deskhand-host" ||
                endpoint.Scheme != "http" || endpoint.Host != "127.0.0.1" || endpoint.AbsolutePath != "/api/v1" || endpoint.Query != "" || endpoint.Fragment != "" || endpoint.UserInfo != "")
                throw new InvalidOperationException();
            var token = d.GetProperty("token").GetString();
            if (string.IsNullOrWhiteSpace(token) || token.Length < 32) throw new InvalidOperationException();
            var op = command.GetProperty("op").GetString();
            var route = op switch
            {
                "jetstream_status" => "",
                "jetstream_jobs" => "/jobs",
                "jetstream_job" => "/jobs/" + Positive(command, "jobId"),
                "jetstream_outcome" => "/actions/" + Uuid(command, "requestId"),
                "jetstream_action" => "/actions",
                _ => throw new InvalidOperationException()
            };
            using var request = new HttpRequestMessage(op == "jetstream_action" ? HttpMethod.Post : HttpMethod.Get, endpoint + route);
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
            if (op is "jetstream_jobs" or "jetstream_job")
            {
                var readProject = command.GetProperty("projectId").GetString();
                if (readProject is null || readProject.Length != 64 || !readProject.All(Uri.IsHexDigit)) throw new InvalidOperationException();
                request.Headers.Add("X-Jetstream-Project", readProject);
            }
            if (op == "jetstream_action")
            {
                var project = command.GetProperty("projectId").GetString();
                if (project is null || project.Length != 64 || !project.All(Uri.IsHexDigit)) throw new InvalidOperationException();
                request.Headers.Add("X-Jetstream-Project", project);
                var action = command.GetProperty("action").GetString();
                var body = new Dictionary<string, object?> { ["requestId"] = Uuid(command, "requestId"), ["action"] = action };
                switch (action)
                {
                    case "submit":
                        var text = command.GetProperty("request").GetString();
                        if (string.IsNullOrWhiteSpace(text) || text.Length > 10000) throw new InvalidOperationException();
                        body["request"] = text; break;
                    case "cancel": body["jobId"] = Positive(command, "jobId"); break;
                    case "reject": body["candidateId"] = Positive(command, "candidateId"); break;
                    case "pause": case "resume": break;
                    default: throw new InvalidOperationException();
                }
                request.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
            }
            if (!allowed()) throw new InvalidOperationException();
            using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead);
            if (!allowed()) throw new InvalidOperationException();
            if ((int)response.StatusCode is >= 300 and < 400) throw new InvalidOperationException();
            using var input = await response.Content.ReadAsStreamAsync();
            using var output = new MemoryStream();
            var buffer = new byte[8192]; int count;
            while ((count = await input.ReadAsync(buffer)) > 0)
            {
                if (output.Length + count > 192 * 1024) throw new InvalidOperationException();
                output.Write(buffer, 0, count);
            }
            if (!allowed()) throw new InvalidOperationException();
            using var json = JsonDocument.Parse(output.ToArray());
            // The API response is bounded; never return descriptor or HTTP headers.
            if (!response.IsSuccessStatusCode) throw new InvalidOperationException();
            return json.RootElement.Clone();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or HttpRequestException or TaskCanceledException or JsonException or KeyNotFoundException or InvalidOperationException or UriFormatException or FormatException)
        { throw new InvalidOperationException("Jetstream is unavailable, the request was refused, or its outcome is unknown. Refresh status and check the existing request ID before retrying."); }
    }
    private static long Positive(JsonElement e, string key) { var n = e.GetProperty(key).GetInt64(); if (n < 1 || n > 9007199254740991) throw new InvalidOperationException(); return n; }
    private static string Uuid(JsonElement e, string key) { if (!Guid.TryParseExact(e.GetProperty(key).GetString(), "D", out var id)) throw new InvalidOperationException(); return id.ToString(); }
    public void Dispose() => _http.Dispose();
}
