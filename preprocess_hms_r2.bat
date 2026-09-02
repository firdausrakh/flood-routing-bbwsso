@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"
title Upload Cloudflare R2 - Penelusuran Banjir

if not exist "%ROOT%.env" (
  echo [ERROR] File .env belum ada. Salin .env.example menjadi .env.
  pause
  exit /b 1
)

if not exist "%ROOT%data\hms\index.json" (
  echo [ERROR] data\hms\index.json belum ada.
  echo [INFO] Jalankan preprocess_hms.bat terlebih dahulu. preprocess_hms_r2.bat hanya melanjutkan hasil preprocessing ke R2.
  pause
  exit /b 1
)

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
"%VENV_PY%" -c "import boto3" >nul 2>nul
if errorlevel 1 (
  echo [SETUP] Menginstal dependency upload R2...
  "%VENV_PY%" -m pip install -r "%ROOT%requirements.txt"
  if errorlevel 1 exit /b 1
)

echo.
echo [R2] Mengunggah hasil preprocessing data\hms ke bucket flood-routing...
"%VENV_PY%" "%ROOT%scripts\upload_hms_r2.py" %*
set "ERR=%ERRORLEVEL%"
if "%ERR%"=="0" (
  echo [R2] Selesai. preprocess_hms.py tidak dijalankan ulang.
  echo [R2] Official basin/rivers tidak diunggah; gunakan bucket dta-map-assets.
) else (
  echo [ERROR] Upload R2 gagal. Periksa credential, .env, dan data\hms.
)
pause
exit /b %ERR%
