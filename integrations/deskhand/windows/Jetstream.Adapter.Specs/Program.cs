using System.Net;
using System.Text;
using System.Text.Json;
using Deskhand.Windows.App;

if (args.Length == 2) {
    using var client = new JetstreamClient(args[0]);
    using var command = JsonDocument.Parse(args[1]);
    Console.WriteLine((await client.CallAsync(command.RootElement, () => true)).GetRawText());
    return;
}
var root = Path.Combine(Path.GetTempPath(), "jetstream-adapter-" + Guid.NewGuid());
Directory.CreateDirectory(root);
try {
    var file = Path.Combine(root, "descriptor.json");
    var token = new string('x', 40);
    await File.WriteAllTextAsync(file, JsonSerializer.Serialize(new {apiVersion=1, actor="deskhand-host", endpoint="http://127.0.0.1:1234/api/v1", token}));
    var handler = new FixtureHandler();
    using var client = new JetstreamClient(file, handler);
    using var command = JsonDocument.Parse("""{"op":"jetstream_action","projectId":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","requestId":"11111111-1111-4111-8111-111111111111","action":"cancel","jobId":1}""");
    await client.CallAsync(command.RootElement, () => true);
    if (handler.Auth != "Bearer " + token || handler.Project?.Length != 64 || !handler.Body!.Contains("requestId")) throw new Exception("Missing credential or action binding");
    var count = handler.Count;
    try { await client.CallAsync(command.RootElement, () => false); throw new Exception("Guard bypassed"); } catch (InvalidOperationException) { }
    if (handler.Count != count) throw new Exception("Revoked session sent a request");
    using var apply = JsonDocument.Parse(command.RootElement.GetRawText().Replace("cancel", "apply"));
    try { await client.CallAsync(apply.RootElement, () => true); throw new Exception("Apply exposed"); } catch (InvalidOperationException) { }
    if (handler.Count != count) throw new Exception("Unsupported action was sent");
    await File.WriteAllTextAsync(file, JsonSerializer.Serialize(new {apiVersion=1, actor="deskhand-host", endpoint="http://example.com/api/v1", token}));
    try { await client.CallAsync(command.RootElement, () => true); throw new Exception("Nonlocal descriptor accepted"); } catch (InvalidOperationException error) { if(error.Message.Contains(token))throw new Exception("Credential leaked"); }
    if (handler.Count != count) throw new Exception("Remote credential destination used");
    Console.WriteLine("Adapter checks passed: scoped local destination, credential exclusion, project binding, session revocation, unsupported actions.");
} finally { Directory.Delete(root, true); }

sealed class FixtureHandler : HttpMessageHandler {
    public int Count; public string? Auth, Project, Body;
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) {
        Count++; Auth=request.Headers.Authorization?.ToString();Project=request.Headers.GetValues("X-Jetstream-Project").Single();Body=await request.Content!.ReadAsStringAsync(cancellationToken);
        return new HttpResponseMessage(HttpStatusCode.Accepted) {Content=new StringContent("{\"state\":\"uncertain\"}",Encoding.UTF8,"application/json")};
    }
}
