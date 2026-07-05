$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root 'logs\server.pid'
if (-not (Test-Path $PidFile)) { Write-Output 'STOPPED'; exit 1 }
$ServerPid = [int](Get-Content -Raw -LiteralPath $PidFile)
if (-not (Get-Process -Id $ServerPid -ErrorAction SilentlyContinue)) { Write-Output "STALE PID=$ServerPid"; exit 1 }
try {
    $Config = Get-Content -Raw -LiteralPath (Join-Path $Root 'config\app.json') | ConvertFrom-Json
    $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$($Config.server.port)/api/v1/health/ready" -TimeoutSec 3
    Write-Output "RUNNING PID=$ServerPid STATUS=$($Health.status)"
} catch {
    Write-Output "RUNNING PID=$ServerPid HEALTH=UNAVAILABLE"
    exit 2
}
