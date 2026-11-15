<#
.SYNOPSIS
  One-shot setup for REDLINE (the laptop hub) on Windows. Safe to run again: it only fills in what is missing.

.DESCRIPTION
  1. finds Python 3.11+ (offers to install it with winget)
  2. creates or reuses .venv in this repo and installs EVERY requirements file (plus Piper, pynput, sounddevice,
     pywinpty, pyautogui; av is pinned to 16.1.0)
  3. installs Playwright's Chromium
  4. checks Ollama (offers to install it and to pull a small chat model and a small vision model)
  5. checks Tailscale, Node and agy (Antigravity CLI)
  6. creates the hub settings if there are none, then runs: python -m hub setup --all
  7. downloads one Piper voice (or tells you the Windows voice will be used)
  8. adds a "Start REDLINE hub" shortcut that always uses the venv's python
  9. prints a green/red checklist with the exact next step for every red item

  No secrets are read, printed or stored by this script. Nothing is deleted. Every install asks first (use -Yes to say yes to all).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup-all.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup-all.ps1 -Yes -SkipOllamaModels
#>
[CmdletBinding()]
param(
    [switch]$Yes,                       # answer yes to every question (installs Python, Ollama and the models too)
    [switch]$SkipOllamaModels,          # never pull models
    [switch]$SkipPlaywright,            # do not download Chromium
    [string]$ChatModel = 'qwen3:4b',    # small default chat model
    [string]$VisionModel = 'moondream', # small vision model
    [string]$PiperVoice = 'en_US-lessac-low',
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
$Results = New-Object System.Collections.ArrayList

function Add-Result {
    param([string]$Name, [bool]$Ok, [string]$Detail, [string]$Next = '', [bool]$Required = $false)
    [void]$Results.Add([pscustomobject]@{ Name = $Name; Ok = $Ok; Detail = $Detail; Next = $Next; Required = $Required })
}

function Say   { param([string]$Text) Write-Host $Text }
function Good  { param([string]$Text) Write-Host ("[ OK ] " + $Text) -ForegroundColor Green }
function Bad   { param([string]$Text) Write-Host ("[FIX ] " + $Text) -ForegroundColor Red }
function Step  { param([string]$Text) Write-Host ""; Write-Host ("== " + $Text) -ForegroundColor Cyan }

function Ask {
    param([string]$Question)
    if ($Yes) { return $true }
    $answer = Read-Host ($Question + " [y/N]")
    return ($answer -match '^(y|yes)$')
}

function Test-Command { param([string]$Name) return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Update-SessionPath {
    # a program installed by winget is not on PATH of this window yet: pick up the machine and user PATH again
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = ($machine + ';' + $user)
}

function Get-PythonVersion {
    param([string]$Exe, [string[]]$PrefixArgs = @())
    try {
        $out = & $Exe @PrefixArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $text = ("" + $out).Trim()
        if ($text -notmatch '^\d+\.\d+$') { return $null }
        return [version]$text
    } catch { return $null }
}

function Find-Python {
    # returns @{ Exe = ...; Args = @(...) } for the first Python 3.11+ found, else $null
    $candidates = @()
    if (Test-Command 'py') {
        foreach ($v in @('-3.13', '-3.12', '-3.11')) { $candidates += , @('py', @($v)) }
    }
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        # the Microsoft Store stub under WindowsApps only opens the Store: skip it
        if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { $candidates += , @($cmd.Source, @()) }
    }
    foreach ($dir in @("$env:LOCALAPPDATA\Programs\Python\Python313", "$env:LOCALAPPDATA\Programs\Python\Python312", "$env:LOCALAPPDATA\Programs\Python\Python311",
                       "$env:ProgramFiles\Python313", "$env:ProgramFiles\Python312", "$env:ProgramFiles\Python311")) {
        if (Test-Path "$dir\python.exe") { $candidates += , @("$dir\python.exe", @()) }
    }
    foreach ($c in $candidates) {
        $ver = Get-PythonVersion -Exe $c[0] -PrefixArgs $c[1]
        if ($ver -and $ver -ge [version]'3.11') { return @{ Exe = $c[0]; Args = $c[1]; Version = $ver } }
    }
    return $null
}

# ---------------------------------------------------------------------------------------------------------------
Step 'Python 3.11 or newer'
$py = Find-Python
if (-not $py) {
    Bad 'Python 3.11+ was not found.'
    if ((Test-Command 'winget') -and (Ask 'Install Python 3.12 with winget now?')) {
        & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        Update-SessionPath
        $py = Find-Python
    }
}
if ($py) {
    Good ("Python " + $py.Version + " (" + $py.Exe + ")")
    Add-Result 'Python 3.11+' $true ("version " + $py.Version) '' $true
} else {
    Add-Result 'Python 3.11+' $false 'not found' 'Run: winget install -e --id Python.Python.3.12   then open a NEW PowerShell and run this script again.' $true
}

$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$DepsOk = $false
if ($py) {
    Step 'Virtual environment (.venv in this folder)'
    if (-not (Test-Path $VenvPython)) {
        $pyArgs = $py.Args
        & $py.Exe @pyArgs -m venv (Join-Path $RepoRoot '.venv')
    }
    $venvVer = Get-PythonVersion -Exe $VenvPython
    if ($venvVer -and $venvVer -ge [version]'3.11') {
        Good ('.venv ready (Python ' + $venvVer + ')')
        Add-Result 'Virtual environment' $true '.venv' '' $true
    } else {
        Bad '.venv could not be created or is older than 3.11.'
        Add-Result 'Virtual environment' $false '.venv missing or too old' 'Delete the .venv folder yourself, then run this script again.' $true
        $py = $null
    }
}

if ($py) {
    Step 'Python packages (every requirements file)'
    & $VenvPython -m pip install --upgrade pip 2>&1 | Out-Null
    $failed = @()
    foreach ($file in @('requirements.txt', 'requirements-voice.txt', 'requirements-work.txt', 'requirements-laptop.txt', 'requirements-extra.txt', 'requirements-screen.txt')) {
        $path = Join-Path $RepoRoot $file
        if (-not (Test-Path $path)) { continue }
        Say ("pip install -r " + $file)
        & $VenvPython -m pip install -r $path
        if ($LASTEXITCODE -ne 0) {
            # one bad package must not hide the others: retry line by line
            Say ("  retrying " + $file + " one package at a time")
            foreach ($line in (Get-Content $path)) {
                $spec = ($line -replace '#.*$', '').Trim()
                if (-not $spec) { continue }
                & $VenvPython -m pip install $spec
                if ($LASTEXITCODE -ne 0) { $failed += $spec }
            }
        }
    }
    # the things the app says "not installed" about, named once more so a changed requirements file cannot drop them
    foreach ($spec in @('av==16.1.0', 'piper-tts', 'pynput', 'sounddevice', 'pywinpty', 'pyautogui', 'mss', 'pillow', 'playwright', 'pygetwindow', 'pywinauto', 'soundcard')) {
        $name = ($spec -split '[=<>]')[0]
        & $VenvPython -m pip show $name 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0 -or $spec -eq 'av==16.1.0') {
            & $VenvPython -m pip install $spec
            if ($LASTEXITCODE -ne 0) { $failed += $spec }
        }
    }
    if ($failed.Count -eq 0) {
        Good 'all Python packages installed'
        $DepsOk = $true
        Add-Result 'Python packages' $true 'every requirements file' '' $true
    } else {
        $list = (($failed | Select-Object -Unique) -join ', ')
        Bad ('could not install: ' + $list)
        Add-Result 'Python packages' $false ('failed: ' + $list) ('Run: .venv\Scripts\python -m pip install ' + $list + '   (if Windows blocks pip, ask for an Application Control rule for .venv\Scripts\python.exe)') $true
    }
}

