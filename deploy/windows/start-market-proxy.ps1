param(
    [string]$StateDirectory = (Join-Path $env:USERPROFILE '.prediction-trader-deploy'),
    [string]$Python = '',
    [int]$Port = 18866
)
$ErrorActionPreference = 'Stop'
if (-not $Python) { $Python = Join-Path $StateDirectory 'python\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw "Python not found: $Python. Pass -Python with a Windows Python 3 path." }
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listener) {
    $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
    $body = if ($health.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($health.Content) } else { [string]$health.Content }
    if ($body -ne 'OK') { throw "Port $Port is occupied by another service." }
    Write-Output "Prediction Trader relay already listening on 127.0.0.1:$Port"
    exit 0
}
New-Item -ItemType Directory -Path $StateDirectory -Force | Out-Null
$source = Join-Path $PSScriptRoot 'market_proxy.py'
$script = Join-Path $StateDirectory 'market_proxy.py'
Copy-Item -LiteralPath $source -Destination $script -Force
$process = Start-Process -FilePath $Python -ArgumentList @('"' + $script + '"', '--port', $Port) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $StateDirectory 'market-proxy.out.log') -RedirectStandardError (Join-Path $StateDirectory 'market-proxy.err.log')
$process.Id | Set-Content -LiteralPath (Join-Path $StateDirectory 'market-proxy.pid')
Start-Sleep -Seconds 1
Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 | Out-Null
Write-Output "Prediction Trader relay started on 127.0.0.1:$Port (PID $($process.Id))"
