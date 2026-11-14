# JobCommon.psm1 - shared helpers for the content-engine Task Scheduler wrappers (SRD 13).
# PowerShell 5.1 compatible, ASCII only. Conventions copied from ventures-ops (SRD 2.3):
# one log per job per day under %LOCALAPPDATA%\content-engine\logs, older logs pruned.
#
# Tests point CE_LOG_DIR at a scratch folder and CE_UV at a fake uv.

$script:RepoRoot = Split-Path -Parent $PSScriptRoot

function Get-LogDir {
    if ($env:CE_LOG_DIR) { return $env:CE_LOG_DIR }
    return (Join-Path $env:LOCALAPPDATA 'content-engine\logs')
}

# One transcript per job per day, appended on every run. Logs of the same job older than
# 30 days are removed on start, so the folder stays bounded without a cleanup job.
function Start-JobLog {
    param([Parameter(Mandatory = $true)][string]$Name)
    $dir = Get-LogDir
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    Get-ChildItem -Path $dir -Filter ("{0}-*.log" -f $Name) -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    $log = Join-Path $dir ("{0}-{1}.log" -f $Name, (Get-Date -Format 'yyyy-MM-dd'))
    try { Start-Transcript -Path $log -Append | Out-Null } catch { }
    return $log
}

function Stop-JobLog {
    try { Stop-Transcript | Out-Null } catch { }
}

# Runs `uv run ce <args>` from the repo root. In Windows PowerShell 5.1, stderr from a
# native command becomes a terminating error under ErrorActionPreference Stop, so run with
# Continue and judge success by the exit code. Output goes to the transcript; ce never
# prints a secret (NFR-7). A missing uv returns 127 with a log line, and an exit code is
# never carried over from an earlier step, so nothing that did not run reads as success.
function Invoke-Ce {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $uv = if ($env:CE_UV) { $env:CE_UV } else { 'uv' }
    $ErrorActionPreference = 'Continue'
    Write-Host ("[{0}] ce {1}" -f (Get-Date -Format 'HH:mm:ss'), ($Arguments -join ' '))
    if (-not (Get-Command $uv -ErrorAction SilentlyContinue)) {
        Write-Host ("[{0}] ce {1}: uv not found ({2}); exit 127" -f (Get-Date -Format 'HH:mm:ss'), $Arguments[0], $uv)
        return 127
    }
    $global:LASTEXITCODE = $null
    Push-Location $script:RepoRoot
    try {
        & $uv run ce @Arguments 2>&1 | ForEach-Object { "$_" } | Out-Host
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($null -eq $code) { $code = 127 }
    Write-Host ("[{0}] ce {1}: exit {2}" -f (Get-Date -Format 'HH:mm:ss'), $Arguments[0], $code)
    return $code
}

Export-ModuleMember -Function Get-LogDir, Start-JobLog, Stop-JobLog, Invoke-Ce
