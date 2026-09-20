<#
    Local preview entry point: serve public/ and open the catalog page in a browser.

    Usage (run from the repo root, or just press Run in PyCharm):
        .\scripts\preview.ps1                # pick a free port automatically
        .\scripts\preview.ps1 -Port 8080     # use a fixed port

    The page fetches data/catalog.json, i.e. public\data\catalog.json, which is produced by
    .\scripts\run-local.ps1. Until it exists the page shows an empty state.
    Press Ctrl+C to stop.
    NOTE: this file is intentionally ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
#>
[CmdletBinding()]
param(
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"

function Get-Msg([string]$b64) {
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
}

$M = @{
    NoPublic = '5om+5LiN5Yiw6aG16Z2i55uu5b2V77ya'
    Hint1    = '5o+Q6YaS77yacHVibGljXGRhdGFcY2F0YWxvZy5qc29uIOWwmuS4jeWtmOWcqO+8jOmhtemdouS8muaYvuekuuepuueKtuaAgeOAgg=='
    Hint2    = 'ICAgICAg6K+35YWI5omn6KGMIC5cc2NyaXB0c1xydW4tbG9jYWwucHMxIOeUn+aIkOaVsOaNruOAgg=='
    Url      = '6aKE6KeI5Zyw5Z2A77ya'
    Stop     = '5oyJIEN0cmwrQyDnu5PmnZ/jgII='
}

$root = Split-Path -Parent $PSScriptRoot
$public = Join-Path $root "public"

if (-not (Test-Path $public)) {
    Write-Host ((Get-Msg $M.NoPublic) + $public) -ForegroundColor Red
    exit 1
}

$PYTHONPATH = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $PYTHONPATH)) { $PYTHONPATH = "python" }

if ($Port -le 0) {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $Port = $listener.LocalEndpoint.Port
    $listener.Stop()
}

if (-not (Test-Path (Join-Path $public "data\catalog.json"))) {
    Write-Host (Get-Msg $M.Hint1) -ForegroundColor Yellow
    Write-Host (Get-Msg $M.Hint2) -ForegroundColor Yellow
    Write-Host ""
}

$url = "http://127.0.0.1:$Port/"
Write-Host ((Get-Msg $M.Url) + $url) -ForegroundColor Green
Write-Host (Get-Msg $M.Stop) -ForegroundColor DarkGray

# static site: the server root must be public/, otherwise data/catalog.json is a 404
Set-Location $public
Start-Process $url
& $PYTHONPATH -m http.server $Port --bind 127.0.0.1
