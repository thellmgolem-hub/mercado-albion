@echo off
chcp 65001 >nul
rem Registra (ou remove, com "remover") as tarefas agendadas do Mercado Albion:
rem   MercadoAlbion-Coleta : coleta a watchlist a cada 30 min, janela oculta
rem   MercadoAlbion-Backup : backup diário do cache às 04:00 (mantém 14)
cd /d "%~dp0"
if /i "%1"=="remover" (
  schtasks /Delete /TN "MercadoAlbion-Coleta" /F
  schtasks /Delete /TN "MercadoAlbion-Backup" /F
  echo Tarefas removidas.
  pause
  exit /b 0
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = Split-Path -Parent '%~dp0'; " ^
  "$vbs = Join-Path $root 'scripts\run_hidden.vbs'; " ^
  "$c = Join-Path $root 'scripts\collect_task.cmd'; " ^
  "$b = Join-Path $root 'scripts\backup_task.cmd'; " ^
  "$a1 = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('\"{0}\" \"{1}\"' -f $vbs, $c); " ^
  "$t1 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 30); " ^
  "Register-ScheduledTask -TaskName 'MercadoAlbion-Coleta' -Action $a1 -Trigger $t1 -Description 'Coleta watchlist do Mercado Albion a cada 30 min' -Force | Out-Null; " ^
  "$a2 = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('\"{0}\" \"{1}\"' -f $vbs, $b); " ^
  "$t2 = New-ScheduledTaskTrigger -Daily -At '04:00'; " ^
  "Register-ScheduledTask -TaskName 'MercadoAlbion-Backup' -Action $a2 -Trigger $t2 -Description 'Backup diario do cache do Mercado Albion' -Force | Out-Null; " ^
  "Get-ScheduledTask -TaskName 'MercadoAlbion-*' | Select-Object TaskName, State"
echo.
echo Tarefas registradas. Para desfazer: instalar_tarefas.cmd remover
pause
