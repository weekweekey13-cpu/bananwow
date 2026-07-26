@echo off
chcp 65001 >nul
title BANANAWOW — keep-alive
cd /d "%~dp0"

set "URL="
if exist "PUBLIC-URL.txt" (
  set /p URL=<PUBLIC-URL.txt
)

echo.
echo  ========================================
echo   BANANAWOW: чтобы Render НЕ засыпал
echo  ========================================
echo.
echo  Бесплатный Render спит через ~15 мин без визитов.
echo  Нужен внешний пинг каждые 5 минут — как у Калаграма.
echo.
if defined URL (
  echo  Ваш PUBLIC-URL: %URL%
  echo  Пинг: %URL%/api/ping
  echo  Инструкция: %URL%/keepalive-setup
  echo.
  start "" "%URL%/keepalive-setup"
) else (
  echo  1) После деплоя на Render скопируй ссылку сервиса
  echo     например https://bananawow-xxxx.onrender.com
  echo  2) Впиши её в файл PUBLIC-URL.txt (одна строка)
  echo  3) Запусти этот bat снова
  echo.
  echo  Или открой в браузере:
  echo     https://ВАШ.onrender.com/keepalive-setup
  echo.
)
echo  UptimeRobot:
echo   Monitor Type: HTTP(s)
echo   URL: https://ВАШ.onrender.com/api/ping
echo   Interval: Every 5 minutes
echo.
timeout /t 2 >nul
start "" "https://uptimerobot.com/signUp"
echo.
pause
