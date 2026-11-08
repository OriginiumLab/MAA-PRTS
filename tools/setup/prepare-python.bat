@echo off
rem Script contains UTF-8 text; switch code page first, otherwise label lookup may misalign
rem (harmless if the caller already switched).
chcp 65001 >nul 2>&1

rem ============================================================
rem Shared Python environment bootstrap (for run.bat / build.bat, do not run standalone)
rem
rem On success: sets PYTHON (interpreter path), PYTHON_IS_VENV, PIP_USER_ARG, exit code 0
rem On failure: prints the reason, exit code 1
rem
rem Note: no setlocal here, otherwise the variables above cannot pass back to the caller.
rem ============================================================

rem This script lives in tools\setup\; project root is two levels up,
rem all later relative paths are root-relative.
cd /d "%~dp0\..\.."
if errorlevel 1 (
    echo [error] cannot enter project dir: %~dp0\..\..
    exit /b 1
)

call :SelectPython
if errorlevel 1 exit /b 1
call :EnsurePip
if errorlevel 1 exit /b 1

rem With system Python, prefer --user installs to avoid write failures without admin rights.
set "PIP_USER_ARG="
if not defined PYTHON_IS_VENV (
    "%PYTHON%" -c "import site; raise SystemExit(0 if site.ENABLE_USER_SITE else 1)" >nul 2>&1
    if not errorlevel 1 set "PIP_USER_ARG=--user"
)
exit /b 0


:SelectPython
set "PYTHON="
set "PYTHON_IS_VENV="
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=%CD%\.venv\Scripts\python.exe"
        set "PYTHON_IS_VENV=1"
        exit /b 0
    )
    echo [hint] .venv in this project is unusable, falling back to system Python.
)

call :FindSystemPython
if errorlevel 1 exit /b 1
set "SYSTEM_PYTHON=%PYTHON%"

echo [init] creating project-local .venv (first run only)...
"%PYTHON%" -m venv ".venv"
if errorlevel 1 (
    echo [hint] failed to create .venv, will install deps with system Python.
    set "PYTHON=%SYSTEM_PYTHON%"
    exit /b 0
)

set "PYTHON=%CD%\.venv\Scripts\python.exe"
set "PYTHON_IS_VENV=1"
"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [hint] newly created .venv is unusable, will use system Python.
    set "PYTHON=%SYSTEM_PYTHON%"
    set "PYTHON_IS_VENV="
)
exit /b 0


:FindSystemPython
set "PYTHON="
call :TryPython py -3
if not errorlevel 1 exit /b 0
call :TryPython python
if not errorlevel 1 exit /b 0
call :TryPython python3
if not errorlevel 1 exit /b 0

rem If not on PATH, check common locations of the official installer and Python Install Manager.
for /d %%D in (
    "%LocalAppData%\Programs\Python\Python*"
    "%LocalAppData%\Python\pythoncore-*"
    "%ProgramFiles%\Python*"
    "%ProgramFiles(x86)%\Python*"
) do (
    if exist "%%~fD\python.exe" (
        call :TryPythonPath "%%~fD\python.exe"
        if not errorlevel 1 exit /b 0
    )
)

echo [error] Python 3.10+ not found.
echo Download: https://www.python.org/downloads/windows/
echo When installing, tick 'Add python.exe to PATH'.
exit /b 1


rem One call for both: print the interpreter path if version >= 3.10, else exit 1 quietly.
:TryPython
set "PYTHON="
for /f "delims=" %%I in ('%* -c "import sys; sys.exit(1) if sys.version_info < (3, 10) else print(sys.executable)" 2^>nul') do set "PYTHON=%%I"
if defined PYTHON exit /b 0
exit /b 1


:TryPythonPath
if not exist "%~1" exit /b 1
"%~1" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYTHON=%~1"
exit /b 0


:EnsurePip
"%PYTHON%" -m pip --version >nul 2>&1
if not errorlevel 1 exit /b 0

echo [init] pip missing, trying to bootstrap it...
"%PYTHON%" -m ensurepip --upgrade
if errorlevel 1 (
    echo [error] cannot bootstrap pip. Reinstall official Python and tick pip in the installer.
    exit /b 1
)
"%PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [error] pip still unavailable.
    exit /b 1
)
exit /b 0
