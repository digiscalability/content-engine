# Library ingest (SRD 13, FR-ING-1, FR-ING-5, FR-ING-6): every night at 02:00, stock clips for
# the beats scriptwriters queued are fetched first, then the folders in the sources config and
# the music pool are scanned, hashed and indexed in place. Unchanged files are skipped, so a
# quiet night costs a directory walk. Exits with the first failing step's code.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'JobCommon.psm1') -Force
$null = Start-JobLog -Name 'ingest'
$code = 0
try {
    $stock = Invoke-Ce -Arguments @('stock', 'fetch')
    $ingest = Invoke-Ce -Arguments @('ingest')
    $code = @($stock, $ingest) | Where-Object { $_ -ne 0 } | Select-Object -First 1
    if ($null -eq $code) { $code = 0 }
} finally {
    Stop-JobLog
}
exit $code
