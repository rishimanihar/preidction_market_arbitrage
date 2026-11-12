#Requires -Version 5.1
<#
.SYNOPSIS
Stops the pmarb collector cleanly and waits up to 90 seconds for it to finish.

.DESCRIPTION
Runs `pmarb stop --wait`, which writes <DataDir>\collector.stop. The collector checks
for that file once a second, then shuts down: it closes open episodes with
collector_shutdown, drains the raw log and the database, deletes the file, and exits 0.
The scheduled task's wrapper then exits too. Signals are not needed.

Paths default to the ones the registered task "pmarb-collector" uses, so this works
even when install.ps1 ran with a custom -DataDir.

Exit code 0: stopped cleanly, or nothing was running. Exit code 1: not confirmed in time.

Do not use "End" in Task Scheduler unless this script fails. It kills the process
without draining. Open episodes are then closed as collector_restart on the next start.

.PARAMETER RepoPath
Checkout root. Default: from the registered task, else two levels above this script.

.PARAMETER DataDir
Data directory. Default: from the registered task, else <RepoPath>\data.

.PARAMETER TimeoutSeconds
How long to wait for the drain. Default 90.
#>
[CmdletBinding()]
param(
    [string]$RepoPath,
    [string]$DataDir,
    [int]$TimeoutSeconds = 90
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$TaskName = 'pmarb-collector'

function Get-TaskArgument([object]$Task, [string]$Name) {
    if ($null -eq $Task) {
        return $null
    }
    $arguments = [string](($Task.Actions | Select-Object -First 1).Arguments)
    $match = [regex]::Match($arguments, '-' + $Name + '\s+"([^"]+)"')
    if ($match.Success) {
        return $match.Groups[1].Value
    }
    return $null
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $RepoPath) {
    $RepoPath = Get-TaskArgument $task 'RepoPath'
}
if (-not $RepoPath) {
    $RepoPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
}
if (-not $DataDir) {
    $DataDir = Get-TaskArgument $task 'DataDir'
}
if (-not $DataDir) {
    $DataDir = Join-Path $RepoPath 'data'
}
$envFile = Get-TaskArgument $task 'EnvFile'
if (-not $envFile) {
    $envFile = Join-Path $env:USERPROFILE '.config\pmarb\.env'
}
$pmarb = Join-Path $RepoPath '.venv\Scripts\pmarb.exe'

if ($null -ne $task -and $task.State -ne 'Running') {
    Write-Host "OK task $TaskName is not running (state: $($task.State)); nothing to stop."
    exit 0
}
if ($null -eq $task) {
    Write-Host "Task $TaskName is not registered; asking any collector that uses $DataDir to stop."
}
if (-not (Test-Path -LiteralPath $pmarb)) {
    Write-Warning "Missing $pmarb; run deploy\windows\install.ps1 first."
    exit 1
}

$started = Get-Date
$env:PMARB_DATA_DIR = $DataDir
$env:PMARB_ENV_FILE = $envFile
# Merge stderr as text: Windows PowerShell 5.1 would turn it into fatal error records.
$ErrorActionPreference = 'Continue'
& $pmarb stop --wait $TimeoutSeconds 2>&1 | ForEach-Object { Write-Host "$_" }
$stopCode = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($stopCode -ne 0) {
    Write-Warning "The collector did not confirm the stop within $TimeoutSeconds s. Check $DataDir\logs\pmarb.log. Only if it is stuck, end the task: Stop-ScheduledTask -TaskName $TaskName"
    exit 1
}

if ($null -ne $task) {
    # The wrapper exits right after the collector; give it the rest of the budget.
    while ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') {
        if (((Get-Date) - $started).TotalSeconds -ge $TimeoutSeconds) {
            Write-Warning "The collector drained, but task $TaskName still shows Running after $TimeoutSeconds s."
            exit 1
        }
        Start-Sleep -Seconds 1
    }
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Host "OK collector stopped cleanly; task state: $state"
} else {
    Write-Host 'OK collector stopped cleanly'
}
exit 0
