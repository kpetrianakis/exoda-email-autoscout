$ErrorActionPreference = 'Stop'

# Repo root is derived from this script's location, so the checkout can
# live anywhere.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "run-$timestamp.log"

# Keep only the newest 50 logs so this directory doesn't grow unbounded.
Get-ChildItem $logDir -Filter '*.log' | Sort-Object LastWriteTime -Descending | Select-Object -Skip 50 | Remove-Item -Force -ErrorAction SilentlyContinue

$configPath = Join-Path $root 'invoices\_config.json'
$config = $null
try {
    $config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Add-Content -Path $logFile -Value "Could not read invoices/_config.json: $($_.Exception.Message)"
}

# --- Reconcile the Task Scheduler trigger against invoices/_config.json ---
try {
    if ($config) {
        $desiredInterval = [int]$config.run_interval_days
        $task = Get-ScheduledTask -TaskName 'InvoiceSync' -ErrorAction SilentlyContinue
        if ($task) {
            $currentInterval = [int]$task.Triggers[0].DaysInterval
            if ($currentInterval -ne $desiredInterval) {
                $existingTime = ([datetime]$task.Triggers[0].StartBoundary).ToString('HH:mm')
                $newTrigger = New-ScheduledTaskTrigger -Daily -At $existingTime -DaysInterval $desiredInterval
                Set-ScheduledTask -TaskName 'InvoiceSync' -Trigger $newTrigger | Out-Null
                Add-Content -Path $logFile -Value "Updated InvoiceSync schedule interval: $currentInterval -> $desiredInterval days"
            }
        }
    }
} catch {
    Add-Content -Path $logFile -Value "Schedule reconciliation skipped: $($_.Exception.Message)"
}

# --- Run the sync ---
# This is a plain deterministic script: no model, no API cost, no tool
# permissions, and the same input always produces the same output.
$env:PYTHONIOENCODING = 'utf-8'
# Without this, PowerShell decodes the child process's UTF-8 stdout using the
# OEM code page and the Greek subject keywords land in the log as mojibake.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$output = & python (Join-Path $root 'scripts\sync_invoices.py') 2>&1
$exitCode = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

Add-Content -Path $logFile -Value $output
Add-Content -Path $logFile -Value "`nExit code: $exitCode"

if ($exitCode -ne 0) {
    # Best-effort failure notification. Without this, a broken run stays
    # silent until someone happens to look 15 days later.
    try {
        $tail = ($output | Select-Object -Last 40) -join "`n"
        $failPayload = [ordered]@{
            to       = @($config.notify_email)
            subject  = "Invoice sync FAILED - $timestamp"
            body     = $tail
            htmlBody = '<pre style="font-family: Consolas, monospace; font-size: 12px; white-space: pre-wrap;">' + [System.Net.WebUtility]::HtmlEncode($tail) + '</pre>'
        }
        $failPayload | ConvertTo-Json -Depth 10 -Compress | & python (Join-Path $root 'scripts\send_summary_email.py') *>> $logFile
    } catch {
        Add-Content -Path $logFile -Value "Failure notification also failed: $($_.Exception.Message)"
    }
}

exit $exitCode
