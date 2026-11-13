#Requires -Version 5.1
<#
.SYNOPSIS
Installs the pmarb collector on Windows as the Task Scheduler task "pmarb-collector".

.DESCRIPTION
Steps, in order. Every step stops the script on failure.
  1. Check that git and Python 3.12 (`py -3.12`) are available.
  2. Create <RepoPath>\.venv with Python 3.12 if it is missing.
  3. Run `python -m pip install -e .[dev]`.
  4. Create %USERPROFILE%\.config\pmarb\. If .env is missing there, copy .env.example
     to it with PMARB_DATA_DIR set to -DataDir and the example key paths cleared
     (M1 needs no keys). An existing .env is never overwritten.
  5. Restrict .env to the current user with icacls (inheritance removed).
  6. Create <DataDir> and <DataDir>\logs, and initialize the database schema.
  7. Run `pmarb doctor`. If it reports any FAIL, stop without registering the task.
  8. Render deploy\windows\pmarb-collector.task.xml.template and register the task.
     It starts at logon, restarts one minute after a failure, has no time limit,
     and runs in <RepoPath>.
  9. With -Start, start the task.

The script needs no administrator rights. It never changes power or sleep
settings; see docs\RUN_ON_WINDOWS.md for those. It never reads or prints secrets.

.PARAMETER RepoPath
Checkout root. Default: two levels above this script.

.PARAMETER DataDir
Data directory. Default: <RepoPath>\data, which git ignores. Keep it short to
stay clear of the 260-character path limit.

.PARAMETER Start
Start the task after registering it.

.PARAMETER Uninstall
Stop the collector cleanly and remove the task. Data and settings are kept.

.PARAMETER DryRun
Print each change instead of making it.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\deploy\windows\install.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$RepoPath,
    [string]$DataDir,
    [switch]$Start,
    [switch]$Uninstall,
    [switch]$DryRun
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$TaskName = 'pmarb-collector'

function Write-Step([string]$Message) {
    Write-Host "==> $Message"
}

function Invoke-Change([string]$Description, [scriptblock]$Action) {
    if ($DryRun) {
        Write-Host "DRY-RUN would $Description"
        return
    }
    Write-Step $Description
    & $Action
}

function Assert-NativeSuccess([string]$What) {
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE"
    }
}

# Windows PowerShell 5.1 turns a native program's stderr into error records when
# output is captured (CI, an assistant's shell), and 'Stop' would make the first
# pip notice or doctor WARN line fatal. These helpers merge stderr as plain text
# and leave $LASTEXITCODE for the caller to check.
function Invoke-Native([scriptblock]$Command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Command 2>&1 | ForEach-Object { Write-Host "$_" }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Get-NativeText([scriptblock]$Command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $Command 2>&1 | ForEach-Object { "$_" })
    } finally {
        $ErrorActionPreference = $previous
    }
    return ($lines -join ' ').Trim()
}

