@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title Penelusuran Banjir - Git Commit and Push

echo.
echo ==================================================
echo        PENELUSURAN BANJIR - GIT COMMIT AND PUSH
echo ==================================================
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git tidak ditemukan.
    pause
    exit /b 1
)

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Folder ini belum menjadi Git repository.
    pause
    exit /b 1
)

for /f "delims=" %%B in ('git branch --show-current') do set "BRANCH=%%B"
if not defined BRANCH set "BRANCH=main"

echo Branch aktif : %BRANCH%
echo.

echo Perubahan saat ini:
echo --------------------------------------------------
git status --short
echo --------------------------------------------------
echo.

REM ==================================================
REM CEK APAKAH ADA PERUBAHAN FILE
REM ==================================================

git status --porcelain > "%TEMP%\git_status.txt"

for %%A in ("%TEMP%\git_status.txt") do set SIZE=%%~zA

if "%SIZE%"=="0" (
    echo Tidak ada perubahan baru untuk di-commit.
    echo Mencoba push commit lokal yang belum terkirim...
    goto :push
)

REM ==================================================
REM COMMIT
REM ==================================================

set /p "MSG=Commit message: "

if not defined MSG (
    echo.
    echo [ERROR] Commit message tidak boleh kosong.
    pause
    exit /b 1
)

echo.
echo [1/3] Menambahkan perubahan...
git add .
if errorlevel 1 goto :error

echo.
echo [2/3] Membuat commit...
git commit -m "%MSG%"
if errorlevel 1 goto :error

REM ==================================================
REM PUSH
REM ==================================================

:push

echo.
echo [3/3] Push ke GitHub...
git push origin %BRANCH%

if errorlevel 1 (
    echo.
    echo ==================================================
    echo PUSH GAGAL
    echo ==================================================
    echo.
    echo Kemungkinan repository GitHub memiliki commit
    echo yang belum ada di komputer lokal.
    echo.
    echo Tidak ada perubahan GitHub yang ditimpa otomatis.
    echo.
    pause
    exit /b 1
)

echo.
echo ==================================================
echo SUCCESS
echo ==================================================
echo Commit lokal sudah dikirim ke GitHub.
echo Branch : %BRANCH%
echo.
echo Vercel akan auto-deploy dari branch %BRANCH%.
echo ==================================================
echo.

pause
exit /b 0


:error

echo.
echo ==================================================
echo ERROR
echo Proses dihentikan. Periksa pesan Git di atas.
echo ==================================================
echo.

pause
exit /b 1