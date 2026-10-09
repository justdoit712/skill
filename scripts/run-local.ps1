<#
    Local entry point: wrapper for tools/run_local.py; persists local discovery, evaluation and reports.

    Usage (run from the repo root, or just press Run in PyCharm):
        .\scripts\run-local.ps1                  # at most 3 candidate evaluations (paid model calls)
        .\scripts\run-local.ps1 -Limit 50        # at most 50 candidate evaluations
        .\scripts\run-local.ps1 -DryRun          # config check only: no model call, no ledger write

    Runbook and result checks: see the documentation links in README.md
    NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads .ps1 as ANSI,
    so non-ASCII source bytes would break parsing. Chinese messages are decoded at runtime.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 2147483647)]
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
    Usage1     = 'ICAgIC5cc2NyaXB0c1xydW4tbG9jYWwucHMxICAgICAgICAgICAgICAgICAgIyDmnIDlpJror4TkvLAgMyDkuKrlgJnpgInvvIzkvJrosIPnlKjmqKHlnos='
    Usage2     = 'ICAgIC5cc2NyaXB0c1xydW4tbG9jYWwucHMxIC1MaW1pdCA1MCAgICAgICAgIyDmnIDlpJror4TkvLAgNTAg5Liq5YCZ6YCJ77yM5LiN5piv5o6o6I2Q55uu5qCH'
    Usage3     = 'ICAgIC5cc2NyaXB0c1xydW4tbG9jYWwucHMxIC1EcnlSdW4gICAgICAjIOWPquS9k+ajgO+8muS4jeiBlOe9keaKk+WPluOAgeS4jeiwg+aooeWei+OAgeS4jeWGmei0puacrA=='
    NoVenv     = '5om+5LiN5Yiw6Jma5ouf546v5aKD77ya'
    Setup1     = '6K+35YWI5Yib5bu65bm25a6J6KOF5L6d6LWW77ya'
    NoToken1   = '5o+Q56S677ya546v5aKD5Y+Y6YeP5pyq6K6+572uIEdJVEhVQl9UT0tFTu+8m+eoi+W6j+i/mOS8muivu+WPluacrOWcsOWvhumSpeaYoOWwhOS4reeahCBnaXRodWJfdG9rZW4g5oiWIGdpdGh1YuOAgg=='
    NoToken2   = 'ICAgICAg5Lik5aSE6YO95pyq6YWN572u5pe25L2/55So5pyq6K6k6K+B6K6/6Zeu77yM5Y+v6IO95Y+X5Yiw6ZmQ5rWB77yb546v5aKD5Y+Y6YeP56S65L6L77ya'
    Warn1      = '5rOo5oSP77ya5pys5qyh5Lya6IGU572R5bm26LCD55So5qih5Z6L77yM5YCZ6YCJ6K+E5Lyw5LiK6ZmQ5Li6IA=='
    Warn2      = '77yb5o6o6I2Q55uu5qCH6K+75Y+W5pys5Zyw6YWN572u77yM6YeN6K+V5ZKM6L2u5o2i5Y+v6IO95aKe5Yqg5a6e6ZmF6K+35rGC5pWw44CC'
    Done1      = '5a6M5oiQ44CC5p+l55yL57uT5p6c77ya'
    Done2      = 'ICDmlbDmja7ntKLlvJUgICBkYXRhXGNhdGFsb2cuanNvbg=='
    Done3      = 'ICDov5DooYzmiqXlkYogICAgIGRhdGFcbG9jYWxccnVuc1zvvIjmnIDmlrDmkZjopoHop4EgZGF0YVxsb2NhbFxsYXRlc3QtcnVuLmpzb27vvIk='
    Done4      = 'ICDpobXpnaLmlbDmja4gICBwdWJsaWNcZGF0YVxjYXRhbG9nLmpzb24='
    Done5      = 'ICDlho3miafooYwgLlxzY3JpcHRzXHByZXZpZXcucHMxIOWPr+WcqOa1j+iniOWZqOmHjOeci+ebruW9lQ=='
    Fail1      = '6L+Q6KGM5pyq5oiQ5Yqf77yI6YCA5Ye656CBIA=='
    Fail2      = '77yJ44CC5bi46KeB5Y6f5Zug77ya'
    Fail3      = 'ICDCtyDmqKHlnovlh63mja7nvLrlpLHmiJbov57mjqXlpLHotKXvvJrmo4Dmn6UgY29uZmlnL21vZGVscy8g5LiL55qE55Sf5pWI6YWN572u44CB5a+G6ZKl5p2l5rqQ5Y+K5Luj55CG'
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

$runArgs = @("tools/run_local.py", "--max-evaluations", "$Limit")
if ($DryRun) {
    # Local precheck only: no network, model call or runtime data write
    $runArgs += @("--check")
}
if ($Fetch -gt 0) { $runArgs += @("--expand-limit", "$Fetch") }
if ($Queries -gt 0) { $runArgs += @("--limit-queries", "$Queries") }

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
