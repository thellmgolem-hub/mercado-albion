@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="

if exist ".venv\Scripts\python.exe" (
  set "PYTHON_CMD=.venv\Scripts\python.exe"
)

if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  where py >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
  echo Python nao encontrado.
  echo Instale Python 3.11+ ou crie um ambiente em .venv e tente novamente.
  pause
  exit /b 1
)

%PYTHON_CMD% -c "import fastapi, uvicorn, httpx" >nul 2>nul
if errorlevel 1 (
  echo Dependencias ausentes para o Mercado Albion.
  echo.
  echo Rode:
  echo   %PYTHON_CMD% -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo Iniciando Mercado Albion (Americas) em http://127.0.0.1:8528 ...
%PYTHON_CMD% app.py
pause
