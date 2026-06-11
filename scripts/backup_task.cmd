@echo off
rem Backup diário do cache (mantém os últimos 14)
cd /d "%~dp0.."
".venv\Scripts\python.exe" -B "scripts\backup_cache.py" >> "data\backup_task.log" 2>&1
