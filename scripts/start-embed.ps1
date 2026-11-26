# start-embed.ps1 — поднять embedding-сервер LEX на :8081 (bge-m3, CPU).
#
# Запускает scripts/embed_server.py, ждёт готовности /health (модель грузится
# ~5-20 с), печатает понятное сообщение. Оставляет сервер в этом окне (Ctrl+C —
# остановить). Для фона: -Background (вернёт управление сразу после health).
#
#   .\scripts\start-embed.ps1
#   .\scripts\start-embed.ps1 -Background
param(
    [int]$Port = 8081,
    [string]$Model = 'D:\models\bge-m3',
    [string]$BindHost = '127.0.0.1',
    [switch]$Background,
    [int]$WaitSec = 120
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here '_python.ps1')

$found = Find-LexPython
if (-not $found) {
    Write-Error "не найден Python 3.12+ с fastapi и sentence-transformers. Задайте LEX_PYTHON=<путь к python.exe>."
    exit 1
}
$py = $found.Exe
$pyArgs = @()
Write-Host "  python: $py ($($found.Version))"

function Test-Health([int]$p) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$p/health" -TimeoutSec 3
        if ($r.StatusCode -eq 200) { return $r.Content }
    } catch { }
    return $null
}

$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $h = Test-Health $Port
    if ($h) {
        Write-Host "порт $Port уже занят — сервер, похоже, жив:" -ForegroundColor Yellow
        Write-Host "  $h"
        exit 0
    }
    Write-Error "порт $Port занят чужим процессом (pid $($busy.OwningProcess)). Освободите порт."
    exit 1
}

if (-not (Test-Path (Join-Path $Model 'config.json'))) {
    Write-Error "нет модели в $Model (нет config.json). Сначала: python scripts/_download_models.py embed"
    exit 1
}

Write-Host "LEX embed: старт на http://${BindHost}:$Port (bge-m3, CPU)" -ForegroundColor Cyan
Write-Host "  модель: $Model"
Write-Host "  ожидание готовности (до $WaitSec с)…"

$scriptPath = Join-Path $here 'embed_server.py'
$argList = $pyArgs + @('-I', $scriptPath, '--port', $Port, '--host', $BindHost, '--model', $Model)

if ($Background) {
    $proc = Start-Process -FilePath $py -ArgumentList $argList -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $env:TEMP 'lex-embed.out.log') `
        -RedirectStandardError (Join-Path $env:TEMP 'lex-embed.err.log')
    $deadline = (Get-Date).AddSeconds($WaitSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        $h = Test-Health $Port
        if ($h) {
            Write-Host "ГОТОВО: http://127.0.0.1:$Port/v1/embeddings  (pid $($proc.Id))" -ForegroundColor Green
            Write-Host "  $h"
            exit 0
        }
        if ($proc.HasExited) {
            Write-Error "процесс завершился (код $($proc.ExitCode)). Лог: $env:TEMP\lex-embed.err.log"
            exit 1
        }
    }
    Write-Error "не дождались /health за $WaitSec с. Лог: $env:TEMP\lex-embed.err.log"
    exit 1
}

# foreground: сервер в этом окне
& $py @argList
exit $LASTEXITCODE
