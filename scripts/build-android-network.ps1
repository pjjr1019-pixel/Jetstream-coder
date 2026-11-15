param(
 [Parameter(Mandatory=$true)][string]$ToolchainRoot,
 [Parameter(Mandatory=$true)][string]$AndroidSdk,
 [string]$NdkVersion='27.2.12479018'
)
$ErrorActionPreference='Stop'
$repoRoot=Split-Path -Parent $PSScriptRoot
$cacheRoot=Join-Path $repoRoot '.cache'
New-Item -ItemType Directory -Path (Join-Path $cacheRoot 'gopath\pkg\gomobile') -Force | Out-Null
$env:GOROOT=Join-Path $ToolchainRoot 'go'
$env:PATH="$ToolchainRoot\go\bin;$ToolchainRoot\bin;$env:PATH"
$env:GOPATH=Join-Path $cacheRoot 'gopath'
$env:GOMODCACHE=Join-Path $ToolchainRoot 'gopath\pkg\mod'
$env:GOCACHE=Join-Path $cacheRoot 'go-build'
$env:GOPROXY='off'
$env:GOSUMDB='off'
$env:GOTOOLCHAIN='local'
$env:GOFLAGS='-p=2'
$env:GOMAXPROCS='2'
$env:ANDROID_HOME=$AndroidSdk
$env:ANDROID_NDK_HOME=Join-Path $AndroidSdk "ndk\$NdkVersion"
$env:CGO_LDFLAGS='-Wl,-z,max-page-size=16384 -Wl,-z,common-page-size=16384'
$output=Join-Path $cacheRoot 'tsbridge.aar'
Push-Location (Join-Path $repoRoot 'integrations\deskhand\embedded-network')
try {
 & (Join-Path $ToolchainRoot 'bin\gomobile.exe') bind '-target=android/arm64,android/amd64' -androidapi=28 '-ldflags=-extldflags=-Wl,-z,max-page-size=16384' -o $output ./tsbridge
 if($LASTEXITCODE -ne 0){throw 'gomobile build failed; existing library retained'}
 & python (Join-Path $repoRoot 'scripts\verify-android-native.py') $output
 if($LASTEXITCODE -ne 0){throw 'Native alignment verification failed; existing library retained'}
 Copy-Item -LiteralPath $output -Destination (Join-Path $repoRoot 'integrations\deskhand\android\app\libs\tsbridge.aar') -Force
} finally { Pop-Location }
