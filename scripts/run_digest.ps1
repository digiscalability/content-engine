# The morning repeat (SRD 13): rebuild today's digest and notify again while anything waits.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'JobCommon.psm1') -Force
$null = Start-JobLog -Name 'digest'
$code = 0
try {
    $code = Invoke-Ce -Arguments @('digest')
} finally {
    Stop-JobLog
}
exit $code
