@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"
title Penelusuran Banjir Sungai

if not exist ".env" (
  echo [ERROR] File .env belum ada.
  echo Salin .env.example menjadi .env lalu pilih DATA_BACKEND=local atau r2.
  exit /b 1
)

set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto :create_venv
"%VENV_PY%" -c "import sys; print(sys.executable)" >nul 2>nul
if errorlevel 1 goto :recreate_venv
goto :venv_ready

:recreate_venv
echo [INFO] .venv lama tidak valid setelah folder dipindah/diubah nama. Membuat ulang...
rmdir /s /q "%ROOT%.venv" 2>nul

:create_venv
echo [1/3] Membuat virtual environment...
py -3.12 -m venv "%ROOT%.venv" 2>nul || py -3.11 -m venv "%ROOT%.venv" 2>nul || py -3 -m venv "%ROOT%.venv" 2>nul || python -m venv "%ROOT%.venv"
if errorlevel 1 exit /b %errorlevel%
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
echo [2/3] Menginstal dependency...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 exit /b %errorlevel%
"%VENV_PY%" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 exit /b %errorlevel%

:venv_ready
echo [3/3] Menjalankan Penelusuran Banjir...
echo Root proyek: %ROOT%
echo Buka http://127.0.0.5:8000
"%VENV_PY%" -m api.app
exit /b %errorlevel%
