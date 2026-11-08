@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1

if not exist "%~dp0run.bat" (
    echo [error] run.bat not found. Run this script inside a complete MAA-PRTS checkout.
    pause
    exit /b 1
)

call "%~dp0run.bat" --debug
exit /b %ERRORLEVEL%
