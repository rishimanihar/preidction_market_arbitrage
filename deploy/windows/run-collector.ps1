#Requires -Version 5.1
<#
.SYNOPSIS
Runs `pmarb scan` for the pmarb-collector scheduled task and restarts it after a crash.

.DESCRIPTION
The scheduled task runs this script, which runs `python -m pmarb.service`, the same
entry point the macOS and Linux services use.

- Application logs go to <DataDir>\logs\pmarb.log, a rotating file.
- The process's stdout and stderr go to collector.stdout.log and collector.stderr.log
  in the same folder. The previous run's copies are kept as *.prev.
- This wrapper writes one line per start and exit to collector.wrapper.log.

Exit code 0 means a clean stop (`pmarb stop`, Ctrl+C). The wrapper then exits too.
Any other exit code is a crash. The wrapper waits RestartDelaySeconds and starts
the collector again. If a stop request (<DataDir>\collector.stop) appears during
that wait, the wrapper deletes it and exits, so `pmarb stop --wait` still completes.

.PARAMETER RepoPath
Checkout root containing .venv.

.PARAMETER DataDir
Data directory (PMARB_DATA_DIR).

.PARAMETER EnvFile
Settings file (PMARB_ENV_FILE).

.PARAMETER RestartDelaySeconds
Delay before restarting after a crash. Default 60.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepoPath,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [int]$RestartDelaySeconds = 60
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$python = Join-Path $RepoPath '.venv\Scripts\python.exe'
$logDir = Join-Path $DataDir 'logs'
$stopFile = Join-Path $DataDir 'collector.stop'
$wrapperLog = Join-Path $logDir 'collector.wrapper.log'
$stdoutLog = Join-Path $logDir 'collector.stdout.log'
$stderrLog = Join-Path $logDir 'collector.stderr.log'
$maxWrapperLogBytes = 5MB

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-WrapperLog([string]$Message) {
    if ((Test-Path -LiteralPath $wrapperLog) -and ((Get-Item -LiteralPath $wrapperLog).Length -gt $maxWrapperLogBytes)) {
        Move-Item -LiteralPath $wrapperLog -Destination "$wrapperLog.prev" -Force
    }
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    Add-Content -LiteralPath $wrapperLog -Value "$stamp $Message" -Encoding UTF8
}

function Remove-StopRequest {
    if (Test-Path -LiteralPath $stopFile) {
        Remove-Item -LiteralPath $stopFile -Force
        return $true
    }
    return $false
}

if (-not (Test-Path -LiteralPath $python)) {
    Write-WrapperLog "missing $python; run deploy\windows\install.ps1 first"
    exit 1
}

# Process-scoped settings for the collector. Nothing is written to the registry.
$env:PMARB_DATA_DIR = $DataDir
$env:PMARB_LOG_DIR = $logDir
$env:PMARB_ENV_FILE = $EnvFile
Set-Location -LiteralPath $RepoPath

while ($true) {
    foreach ($log in @($stdoutLog, $stderrLog)) {
        if (Test-Path -LiteralPath $log) {
            Move-Item -LiteralPath $log -Destination "$log.prev" -Force
        }
    }
    Write-WrapperLog 'starting pmarb scan'
    $process = Start-Process -FilePath $python -ArgumentList @('-m', 'pmarb.service') `
        -WorkingDirectory $RepoPath -NoNewWindow -PassThru `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    # Reading Handle keeps the process handle open so ExitCode is available after exit.
    $null = $process.Handle
    $process.WaitForExit()
    $code = $process.ExitCode
    Write-WrapperLog "pmarb scan exited with code $code"
    if ($code -eq 0) {
        Write-WrapperLog 'clean stop; not restarting'
        exit 0
    }
    Write-WrapperLog "restarting in $RestartDelaySeconds s; see $stderrLog"
    for ($waited = 0; $waited -lt $RestartDelaySeconds; $waited++) {
        if (Remove-StopRequest) {
            Write-WrapperLog 'stop requested while waiting to restart; exiting'
            exit 0
        }
        Start-Sleep -Seconds 1
    }
}
