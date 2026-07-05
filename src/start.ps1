param(
    [switch]$SkipInstall,
    [switch]$SkipFrontend
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root '.venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$PidFile = Join-Path $Root 'logs\server.pid'

New-Item -ItemType Directory -Force -Path (Join-Path $Root 'data'), (Join-Path $Root 'logs') | Out-Null
if (-not (Test-Path (Join-Path $Root 'data\password.txt'))) {
    @('# 格式: username:password:role  (role 取值: admin | user)', 'admin:admin123:admin') | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'data\password.txt')
}
if (Test-Path $PidFile) {
    $ExistingPid = [int](Get-Content -Raw -LiteralPath $PidFile)
    if (Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue) {
        Write-Output "服务已运行，PID=$ExistingPid"
        exit 0
    }
    Remove-Item -LiteralPath $PidFile -Force
}
if (-not (Test-Path $Python)) { python -m venv $Venv }
if (-not $SkipInstall) { & $Python -m pip install -r (Join-Path $Root 'requirements.txt') }

$Frontend = Join-Path $Root 'app\frontend'
if (-not $SkipFrontend -and -not (Test-Path (Join-Path $Frontend 'dist\index.html')) -and (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    Push-Location $Frontend
    try {
        if (Test-Path 'package-lock.json') { npm.cmd ci } else { npm.cmd install }
        npm.cmd run build
    } finally { Pop-Location }
}

$env:PYTHONPATH = $Root
$Process = Start-Process -FilePath $Python -ArgumentList @('-m', 'app.backend.main') -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Root 'logs\server.stdout.log') -RedirectStandardError (Join-Path $Root 'logs\server.stderr.log')
$Process.Id | Set-Content -Encoding ASCII -LiteralPath $PidFile
Start-Sleep -Seconds 2
if (-not (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue)) {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    throw '服务启动失败，请检查 logs/server.stderr.log'
}
Write-Output "服务已启动，PID=$($Process.Id)"