# ---------------------------------------------------------------------------------------------------------------
if ($DepsOk -and -not $SkipPlaywright) {
    Step 'Playwright browser (Chromium, for form filling and chat)'
    & $VenvPython -m playwright install chromium
    if ($LASTEXITCODE -eq 0) {
        Good 'Chromium installed for Playwright'
        Add-Result 'Playwright Chromium' $true 'installed' ''
    } else {
        Bad 'python -m playwright install chromium failed'
        Add-Result 'Playwright Chromium' $false 'download failed' 'Check the internet connection, then run: .venv\Scripts\python -m playwright install chromium'
    }
}

# ---------------------------------------------------------------------------------------------------------------
Step 'Ollama (local AI)'
$ollama = $null
if (Test-Command 'ollama') { $ollama = 'ollama' }
elseif (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe") { $ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
if (-not $ollama) {
    Bad 'Ollama is not installed.'
    if ((Test-Command 'winget') -and (Ask 'Install Ollama with winget now?')) {
        & winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
        Update-SessionPath
        if (Test-Command 'ollama') { $ollama = 'ollama' }
        elseif (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe") { $ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
    }
}
if ($ollama) {
    Good 'Ollama program found'
    $listing = & $ollama list 2>&1
    if ($LASTEXITCODE -ne 0) {
        Bad 'Ollama is installed but not running.'
        Add-Result 'Ollama' $false 'not running' 'Start the Ollama app from the Start menu (tray icon), then run this script again.'
    } else {
        Add-Result 'Ollama' $true 'running' ''
        if (-not $SkipOllamaModels) {
            foreach ($model in @($ChatModel, $VisionModel)) {
                $have = ($listing | Out-String) -match [regex]::Escape(($model -split ':')[0])
                if ($have) { Good ('model ' + $model + ' already pulled'); Add-Result ('Model ' + $model) $true 'pulled' ''; continue }
                if (Ask ('Pull the small model ' + $model + ' now (a few GB, needs internet)?')) {
                    & $ollama pull $model
                    if ($LASTEXITCODE -eq 0) { Good ('pulled ' + $model); Add-Result ('Model ' + $model) $true 'pulled' '' }
                    else { Bad ('could not pull ' + $model); Add-Result ('Model ' + $model) $false 'pull failed' ('Run: ollama pull ' + $model) }
                } else {
                    Add-Result ('Model ' + $model) $false 'not pulled' ('Run: ollama pull ' + $model)
                }
            }
        }
    }
} else {
    Add-Result 'Ollama' $false 'not installed' 'Run: winget install -e --id Ollama.Ollama   then start the Ollama app and run this script again.'
}

# ---------------------------------------------------------------------------------------------------------------
Step 'Tailscale, Node, agy'
$tailscale = $null
if (Test-Command 'tailscale') { $tailscale = 'tailscale' }
elseif (Test-Path "$env:ProgramFiles\Tailscale\tailscale.exe") { $tailscale = "$env:ProgramFiles\Tailscale\tailscale.exe" }
if ($tailscale) {
    & $tailscale status 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Good 'Tailscale connected'; Add-Result 'Tailscale' $true 'connected' '' }
    else { Bad 'Tailscale is installed but not signed in'; Add-Result 'Tailscale' $false 'not connected' 'Open the Tailscale tray icon and sign in, then run: tailscale serve --bg 8765' }
} else {
    Bad 'Tailscale not found'
    Add-Result 'Tailscale' $false 'not installed' 'Install it from https://tailscale.com/download (or: winget install -e --id Tailscale.Tailscale), sign in on the laptop AND the phone.'
}

if (Test-Command 'node') { Good ('Node ' + (& node --version)); Add-Result 'Node' $true (& node --version) '' }
else { Bad 'Node not found (only needed to build the phone app)'; Add-Result 'Node' $false 'not installed' 'Run: winget install -e --id OpenJS.NodeJS.LTS (only if you build the Android app yourself).' }

if (Test-Command 'agy') {
    & agy --help 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Good 'agy (Antigravity CLI) works'
        Add-Result 'agy (Antigravity CLI)' $true 'agy --help works' 'Run `agy` once in a terminal and sign in with your Google account (this script cannot do that for you).'
    } else {
        Bad 'agy is installed but "agy --help" failed'
        Add-Result 'agy (Antigravity CLI)' $false 'agy --help failed' 'Reinstall Antigravity, then run `agy` once and sign in.'
    }
} else {
    Bad 'agy (Antigravity CLI) not found'
    Add-Result 'agy (Antigravity CLI)' $false 'not installed' 'Install Antigravity from antigravity.google, make sure `agy --help` works in a NEW PowerShell, then run `agy` once and sign in.'
}

