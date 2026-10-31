# Сборка одного файла dist\AmneziaMonitor.exe (Windows, PowerShell 5.1+).
# Запуск из папки проекта:
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
# Ключи: -Clean (пересоздать .venv-build), -Test (прогнать тесты перед сборкой),
#        -Python "C:\Path\python.exe" (явно указать Python 3.9+).
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Test,
    [string]$Python = ""
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

function Step([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }

# Запуск внешней программы с проверкой кода возврата (stderr НЕ перенаправляем: в PS 5.1 это даёт ложные ошибки)
function Invoke-Exe {
    param([Parameter(Mandatory = $true)][string]$Exe, [string[]]$ArgList = @())
    & $Exe @ArgList
    if ($LASTEXITCODE -ne 0) { throw "Команда завершилась с кодом ${LASTEXITCODE}: $Exe $($ArgList -join ' ')" }
}

# Проверяет кандидата: это настоящий Python 3.9+ (а не заглушка Microsoft Store)?
function Test-Python {
    param([string]$Exe, [string[]]$Pre = @())
    try {
        $out = & $Exe @Pre -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)"
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $lines = @($out)
        $ver = [version]$lines[0].Trim()
        if ($ver -lt [version]'3.9') { Write-Host "  пропускаю Python $ver ($Exe): нужен 3.9+"; return $null }
        return @{ Version = $ver; Path = $lines[1].Trim() }
    } catch { return $null }
}

Step "Поиск Python 3.9+"
$found = $null
if ($Python) {
    $found = Test-Python -Exe $Python
    if (-not $found) { throw "Указанный Python не подходит: $Python" }
} else {
    if (Get-Command py -ErrorAction SilentlyContinue) { $found = Test-Python -Exe 'py' -Pre @('-3') }
    if (-not $found -and (Get-Command python -ErrorAction SilentlyContinue)) { $found = Test-Python -Exe 'python' }
    if (-not $found -and (Get-Command python3 -ErrorAction SilentlyContinue)) { $found = Test-Python -Exe 'python3' }
}
if (-not $found) {
    throw "Python 3.9+ не найден. Установите его с https://www.python.org/downloads/windows/ (галочка 'Add python.exe to PATH') и запустите скрипт снова."
}
$BasePy = $found.Path
Write-Host "  Python $($found.Version): $BasePy"

$Venv = Join-Path $Root '.venv-build'
$VPy = Join-Path $Venv 'Scripts\python.exe'
if ($Clean -and (Test-Path -LiteralPath $Venv)) {
    Step "Удаляю старое окружение .venv-build"
    Remove-Item -LiteralPath $Venv -Recurse -Force
}
if (-not (Test-Path -LiteralPath $VPy)) {
    Step "Создаю виртуальное окружение .venv-build"
    Invoke-Exe $BasePy @('-m', 'venv', $Venv)
}

Step "Устанавливаю зависимости (paramiko, PyYAML, requests, pystray, Pillow, PyInstaller)"
Invoke-Exe $VPy @('-m', 'pip', 'install', '--upgrade', 'pip')
Invoke-Exe $VPy @('-m', 'pip', 'install', '-r', (Join-Path $Root 'requirements-app.txt'), 'pyinstaller>=6.6')

if ($Test) {
    Step "Тесты"
    Invoke-Exe $VPy @('-m', 'unittest', 'discover', '-s', (Join-Path $Root 'tests'))
}

Step "Сборка PyInstaller (один файл, без консоли)"
foreach ($d in @('build', 'dist')) {
    $p = Join-Path $Root $d
    if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
}
Invoke-Exe $VPy @('-m', 'PyInstaller', '--noconfirm', '--clean', (Join-Path $Root 'AmneziaMonitor.spec'))

$Exe = Join-Path $Root 'dist\AmneziaMonitor.exe'
if (-not (Test-Path -LiteralPath $Exe)) { throw "Сборка завершилась, но файл не найден: $Exe" }
$size = [math]::Round((Get-Item -LiteralPath $Exe).Length / 1MB, 1)

Write-Host ""
Write-Host "Готово! Файл программы ($size МБ):" -ForegroundColor Green
Write-Host (Resolve-Path -LiteralPath $Exe).Path -ForegroundColor Green
Write-Host "Запуск: двойной щелчок. Настройки и база: $env:APPDATA\AmneziaMonitor"