function Resolve-FullPath([string]$Path) {
    $provider = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $full = [System.IO.Path]::GetFullPath($provider)
    if ($full.Length -gt 3) {
        $full = $full.TrimEnd('\')
    }
    return $full
}

function Assert-SafePath([string]$Name, [string]$Path) {
    if ($Path -match '["%\r\n]') {
        throw "$Name cannot contain double quotes, percent signs, or line breaks: $Path"
    }
    if ($Path.Length -le 3) {
        throw "$Name cannot be a drive root: $Path"
    }
}

# --- Paths -------------------------------------------------------------------
if (-not $RepoPath) {
    $RepoPath = Join-Path $PSScriptRoot '..\..'
}
$RepoPath = Resolve-FullPath $RepoPath
if (-not (Test-Path -LiteralPath (Join-Path $RepoPath 'pyproject.toml'))) {
    throw "RepoPath does not look like the pmarb checkout (no pyproject.toml): $RepoPath"
}
if (-not $DataDir) {
    $DataDir = Join-Path $RepoPath 'data'
}
$DataDir = Resolve-FullPath $DataDir
Assert-SafePath 'RepoPath' $RepoPath
Assert-SafePath 'DataDir' $DataDir
if (-not $env:USERPROFILE) {
    throw 'USERPROFILE is not set; run this script as a normal logged-on user.'
}

$venvDir = Join-Path $RepoPath '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$pmarb = Join-Path $venvDir 'Scripts\pmarb.exe'
$configDir = Join-Path $env:USERPROFILE '.config\pmarb'
$envFile = Join-Path $configDir '.env'
$logDir = Join-Path $DataDir 'logs'
$template = Join-Path $RepoPath 'deploy\windows\pmarb-collector.task.xml.template'
$renderer = Join-Path $RepoPath 'deploy\render_template.py'
$runner = Join-Path $RepoPath 'deploy\windows\run-collector.ps1'
$stopScript = Join-Path $RepoPath 'deploy\windows\stop.ps1'
$renderedTask = Join-Path $configDir 'pmarb-collector.task.xml'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$taskUser = $identity.Name
$userSid = $identity.User.Value

Write-Host "Task:     $TaskName"
Write-Host "Repo:     $RepoPath"
Write-Host "Data:     $DataDir"
Write-Host "Logs:     $logDir"
Write-Host "Settings: $envFile"
Write-Host "User:     $taskUser"

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

# --- Uninstall ---------------------------------------------------------------
if ($Uninstall) {
    if ($null -eq $existingTask) {
        Write-Host "OK task $TaskName is not registered; nothing to remove."
        exit 0
    }
    if ($existingTask.State -eq 'Running') {
        Invoke-Change "stop the running collector cleanly with $stopScript" {
            # No path arguments: stop.ps1 reads them from the registered task.
            & $stopScript
            Assert-NativeSuccess 'stop.ps1'
        }
    }
    Invoke-Change "unregister task $TaskName" {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Write-Host "Kept data ($DataDir) and settings ($envFile)."
    exit 0
}

if ($null -ne $existingTask -and $existingTask.State -eq 'Running') {
    throw "Task $TaskName is running. Stop it first: powershell -NoProfile -ExecutionPolicy RemoteSigned -File `"$stopScript`""
}

# --- 1. Prerequisites (read-only checks, also run with -DryRun) ---------------
Write-Step 'check git and Python 3.12'
if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git was not found on PATH. Install it with: winget install -e --id Git.Git (then open a new PowerShell window).'
}
if ($null -eq (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "The Python launcher 'py' was not found. Install Python 3.12 from python.org (keep 'py launcher' ticked) or: winget install -e --id Python.Python.3.12"
}
$pythonVersion = Get-NativeText { & py -3.12 --version }
if ($LASTEXITCODE -ne 0 -or "$pythonVersion" -notmatch '^Python 3\.12\.') {
    throw "py -3.12 did not find Python 3.12 (got: $pythonVersion). Install it: winget install -e --id Python.Python.3.12"
}
Write-Host "OK $pythonVersion"

# --- 2. Virtual environment ----------------------------------------------------
if (Test-Path -LiteralPath $venvPython) {
    $venvVersion = Get-NativeText { & $venvPython --version }
    if ("$venvVersion" -notmatch '^Python 3\.12\.') {
        throw "$venvDir uses $venvVersion, not Python 3.12. Rename or remove that folder yourself, then rerun."
    }
    Write-Host "OK existing virtual environment ($venvVersion)"
} else {
    Invoke-Change "create the virtual environment $venvDir" {
        Invoke-Native { & py -3.12 -m venv $venvDir }
        Assert-NativeSuccess 'py -3.12 -m venv'
    }
}

# --- 3. Package ------------------------------------------------------------------
Invoke-Change 'install the package: python -m pip install -e .[dev]' {
    Push-Location -LiteralPath $RepoPath
    try {
        Invoke-Native { & $venvPython -m pip install -e '.[dev]' }
        Assert-NativeSuccess 'pip install'
    } finally {
        Pop-Location
    }
}

# --- 4. Settings file --------------------------------------------------------------
Invoke-Change "create $configDir" {
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
}
if (Test-Path -LiteralPath $envFile) {
    Write-Host "OK keeping the existing settings file $envFile (not overwritten)"
    $configured = Select-String -LiteralPath $envFile -Pattern '^PMARB_DATA_DIR=(.*)$' | Select-Object -First 1
    if ($null -ne $configured) {
        $configuredDir = $configured.Matches[0].Groups[1].Value.Trim()
        if ($configuredDir -and $configuredDir -ne $DataDir) {
            Write-Warning "$envFile sets PMARB_DATA_DIR=$configuredDir but the task will use $DataDir. Commands you run by hand (pmarb report, pmarb review, pmarb stop) read the settings file, so edit that line to $DataDir."
        }
    }
} else {
    Invoke-Change "copy .env.example to $envFile (PMARB_DATA_DIR set, example key paths cleared)" {
        $example = Join-Path $RepoPath '.env.example'
        $lines = Get-Content -LiteralPath $example -Encoding UTF8 | ForEach-Object {
            if ($_ -match '^PMARB_DATA_DIR=') {
                "PMARB_DATA_DIR=$DataDir"
            } elseif ($_ -match '^(KALSHI|POLYUS)_PRIVATE_KEY_PATH=') {
                # M1 needs no keys. A path to a missing key file fails pmarb doctor.
                ($_ -split '=', 2)[0] + '='
            } else {
                $_
            }
        }
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllLines($envFile, [string[]]$lines, $utf8NoBom)
    }
}

# --- 5. Restrict the settings file to this account ------------------------------
Invoke-Change "restrict $envFile to $taskUser (icacls /inheritance:r /grant:r)" {
    Invoke-Native { & icacls $envFile /inheritance:r /grant:r "*${userSid}:(F)" }
    Assert-NativeSuccess 'icacls'
}

# --- 6. Data directory and database schema --------------------------------------
Invoke-Change "create $DataDir and $logDir" {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
Invoke-Change "initialize the database schema in $DataDir" {
    $init = "import sys; from pathlib import Path; from pmarb.storage.db import open_writer; open_writer(Path(sys.argv[1]) / 'pmarb.sqlite').close()"
    Invoke-Native { & $venvPython -c $init $DataDir }
    Assert-NativeSuccess 'database initialization'
}

# --- 7. Doctor -----------------------------------------------------------------------
Invoke-Change 'run pmarb doctor' {
    $env:PMARB_DATA_DIR = $DataDir
    $env:PMARB_ENV_FILE = $envFile
    Invoke-Native { & $pmarb doctor }
    if ($LASTEXITCODE -ne 0) {
        throw 'pmarb doctor reported FAIL lines (above); the task was NOT registered. Fix them and rerun install.ps1.'
    }
}

# --- 8. Task Scheduler ------------------------------------------------------------
Invoke-Change "render the task definition to $renderedTask" {
    Invoke-Native {
        & $venvPython $renderer windows $template $renderedTask `
            "TASK_USER=$taskUser" "RUNNER_SCRIPT=$runner" "REPO_DIR=$RepoPath" `
            "DATA_DIR=$DataDir" "ENV_FILE=$envFile"
    }
    Assert-NativeSuccess 'render_template.py'
}
Invoke-Change "register the scheduled task $TaskName (starts at logon, restarts after a failure)" {
    $xml = Get-Content -LiteralPath $renderedTask -Raw
    # The template's explanatory comment is for people; register the bare definition.
    $xml = [regex]::Replace($xml, '<!--[\s\S]*?-->', '')
    Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
}

# --- 9. Start ------------------------------------------------------------------------
if ($Start) {
    Invoke-Change "start task $TaskName" {
        Start-ScheduledTask -TaskName $TaskName
    }
} else {
    Write-Host "Not started. Start it with: Start-ScheduledTask -TaskName $TaskName"
}

Write-Host ''
Write-Host "OK installed. Logs: $logDir (pmarb.log, collector.wrapper.log, collector.stderr.log)"
Write-Host 'Stop cleanly with: powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\deploy\windows\stop.ps1'
Write-Host 'Sleep settings are not changed by this script; see docs\RUN_ON_WINDOWS.md.'