# ---------------------------------------------------------------------------------------------------------------
if ($DepsOk) {
    Step 'Hub settings and background tasks'
    $configPath = Join-Path $env:USERPROFILE '.laptop-hub\hub.json'
    if (-not (Test-Path $configPath)) {
        Say 'No settings yet: creating them (your login token is shown ONCE below; write it down).'
        & $VenvPython -m hub init
    }
    if (Test-Path $configPath) {
        & $VenvPython -m hub setup --all
        if ($LASTEXITCODE -eq 0) { Good 'updater, watchdog and backup tasks set up'; Add-Result 'Hub setup (python -m hub setup --all)' $true 'done' '' }
        else { Bad 'python -m hub setup --all reported a problem'; Add-Result 'Hub setup (python -m hub setup --all)' $false 'a step failed' 'Run: .venv\Scripts\python -m hub setup   (it explains each step)' }
    } else {
        Add-Result 'Hub settings' $false 'hub.json was not created' 'Run: .venv\Scripts\python -m hub init' $true
    }

    Step 'Laptop voice'
    & $VenvPython -m pip show piper-tts 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        & $VenvPython -m hub voice --download $PiperVoice
        if ($LASTEXITCODE -eq 0) { Good ('Piper voice ' + $PiperVoice + ' ready'); Add-Result 'Laptop voice (Piper)' $true $PiperVoice '' }
        else { Bad 'Piper voice download failed'; Add-Result 'Laptop voice (Piper)' $false 'download failed' ('The Windows voice still works. Retry: .venv\Scripts\python -m hub voice --download ' + $PiperVoice) }
    } else {
        Good 'Piper is not installed: the laptop will use the Windows voice (SAPI), which needs nothing extra'
        Add-Result 'Laptop voice (Windows SAPI)' $true 'Windows voice' ''
    }

    Step 'Doctor (the same checklist the app uses)'
    & $VenvPython -m hub doctor
}

