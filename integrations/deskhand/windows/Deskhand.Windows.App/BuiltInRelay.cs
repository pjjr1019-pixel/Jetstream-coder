using Deskhand.Relay;
using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.Logging;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.Extensions.DependencyInjection;
using System.Net;
using System.Security.Cryptography.X509Certificates;

namespace Deskhand.Windows.App;

/// <summary>Runs the relay inside the app so no separate server is needed.</summary>
public sealed class BuiltInRelay
{
    private WebApplication? _app;

    public string? Error { get; private set; }
    public bool Running => _app is not null;

    public async Task StartAsync(int port)
    {
        Error = null;
        WebApplication? app = null;
        try
        {
            await StopAsync();
            var builder = WebApplication.CreateBuilder(new WebApplicationOptions { ContentRootPath = AppContext.BaseDirectory });
            builder.Logging.ClearProviders();
            builder.WebHost.UseUrls($"http://0.0.0.0:{port}");
            app = builder.Build();
            app.MapRelay();
            await app.StartAsync();
            _app = app;
            app = null;
        }
        catch (Exception ex)
        {
            // Never fail silently: a dead relay used to start the app
            // "normal" and hand out pairing QR codes for it. Record the
            // failure, log it the same way RelayLink logs relay errors,
            // leave Running false, and dispose the half-built app so a
            // failed start leaks nothing. Callers check Error/Running.
            _app = null;
            Error = $"The built-in relay could not start on port {port}: {ex.GetBaseException().Message}";
            System.Diagnostics.Debug.WriteLine($"Deskhand built-in relay failed to start on port {port}: {ex.GetBaseException().Message}");
            if (app is not null)
            {
                try { await app.DisposeAsync(); } catch { }
            }
        }
    }

    public async Task StopAsync()
    {
        var app = _app;
        _app = null;
        if (app is null) return;
        await app.StopAsync();
        await app.DisposeAsync();
    }
}
