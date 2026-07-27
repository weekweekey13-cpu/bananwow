# BANANWOW keep-alive — пингует Render, чтобы free-инстанс не засыпал
$ErrorActionPreference = "Continue"
$Url = "https://bananwow.onrender.com/api/ping"
$LogDir = Join-Path $PSScriptRoot "..\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
$Log = Join-Path $LogDir "keepalive.log"

function Write-Log($msg) {
  $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $msg
  Add-Content -Path $Log -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}

try {
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 110 -Headers @{ "User-Agent" = "bananwow-windows-keepalive/1.0" }
  $sw.Stop()
  Write-Log ("OK HTTP {0} in {1}ms body={2}" -f $resp.StatusCode, $sw.ElapsedMilliseconds, $resp.Content.Substring(0, [Math]::Min(120, $resp.Content.Length)))
  exit 0
} catch {
  Write-Log ("FAIL {0}" -f $_.Exception.Message)
  # one retry after cold-start wait
  Start-Sleep -Seconds 20
  try {
    $resp2 = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 110 -Headers @{ "User-Agent" = "bananwow-windows-keepalive/1.0" }
    Write-Log ("OK-retry HTTP {0}" -f $resp2.StatusCode)
    exit 0
  } catch {
    Write-Log ("FAIL-retry {0}" -f $_.Exception.Message)
    exit 1
  }
}
