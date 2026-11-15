<#
  Starts the REDLINE hub with this repo's own virtual environment, never whatever "python" is on PATH.
  Written by scripts\setup-all.ps1's Desktop shortcut; safe to run by hand: powershell -ExecutionPolicy Bypass -File scripts\start-hub.ps1
#>
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Host 'The virtual environment is missing. Run: powershell -ExecutionPolicy Bypass -File scripts\setup-all.ps1' -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}
Set-Location $RepoRoot
& $VenvPython -m hub serve @args
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ('The hub stopped with code ' + $code + '. Run: .venv\Scripts\python -m hub doctor') -ForegroundColor Red
    Read-Host 'Press Enter to close'
}
exit $code
