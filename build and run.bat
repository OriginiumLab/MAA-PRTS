@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
taskkill /F /IM MAA-PRTS.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul
call "%~dp0build.bat" || (echo [error] build failed, see log above. & pause & exit /b 1)
echo.
echo [start] launching freshly built MAA-PRTS.exe...
call "%~dp0run.bat"
