@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" "%~dp0ВЫЛОЖИТЬ В ИНТЕРНЕТ.html"
start "" "https://github.com/signup"
timeout /t 2 /nobreak >nul
start "" "https://dashboard.render.com/register"
echo.
echo   Открылась инструкция + GitHub + Render.
echo   Дальше: ЗАГРУЗИТЬ НА GITHUB.bat → Render Web Service → НАСТРОЙ KEEP-ALIVE.bat
echo.
pause
