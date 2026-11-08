@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1

cd /d "%~dp0"
if errorlevel 1 goto :CannotEnter
if not exist "tools\build.py" goto :MissingBuildScript
if not exist "tools\setup\prepare-python.bat" goto :MissingBuildScript

call "tools\setup\prepare-python.bat"
if errorlevel 1 goto :PrepareFailed

call :InstallBuildDependencies
set "INSTALL_RC=%ERRORLEVEL%"
if not "%INSTALL_RC%"=="0" goto :InstallFailed

echo.
echo [build] generating MAA-PRTS.exe...
"%PYTHON%" -u "tools\build.py"
set "BUILD_RC=%ERRORLEVEL%"
if not "%BUILD_RC%"=="0" goto :BuildFailed

echo.
echo [done] output: build\MAA-PRTS\MAA-PRTS.exe
echo [done] the whole build\MAA-PRTS folder is the release package, zip it for Release assets.
exit /b 0


:InstallBuildDependencies
"%PYTHON%" -c "import webview, clr, PyInstaller, PIL" >nul 2>&1
if not errorlevel 1 exit /b 0

echo [init] installing build dependencies (first run only)...
"%PYTHON%" -m pip install --disable-pip-version-check --prefer-binary --upgrade %PIP_USER_ARG% pywebview pythonnet pyinstaller pillow
if errorlevel 1 exit /b 1

"%PYTHON%" -c "import webview, clr, PyInstaller, PIL" >nul 2>&1
if errorlevel 1 (
    echo [error] build dependencies still not importable after install.
    exit /b 1
)
exit /b 0


:CannotEnter
echo [error] cannot enter script dir: %~dp0
pause
exit /b 1


:MissingBuildScript
echo [error] tools\build.py or tools\setup\prepare-python.bat not found.
echo Run this script inside a complete MAA-PRTS checkout.
pause
exit /b 1


:PrepareFailed
rem reason already printed by tools\setup\prepare-python.bat.
pause
exit /b 1


:InstallFailed
echo [error] failed to install build dependencies. Check network access to pypi.org (proxy/cert) and retry.
pause
exit /b %INSTALL_RC%


:BuildFailed
echo.
echo [error] build failed, exit code %BUILD_RC%.
pause
exit /b %BUILD_RC%
