@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "GIT=%USERPROFILE%\.grok\tools\PortableGit\bin\git.exe"
if not exist "%GIT%" where git >nul 2>&1 && set "GIT=git"
if not exist "%GIT%" if /I not "%GIT%"=="git" (
  echo Git не найден. Сообщите ассистенту — поставим снова.
  pause
  exit /b 1
)

echo.
echo   === Загрузка BANANAWOW на GitHub ===
echo.
echo   1) Создайте репозиторий: https://github.com/new
echo      имя: bananawow  (Public, БЕЗ README)
echo   2) Скопируйте URL, например:
echo      https://github.com/ВАШ_НИК/bananawow.git
echo.

if not exist ".git" (
  echo   Инициализирую git...
  "%GIT%" init
  "%GIT%" add -A
  "%GIT%" commit -m "BANANAWOW: bot + Render + keep-alive"
)

set /p REPO=Вставьте URL репозитория: 
if "%REPO%"=="" (
  echo Пустой URL.
  pause
  exit /b 1
)

"%GIT%" remote remove origin 2>nul
"%GIT%" remote add origin %REPO%
"%GIT%" branch -M main
echo.
echo   Отправляю код... (войдите / вставьте token, если спросит)
"%GIT%" add -A
"%GIT%" status
"%GIT%" commit -m "Update bananawow deploy" 2>nul
"%GIT%" push -u origin main
if errorlevel 1 (
  echo.
  echo   Не удалось. Нужен Personal Access Token вместо пароля:
  echo   https://github.com/settings/tokens  → classic → галочка repo
  echo.
  pause
  exit /b 1
)

echo.
echo   OK! Код на GitHub. Дальше:
echo   1) https://dashboard.render.com
echo   2) New → Web Service → bananawow
echo   3) Build: pip install -r requirements.txt
echo   4) Start: python bot.py
echo   5) Free + env BOT_TOKEN=... (токен бота)
echo   6) После деплоя: НАСТРОЙ KEEP-ALIVE.bat
echo.
start "" "https://dashboard.render.com/select-repo?type=web"
pause
