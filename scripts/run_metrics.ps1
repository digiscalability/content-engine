# Every 6 h (SRD 13, FR-LRN-1): follow drafted posts through Postiz and YouTube, then take
# the 24 h, 72 h and 7 d metrics snapshots that have come due. Metrics run even when
# reconciliation fails. Exits with the first failing step's code.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'JobCommon.psm1') -Force
$null = Start-JobLog -Name 'metrics'
$code = 0
try {
    $reconcile = Invoke-Ce -Arguments @('reconcile')
    $metrics = Invoke-Ce -Arguments @('metrics')
    $code = if ($reconcile -ne 0) { $reconcile } else { $metrics }
} finally {
    Stop-JobLog
}
exit $code
