# The film drive index (SRD 8.2, 13): every night from 00:00, stopping before 06:00. It does
# nothing when no registered film drive is connected, and resumes where it stopped.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'JobCommon.psm1') -Force
$null = Start-JobLog -Name 'index-films'
$code = 0
try {
    $code = Invoke-Ce -Arguments @('index-films', '--until', '06:00')
} finally {
    Stop-JobLog
}
exit $code
