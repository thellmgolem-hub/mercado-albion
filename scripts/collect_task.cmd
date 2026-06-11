@echo off
rem Coleta agendada da watchlist (pula se o servidor coletou há pouco)
cd /d "%~dp0.."
".venv\Scripts\python.exe" -B analyze.py collect --skip-if-recent 25 >> "data\collect_task.log" 2>&1
