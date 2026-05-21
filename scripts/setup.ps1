param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SetupArgs
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Python = Get-Command python -ErrorAction SilentlyContinue
if ($Python) {
    & $Python.Source "scripts\setup.py" @SetupArgs
    exit $LASTEXITCODE
}

$PyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($PyLauncher) {
    & $PyLauncher.Source -3 "scripts\setup.py" @SetupArgs
    exit $LASTEXITCODE
}

Write-Host "[setup エラー] Python 3.10 以上が見つかりません。" -ForegroundColor Red
Write-Host "Python をインストールして PATH を通してから、もう一度 .\scripts\setup.ps1 を実行してください。" -ForegroundColor Red
exit 1
