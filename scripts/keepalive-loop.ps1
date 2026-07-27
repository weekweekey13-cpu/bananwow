# BANANWOW keep-alive loop — каждые 4 минуты, пока процесс жив
$ErrorActionPreference = "Continue"
$Url = "https://bananwow.onrender.com/api/ping"
$IntervalSec = 240
$LogDir = Join-Path $PSScriptRoot "..\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
$Log = Join-Path $LogDir "keepalive-loop.log"

function Write-Log($msg) {
  $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $msg
  Add-Content -Path $Log -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
  Write-Host $line
}

Write-Log "START keep-alive loop interval=${IntervalSec}s url=$Url"
while ($true) {
  try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 110 -Headers @{ "User-Agent" = "bananwow-loop-keepalive/1.0" }
    $sw.Stop()
    Write-Log ("OK HTTP {0} {1}ms" -f $resp.StatusCode, $sw.ElapsedMilliseconds)
  } catch {
    Write-Log ("FAIL {0}" -f $_.Exception.Message)
  }
  Start-Sleep -Seconds $IntervalSec
}
