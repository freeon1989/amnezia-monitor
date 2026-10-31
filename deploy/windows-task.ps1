# Windows (например, ПК NAKO): регистрирует задачу Планировщика, которая запускает `check` каждую минуту.
# Запуск: PowerShell от имени администратора, из папки amnezia-monitor:
#   powershell -ExecutionPolicy Bypass -File deploy\windows-task.ps1            (только мониторинг)
#   powershell -ExecutionPolicy Bypass -File deploy\windows-task.ps1 -Dashboard (+ веб-панель при входе)
param([switch]$Dashboard)
$Dir = (Resolve-Path "$PSScriptRoot\..").Path
$Py  = Join-Path $Dir ".venv\Scripts\pythonw.exe"
$Arg = "`"$Dir\amnezia_monitor.py`" -c `"$Dir\servers.yaml`" check"
$Action  = New-ScheduledTaskAction -Execute $Py -Argument $Arg -WorkingDirectory $Dir
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
$Set     = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable
Register-ScheduledTask -TaskName "AmneziaMonitor" -Action $Action -Trigger $Trigger -Settings $Set -Description "AmneziaVPN read-only monitor" -Force
$Sum = New-ScheduledTaskAction -Execute $Py -Argument "`"$Dir\amnezia_monitor.py`" -c `"$Dir\servers.yaml`" summary --hours 24 --send" -WorkingDirectory $Dir
Register-ScheduledTask -TaskName "AmneziaMonitorSummary" -Action $Sum -Trigger (New-ScheduledTaskTrigger -Daily -At 9am) -Force
# Необязательно: веб-панель при входе в Windows (http://127.0.0.1:8765). Запуск скрипта с ключом -Dashboard
if ($Dashboard) {
    $Dash = New-ScheduledTaskAction -Execute $Py -Argument "`"$Dir\amnezia_monitor.py`" -c `"$Dir\servers.yaml`" dashboard" -WorkingDirectory $Dir
    $DSet = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    Register-ScheduledTask -TaskName "AmneziaDashboard" -Action $Dash -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Settings $DSet -Description "AmneziaVPN monitor web dashboard (localhost)" -Force
    Write-Host "Задача AmneziaDashboard создана: панель http://127.0.0.1:8765 после входа в систему."
}
Write-Host "Готово. Задачи AmneziaMonitor и AmneziaMonitorSummary созданы."
