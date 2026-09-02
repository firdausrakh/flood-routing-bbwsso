@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"
title Preprocessing HEC-HMS - Penelusuran Banjir

set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto :create_venv
"%VENV_PY%" -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [INFO] .venv lama tidak valid setelah folder dipindah/diubah nama. Membuat ulang...
  rmdir /s /q "%ROOT%.venv" 2>nul
  goto :create_venv
)
goto :venv_ready

:create_venv
echo [SETUP] Membuat .venv...
py -3.12 -m venv "%ROOT%.venv" 2>nul || py -3.11 -m venv "%ROOT%.venv" 2>nul || py -3 -m venv "%ROOT%.venv" 2>nul || python -m venv "%ROOT%.venv"
if errorlevel 1 exit /b 1
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"

:venv_ready
"%VENV_PY%" -c "import geopandas, shapely, pyproj, numpy; from pydsstools.heclib.dss import HecDss" >nul 2>nul
if errorlevel 1 (
  echo [SETUP] Menginstal dependency preprocessing...
  "%VENV_PY%" -m pip install -r "%ROOT%requirements-preprocess.txt"
  if errorlevel 1 exit /b 1
)

if not exist "%ROOT%data\source" mkdir "%ROOT%data\source"
if not exist "%ROOT%data\hms" mkdir "%ROOT%data\hms"
set "MODELARG="
if not "%~1"=="" set "MODELARG=--model "%~1""

echo [PROSES] data\source -^> data\hms
"%VENV_PY%" "%ROOT%scripts\preprocess_hms.py" %MODELARG%
set "ERR=%ERRORLEVEL%"
if "%ERR%"=="0" echo [SELESAI] data\hms siap dibaca backend lokal atau diunggah ke R2.
if not "%ERR%"=="0" echo [ERROR] Preprocessing gagal. Pastikan tiap model berisi .basin, .sqlite, dan T_*.dss.
echo.
if not "%FLOOD_NO_PAUSE%"=="1" pause
exit /b %ERR%
