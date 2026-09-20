<#
    Local entry point: discover -> prescreen -> reserve quota -> evaluate -> build index.

    Usage (run from the repo root, or just press Run in PyCharm):
        .\scripts\run-local.ps1                  # 3 evaluation slots (safe trial)
        .\scripts\run-local.ps1 -Limit 50        # full weekly quota
        .\scripts\run-local.ps1 -DryRun          # config check only: no model call, no ledger write

    Runbook and result checks: docs/运行说明.md
    NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads .ps1 as ANSI,
    so non-ASCII source bytes would break parsing. Chinese messages are decoded at runtime.
#>
[CmdletBinding()]
param(
    [int]$Limit = 3,
    [int]$Fetch = 0,
    [int]$Queries = 0,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Get-Msg([string]$b64) {
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
}

$M = @{
    Usage1     = 'ICAgIC5cc2NyaXB0c1xydW4tbG9jYWwucHMxICAgICAgICAgICAgICAjIDMg5Liq5ZCN6aKd6K+V5rC0'
    Usage2     = 'ICAgIC5cc2NyaXB0c1xydW4tbG9jYWwucHMxIC1MaW1pdCA1MCAgICAjIOi3kea7oeS4gOWRqOmineW6pg=='
    Usage3     = 'ICAgIC5cc2NyaXB0c1xydW4tbG9jYWwucHMxIC1EcnlSdW4gICAgICAjIOWPquS9k+ajgO+8muS4jeiBlOe9keaKk+WPluOAgeS4jeiwg+aooeWei+OAgeS4jeWGmei0puacrA=='
    NoVenv     = '5om+5LiN5Yiw6Jma5ouf546v5aKD77ya'
    Setup1     = '6K+35YWI5Yib5bu65bm25a6J6KOF5L6d6LWW77ya'
    NoToken1   = '5o+Q56S677ya5pyq6K6+572uIEdJVEhVQl9UT0tFTuOAgkdpdEh1YiDmnKrorqTor4HpmZDmtYHkuLogY29yZSA2MC/lsI/ml7bjgIFzZWFyY2ggMTAv5YiG6ZKf77yM'
    NoToken2   = 'ICAgICAg4oCc5bGV5byA5Yiw5YW35L2T5oqA6IO94oCd5Lya6KKr6ZmQ5rWB5oyh5L2P5LiA6YOo5YiG44CC6ZyA6KaB5pe25YWI5omn6KGM77ya'
    Warn1      = '5rOo5oSP77ya5pys5qyh5Lya55yf5a6e6LCD55So5qih5Z6L44CC5oyJIMKnNy4zIOavj+i9ruacgOWkmiA='
    Warn2      = 'IOS4quWQjemine+8jOWksei0peS5n+WNoOWQjemineOAgg=='
    Done1      = '5a6M5oiQ44CC5p+l55yL57uT5p6c77ya'
    Done2      = 'ICDmlbDmja7ntKLlvJUgICBkYXRhXGNhdGFsb2cuanNvbg=='
    Done3      = 'ICDlkajmiqUgICAgICAgZGF0YVxyZXBvcnRzXA=='
    Done4      = 'ICDpobXpnaLmlbDmja4gICBwdWJsaWNcZGF0YVxjYXRhbG9nLmpzb24='
    Done5      = 'ICDlho3miafooYwgLlxzY3JpcHRzXHByZXZpZXcucHMxIOWPr+WcqOa1j+iniOWZqOmHjOeci+ebruW9lQ=='
    Fail1      = '6L+Q6KGM5pyq5oiQ5Yqf77yI6YCA5Ye656CBIA=='
    Fail2      = '77yJ44CC5bi46KeB5Y6f5Zug77ya'
    Fail3      = 'ICDCtyDmqKHlnovlh63mja7nvLrlpLHvvIzmiJbku6PnkIbmlq3lvIDvvIhjb25maWdcbW9kZWwubG9jYWwuanNvbiAvIOezu+e7n+S7o+eQhu+8iQ=='
    Fail4      = 'ICDCtyDnvZHnu5zlpLHotKXvvJrmjInorr7orqHkv53nlZnkuIrmrKHmnInmlYjmlbDmja7vvIzkuI3lgZrmibnph4/liKDpmaQ='
    Fail5      = 'ICDCtyDlhajpg6jmnaXmupDlpLHotKXkuJTml7LmnInntKLlvJXpnZ7nqbrml7bkuLvliqjkuK3mraLvvIzpgb/lhY3muIXnqbrnm67lvZU='
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$PYTHONPATH = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $PYTHONPATH)) {
    Write-Host ((Get-Msg $M.NoVenv) + $PYTHONPATH) -ForegroundColor Red
    Write-Host (Get-Msg $M.Setup1) -ForegroundColor Yellow
    Write-Host "    py -3 -m venv .venv"
    Write-Host "    .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

$runArgs = @("-m", "src.pipeline")
if ($DryRun) {
    # requirement 7.3: dry_run does not touch the ledger, the model, git or the deployment
    $runArgs += @("--dry-run", "--limit-evaluations", $Limit)
} else {
    $runArgs += @("--phase", "all", "--limit-evaluations", $Limit)
}
if ($Fetch -gt 0) { $runArgs += @("--limit-fetches", $Fetch) }
if ($Queries -gt 0) { $runArgs += @("--limit-queries", $Queries) }

if (-not $env:GITHUB_TOKEN) {
    Write-Host (Get-Msg $M.NoToken1) -ForegroundColor Yellow
    Write-Host (Get-Msg $M.NoToken2) -ForegroundColor Yellow
    Write-Host '      $env:GITHUB_TOKEN = "<your-token>"' -ForegroundColor Yellow
}
if (-not $DryRun) {
    Write-Host ((Get-Msg $M.Warn1) + "$Limit" + (Get-Msg $M.Warn2)) -ForegroundColor Yellow
}

Write-Host ""
Write-Host ("> " + $PYTHONPATH + " " + ($runArgs -join " ")) -ForegroundColor DarkGray
& $PYTHONPATH @runArgs
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host (Get-Msg $M.Done1) -ForegroundColor Green
    Write-Host (Get-Msg $M.Done2)
    Write-Host (Get-Msg $M.Done3)
    Write-Host (Get-Msg $M.Done4)
    Write-Host (Get-Msg $M.Done5)
} else {
    Write-Host ((Get-Msg $M.Fail1) + "$code" + (Get-Msg $M.Fail2)) -ForegroundColor Red
    Write-Host (Get-Msg $M.Fail3)
    Write-Host (Get-Msg $M.Fail4)
    Write-Host (Get-Msg $M.Fail5)
}
exit $code
