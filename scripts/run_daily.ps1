# The 04:00 job (SRD 13): one production pass, reconciliation with Postiz, then the digest.
# Reconciliation and the digest run even when the pass fails, so the failure reaches Abbas in
# the report and the notification. Exits with the first failing step's code.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'JobCommon.psm1') -Force
$null = Start-JobLog -Name 'daily'
$code = 0
try {
    $daily = Invoke-Ce -Arguments @('daily')
    $reconcile = Invoke-Ce -Arguments @('reconcile')
    $digest = Invoke-Ce -Arguments @('digest')
    $code = @($daily, $reconcile, $digest) | Where-Object { $_ -ne 0 } | Select-Object -First 1
    if ($null -eq $code) { $code = 0 }
} finally {
    Stop-JobLog
}
exit $code
