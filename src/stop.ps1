$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root 'logs\server.pid'
if (-not (Test-Path $PidFile)) { Write-Output '服务未运行'; exit 0 }
$ServerPid = [int](Get-Content -Raw -LiteralPath $PidFile)
$Process = Get-Process -Id $ServerPid -ErrorAction SilentlyContinue
if ($Process) {
    Stop-Process -Id $ServerPid
    $Process.WaitForExit(5000) | Out-Null
}
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
Write-Output "服务已停止，PID=$ServerPid"
