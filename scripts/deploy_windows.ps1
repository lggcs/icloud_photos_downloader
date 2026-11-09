# Deploy script for the icloudpd TUI on Windows (PowerShell 5.1 / ISE).
#
# Usage - in ISE: open this file and press F5. In a PowerShell console:
#     powershell -ExecutionPolicy Bypass -File .\scripts\deploy_windows.ps1
# If execution policy blocks the run: Set-ExecutionPolicy -Scope Process Bypass
#
# What it does, idempotently (everything already done is skipped):
#   1. Uses an existing Python 3.15; if absent, downloads and silently installs
#      Python 3.15.2 (per-user, no admin rights, installer cached in %TEMP%).
#   2. Creates .venv at the repo root, or reuses it when it was built with 3.15.
#   3. Installs icloudpd into it, editable from this checkout (one-time download
#      of the pinned dependency wheels; later source changes need no reinstall).
#   4. Proves 'python -m icloudpd' works (the channel the TUI spawns).
#   5. Launches the TUI - in a separate console window when run from ISE,
#      because ISE cannot host icloudpd's hidden password prompt.
#
# To follow a newer 3.15.x patch release, change $PythonVersion below.
# ASCII-only on purpose: ISE mangles UTF-8 files saved without a BOM.

[Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = 'Stop'

$PythonVersion = '3.15.2'
$Root = Split-Path $PSScriptRoot -Parent   # repo root: this script lives in scripts/
Set-Location -LiteralPath $Root

function Test-Python315 {
    # True when the file exists and runs as a Python 3.15 interpreter.
    param([string] $Exe)
    if (-not ($Exe -and (Test-Path -LiteralPath $Exe))) { return $false }
    & $Exe -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 15) else 2)' | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-Python315 {
    # Returns the full path of a Python 3.15 interpreter, or $null.
    # Local 'Continue': the py launcher writes to stderr when 3.15 is absent,
    # and a redirected native stderr line would otherwise terminate the script.
    $ErrorActionPreference = 'Continue'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $exe = (& py -3.15 -c 'import sys; print(sys.executable)' 2>$null) | Select-Object -First 1
        if (Test-Python315 $exe) { return $exe }
    }
    foreach ($candidate in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python315\python.exe'),
            (Join-Path $env:ProgramFiles 'Python315\python.exe')
        )) {
        if (Test-Python315 $candidate) { return $candidate }
    }
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($onPath -and (Test-Python315 $onPath.Source)) { return $onPath.Source }
    return $null
}

# 1) Interpreter
$Python = Get-Python315
if ($Python) {
    Write-Host "Python 3.15 already present: $Python"
} else {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
    $installerUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-$arch.exe"
    $installer = Join-Path $env:TEMP "python-$PythonVersion-$arch.exe"
    if ((Test-Path -LiteralPath $installer) -and ((Get-Item -LiteralPath $installer).Length -gt 10MB)) {
        Write-Host "Python installer already cached: $installer"
    } else {
        Write-Host "Downloading Python $PythonVersion ($arch, about 36 MB, one-time) from python.org..."
        $previousPreference = $ProgressPreference
        $ProgressPreference = 'SilentlyContinue'
        try { Invoke-WebRequest -Uri $installerUrl -OutFile $installer -UseBasicParsing }
        finally { $ProgressPreference = $previousPreference }
        if (-not (Test-Path -LiteralPath $installer) -or
            ((Get-Item -LiteralPath $installer).Length -lt 10MB)) {
            throw "Python installer download produced no usable file at $installer"
        }
    }
    Write-Host "Installing Python $PythonVersion (silent, per-user, no admin; typically under a minute)..."
    $process = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=0',
        'Include_launcher=1', 'InstallLauncherAllUsers=0', 'Include_test=0'
    )
    if ($process.ExitCode -ne 0) {
        throw "Python installer exited with code $($process.ExitCode).`n" +
            "Run it by hand without '/quiet' to see the error: $installer"
    }
    $Python = Get-Python315
    if (-not $Python) { throw "Python $PythonVersion was installed, but no 3.15 interpreter can be found." }
    Write-Host "Using newly installed Python 3.15: $Python"
}

# 2) Virtual environment at the repo root
$Venv = Join-Path $Root '.venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
if (Test-Path -LiteralPath $VenvPython) {
    & $VenvPython -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 15) else 2)' | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host ".venv already exists with Python 3.15 - reusing it."
    } else {
        Write-Host ".venv was built with a different Python - recreating it with 3.15."
        Remove-Item -Recurse -Force -LiteralPath $Venv
    }
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Creating .venv in $Root ..."
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed with exit code $LASTEXITCODE" }
}

# 3) icloudpd, editable from this checkout (skipped once installed)
$Tui = Join-Path $Venv 'Scripts\icloudpd-tui.exe'
if (Test-Path -LiteralPath $Tui) {
    Write-Host "icloudpd is already installed in .venv (editable) - skipping pip."
} else {
    Write-Host "Installing icloudpd from this checkout into .venv..."
    Write-Host "First run only: fetches the pinned dependency wheels from PyPI (about 20-30 MB)."
    & $VenvPython -m pip install -e $Root
    if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }
}

# 4) Gate: the TUI spawns 'python -m icloudpd', so that channel must work
& $VenvPython -m icloudpd --version
if ($LASTEXITCODE -ne 0) { throw "python -m icloudpd is not working inside .venv; the TUI cannot start." }
Write-Host "icloudpd works inside .venv."

# 5) Hand over to the TUI
if (Get-Variable psISE -ErrorAction SilentlyContinue) {
    Write-Host "Launching the iCloud backup TUI in a new console window (ISE cannot host its password prompt)..."
    Start-Process -FilePath $Tui -WorkingDirectory $Root
} else {
    Write-Host "Launching the iCloud backup TUI in this window..."
    & $Tui
    exit $LASTEXITCODE
}
