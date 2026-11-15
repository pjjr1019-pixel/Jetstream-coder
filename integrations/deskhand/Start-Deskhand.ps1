param([switch]$PairPhone, [string]$HostExe, [switch]$EmbeddedUI)
$ErrorActionPreference = 'Stop'
$configRoot = Join-Path $env:LOCALAPPDATA 'JetstreamCoder\Deskhand-verification'
New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
$configFile = Join-Path $configRoot 'config.json'
if (-not (Test-Path -LiteralPath $configFile)) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $relayPort = $listener.LocalEndpoint.Port
    $listener.Stop()
    @{ DeviceName = 'Jetstream Coder verification'; RelayPort = $relayPort; RunBuiltInRelay = $true; StartWithWindows = $false; PhoneFriendlyDisplay = $false; PersistentDesktopAccess = $false; Devices = @() } | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding utf8
}
$env:DESKHAND_CONFIG_DIR = $configRoot
if (-not $HostExe) {
    $HostExe = Join-Path $PSScriptRoot 'Deskhand.exe'
    if (-not (Test-Path -LiteralPath $HostExe)) { $HostExe = Join-Path $PSScriptRoot 'Deskhand\Deskhand.exe' }
}
if (-not (Test-Path -LiteralPath $hostExe)) { throw 'Build the combined bundle first: node scripts/package-companion.mjs' }
if ($EmbeddedUI) {
    Start-Process -FilePath $HostExe -ArgumentList @("--jetstream", "--minimized") -WorkingDirectory (Split-Path -Parent $HostExe) -WindowStyle Hidden
} elseif ($PairPhone) {
    Start-Process -FilePath $HostExe -ArgumentList '--pair-phone' -WorkingDirectory (Split-Path -Parent $HostExe) -WindowStyle Normal
} else {
    Start-Process -FilePath $HostExe -WorkingDirectory (Split-Path -Parent $HostExe) -WindowStyle Hidden
}
