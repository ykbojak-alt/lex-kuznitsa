# _python.ps1 — найти рабочий Python 3.14+ со стеком LEX (fastapi, sentence-transformers).
#
# Не полагаемся на `py -3.14`: лаунчер в этой системе указывает на несуществующий
# C:\Python314. Перебираем кандидатов и возвращаем первый, где импорты проходят.
# Дот-сорсится из start-embed.ps1 / start-rerank.ps1.
function Find-LexPython {
    $candidates = New-Object System.Collections.Generic.List[string]

    if ($env:LEX_PYTHON) { $candidates.Add($env:LEX_PYTHON) }

    $local = Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.14-64\python.exe'
    if (Test-Path $local) { $candidates.Add($local) }

    foreach ($name in @('python3', 'python')) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c -and $c.Source -notlike '*WindowsApps*') { $candidates.Add($c.Source) }
    }
    # WindowsApps-шим тоже годится, если за ним настоящий питон
    $shim = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\python3.exe'
    if (Test-Path $shim) { $candidates.Add($shim) }

    foreach ($exe in $candidates) {
        if (-not $exe -or -not (Test-Path $exe)) { continue }
        $probe = & $exe -I -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $probe) { continue }
        $parts = $probe.Trim().Split('.')
        if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 12)) { continue }
        & $exe -I -c "import fastapi, sentence_transformers" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Exe = $exe; Version = $probe.Trim() }
        }
    }
    return $null
}
