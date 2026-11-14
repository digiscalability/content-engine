<#
.SYNOPSIS
  Registers the content-engine jobs (SRD 13) in Windows Task Scheduler.

.DESCRIPTION
  Dry run by default: prints what it would register and changes nothing. Registering is
  -Apply, and only after Abbas says yes in chat (SRD 13). -Describe prints the task
  definitions as JSON without registering; -Unregister removes them.

  Every task wakes the computer to run and runs as soon as possible after a missed start,
  so a sleeping PC still runs it. conhost --headless keeps the console hidden. Jobs whose
  wrapper does not exist yet are skipped with a warning.

.EXAMPLE
  .\scripts\register-tasks.ps1                       # dry run
  .\scripts\register-tasks.ps1 -Describe              # task definitions as JSON
  .\scripts\register-tasks.ps1 -Apply                 # register (after Abbas's yes)
  .\scripts\register-tasks.ps1 -Apply -Only ContentEngine-Daily
  .\scripts\register-tasks.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Describe,
    [switch]$Unregister,
    [string[]]$Only
)

$ErrorActionPreference = 'Stop'
$scripts = $PSScriptRoot
$taskPath = '\ContentEngine\'

# Name, wrapper, schedule (Melbourne local time), from SRD 13.
$jobs = @(
    @{ Name = 'ContentEngine-IndexFilms'; Script = 'run_index_films.ps1'; Kind = 'Daily'; At = @('00:00') },
    @{ Name = 'ContentEngine-Ingest';  Script = 'run_ingest.ps1';  Kind = 'Daily'; At = @('02:00') },
    @{ Name = 'ContentEngine-Daily';   Script = 'run_daily.ps1';   Kind = 'Daily'; At = @('04:00') },
    @{ Name = 'ContentEngine-Digest';  Script = 'run_digest.ps1';  Kind = 'Daily'; At = @('08:00') },
    @{ Name = 'ContentEngine-Metrics'; Script = 'run_metrics.ps1'; Kind = 'Every6Hours'; At = @('00:30') },
    @{ Name = 'ContentEngine-Weekly';  Script = 'run_weekly.ps1';  Kind = 'Weekly'; At = @('20:00'); Day = 'Sunday' }
)
if ($Only) { $jobs = $jobs | Where-Object { $Only -contains $_.Name } }

function New-JobTriggers($job) {
    $first = [datetime]::ParseExact($job.At[0], 'HH:mm', $null)
    if ($job.Kind -eq 'Every6Hours') {
        return @(New-ScheduledTaskTrigger -Once -At $first -RepetitionInterval (New-TimeSpan -Hours 6))
    }
    if ($job.Kind -eq 'Weekly') {
        return @(New-ScheduledTaskTrigger -Weekly -DaysOfWeek $job.Day -At $first)
    }
    return @($job.At | ForEach-Object { New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($_, 'HH:mm', $null)) })
}

function New-JobTask($job, $scriptPath, $when) {
    $arguments = '--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $scriptPath
    $action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument $arguments -WorkingDirectory (Split-Path $scripts -Parent)
    # NFR-2: a daily run finishes within 2 h; the limit leaves room for a GPU wait.
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    return New-ScheduledTask -Action $action -Trigger (New-JobTriggers $job) -Settings $settings `
        -Principal $principal -Description "content-engine job ($when). Source: $scriptPath"
}

$described = @()
foreach ($job in $jobs) {
    $scriptPath = Join-Path $scripts $job.Script
    $when = switch ($job.Kind) {
        'Every6Hours' { 'every 6 hours from ' + $job.At[0] }
        'Weekly' { "$($job.Day)s at " + $job.At[0] }
        default { 'daily at ' + ($job.At -join ', ') }
    }

    if ($Unregister) {
        $existing = Get-ScheduledTask -TaskPath $taskPath -TaskName $job.Name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskPath $taskPath -TaskName $job.Name -Confirm:$false
            Write-Host "removed  $($job.Name)"
        } else {
            Write-Host "absent   $($job.Name)"
        }
        continue
    }

    if (-not (Test-Path $scriptPath)) {
        Write-Host ("skip {0}: {1} does not exist yet" -f $job.Name, $job.Script)
        continue
    }

    $task = New-JobTask $job $scriptPath $when
    if ($Describe) {
        $described += [pscustomobject]@{
            Name               = $job.Name
            At                 = @($job.At)
            Kind               = $job.Kind
            Execute            = $task.Actions[0].Execute
            Arguments          = $task.Actions[0].Arguments
            WakeToRun          = [bool]$task.Settings.WakeToRun
            StartWhenAvailable = [bool]$task.Settings.StartWhenAvailable
        }
        continue
    }
    if (-not $Apply) {
        Write-Host ("would register {0,-22} {1,-26} -> {2}" -f $job.Name, $when, $scriptPath)
        continue
    }
    Register-ScheduledTask -TaskPath $taskPath -TaskName $job.Name -InputObject $task -Force | Out-Null
    Write-Host "registered $($job.Name) ($when)"
}

if ($Describe) {
    ConvertTo-Json -InputObject @($described) -Depth 4
} elseif (-not $Apply -and -not $Unregister) {
    Write-Host ''
    Write-Host 'Dry run only. Re-run with -Apply to register these tasks (after Abbas says yes).'
}
