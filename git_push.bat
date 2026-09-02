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
    echo Install Git for Windows terlebih dahulu.
    pause
    exit /b 1
)

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Folder ini belum menjadi Git repository.
    echo Jalankan git init / hubungkan ke repository GitHub terlebih dahulu.
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

git diff --quiet
set "WT_CHANGED=%ERRORLEVEL%"
git diff --cached --quiet
set "IDX_CHANGED=%ERRORLEVEL%"
for /f "delims=" %%F in ('git ls-files --others --exclude-standard') do set "UNTRACKED=1"

if "%WT_CHANGED%"=="0" if "%IDX_CHANGED%"=="0" if not defined UNTRACKED (
    echo Tidak ada perubahan untuk di-commit.
    echo.
    pause
    exit /b 0
)

set /p "MSG=Commit message: "
if not defined MSG (
    echo.
    echo [ERROR] Commit message tidak boleh kosong.
    pause
    exit /b 1
)

echo.
echo [1/4] Menambahkan perubahan...
git add .
if errorlevel 1 goto :error

echo.
echo [2/4] Membuat commit...
git commit -m "%MSG%"
if errorlevel 1 goto :error

echo.
echo [3/4] Sinkronisasi dengan GitHub...
git pull --rebase origin %BRANCH%
if errorlevel 1 (
    echo.
    echo [ERROR] git pull --rebase gagal.
    echo Jika ada konflik, selesaikan konflik terlebih dahulu lalu jalankan:
    echo   git add .
    echo   git rebase --continue
    echo Setelah selesai, jalankan git_push.bat lagi.
    pause
    exit /b 1
)

echo.
echo [4/4] Push ke GitHub...
git push origin %BRANCH%
if errorlevel 1 goto :error

echo.
echo ==================================================
echo SUCCESS
echo Commit dan push selesai.
echo GitHub sudah diperbarui.
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