# ---------------------------------------------------------------------------------------------------------------
if ($DepsOk) {
    Step 'Start shortcut (always uses .venv python)'
    $startScript = Join-Path $RepoRoot 'scripts\start-hub.ps1'
    if (Test-Path $startScript) {
        Good ('start script: ' + $startScript)
        if (-not $NoShortcut) {
            try {
                $desktop = [Environment]::GetFolderPath('Desktop')
                $lnk = Join-Path $desktop 'Start REDLINE hub.lnk'
                $shell = New-Object -ComObject WScript.Shell
                $sc = $shell.CreateShortcut($lnk)
                $sc.TargetPath = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
                $sc.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $startScript + '"'
                $sc.WorkingDirectory = $RepoRoot
                $sc.Save()
                Good ('shortcut: ' + $lnk)
                Add-Result 'Start shortcut' $true $lnk ''
            } catch {
                Add-Result 'Start shortcut' $false 'could not create the Desktop shortcut' ('Start the hub with: powershell -ExecutionPolicy Bypass -File "' + $startScript + '"')
            }
        }
    } else {
        Add-Result 'Start script' $false 'scripts\start-hub.ps1 is missing' 'Run: git pull'
    }
}

# ---------------------------------------------------------------------------------------------------------------
Write-Host ''
Write-Host '================ CHECKLIST ================' -ForegroundColor Cyan
$anyRequiredRed = $false
foreach ($r in $Results) {
    if ($r.Ok) {
        Write-Host ('[GREEN] ' + $r.Name + ' - ' + $r.Detail) -ForegroundColor Green
    } else {
        Write-Host ('[ RED ] ' + $r.Name + ' - ' + $r.Detail) -ForegroundColor Red
        Write-Host ('         next: ' + $r.Next)
        if ($r.Required) { $anyRequiredRed = $true }
    }
}
$agyRow = $Results | Where-Object { $_.Name -like 'agy*' -and $_.Ok }
if ($agyRow) { Write-Host '         agy: run `agy` once in a terminal and sign in (this script cannot do that for you).' -ForegroundColor Yellow }
Write-Host ''
if ($anyRequiredRed) {
    Write-Host 'Something required is still red. Fix the "next" line above and run this script again (it is safe to re-run).' -ForegroundColor Red
    exit 1
}
Write-Host 'Next: double-click "Start REDLINE hub" on your Desktop (or run scripts\start-hub.ps1), then open the app on your phone.' -ForegroundColor Green
exit 0
