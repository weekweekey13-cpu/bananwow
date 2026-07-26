@echo off
chcp 65001 >nul
cd /d "%~dp0"
title BANANAWOW bot + tunnel
echo ============================================
echo   BANANAWOW — бот + HTTPS (без предупреждения)
echo   Окно НЕ ЗАКРЫВАТЬ — пока открыто, бот жив.
echo ============================================
echo.

set PATH=%~dp0tools\node;%PATH%

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python не найден. Установите Python 3 и добавьте в PATH.
  pause
  exit /b 1
)

if not exist ".env" (
  echo [ERROR] Нет файла .env — скопируйте .env.example и впишите BOT_TOKEN
  pause
  exit /b 1
)

echo Запуск... логи также пишутся в stable.log
echo.
python run_stable.py
echo.
echo Процесс завершился. Если упал — смотри stable.log
pause
