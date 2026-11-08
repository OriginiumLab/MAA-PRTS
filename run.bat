@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1

cd /d "%~dp0"
if errorlevel 1 goto :CannotEnter

set "DEBUG_MODE="
if /i "%~1"=="--debug" set "DEBUG_MODE=1"

if exist "build\MAA-PRTS\MAA-PRTS.exe" goto :RunExe
if not exist "backend\main.py" goto :MissingSource
if not exist "tools\setup\prepare-python.bat" goto :MissingSource

call "tools\setup\prepare-python.bat"
if errorlevel 1 goto :PrepareFailed

call :InstallRuntime
set "INSTALL_RC=%ERRORLEVEL%"
if not "%INSTALL_RC%"=="0" goto :InstallFailed

echo.
if defined DEBUG_MODE (
    echo [debug] starting MAA-PRTS from source (DevTools port 9333)...
    "%PYTHON%" -u "backend\main.py" --debug
) else (
    echo [start] starting MAA-PRTS from source...
    "%PYTHON%" "backend\main.py"
)
set "APP_RC=%ERRORLEVEL%"
if not "%APP_RC%"=="0" goto :AppFailed

if defined DEBUG_MODE (
    echo.
    pause
)
exit /b 0


:RunExe
if defined DEBUG_MODE goto :RunDebugExe
start "" /D "%CD%\build\MAA-PRTS" "%CD%\build\MAA-PRTS\MAA-PRTS.exe"
if errorlevel 1 goto :ExeFailed
exit /b 0


:RunDebugExe
set "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333"
echo [debug] starting packaged MAA-PRTS (DevTools port 9333)...
"%CD%\build\MAA-PRTS\MAA-PRTS.exe" --debug
set "APP_RC=%ERRORLEVEL%"
if not "%APP_RC%"=="0" goto :AppFailed
echo.
pause
exit /b 0


:InstallRuntime
"%PYTHON%" -c "import webview, clr" >nul 2>&1
if not errorlevel 1 exit /b 0

echo [init] installing runtime deps pywebview & pythonnet (first run only)...
"%PYTHON%" -m pip install --disable-pip-version-check --prefer-binary --upgrade %PIP_USER_ARG% pywebview pythonnet
if errorlevel 1 exit /b 1

"%PYTHON%" -c "import webview, clr" >nul 2>&1
if errorlevel 1 (
    echo [error] runtime deps still not importable after install.
    exit /b 1
)
exit /b 0


:CannotEnter
echo [error] cannot enter script dir: %~dp0
pause
exit /b 1


:MissingSource
echo [error] backend\main.py or tools\setup\prepare-python.bat not found.
echo Run this script inside a complete MAA-PRTS checkout.
pause
exit /b 1


:PrepareFailed
rem reason already printed by tools\setup\prepare-python.bat.
pause
exit /b 1


:InstallFailed
echo [error] failed to install dependencies. Check network access to pypi.org (proxy/cert) and retry.
pause
exit /b %INSTALL_RC%


:ExeFailed
echo [error] failed to start build\MAA-PRTS\MAA-PRTS.exe.
pause
exit /b 1


:AppFailed
echo.
echo [error] app exited abnormally, exit code %APP_RC%.
echo Log: debug\app.log
echo If the error mentions WebView2 / Edge Chromium, install Microsoft Edge WebView2 Runtime and retry.
pause
exit /b %APP_RC%
