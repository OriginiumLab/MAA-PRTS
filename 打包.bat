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
echo [打包] 正在生成 MAA-PRTS.exe...
"%PYTHON%" -u "tools\build.py"
set "BUILD_RC=%ERRORLEVEL%"
if not "%BUILD_RC%"=="0" goto :BuildFailed

echo.
echo [完成] 打包产物位于 build\MAA-PRTS\MAA-PRTS.exe
echo [完成] 整个 build\MAA-PRTS 文件夹即发布包，压缩成 zip 就是 Release 附件。
exit /b 0


:InstallBuildDependencies
"%PYTHON%" -c "import webview, clr, PyInstaller, PIL" >nul 2>&1
if not errorlevel 1 exit /b 0

echo [初始化] 正在安装打包依赖（仅首次需要）...
"%PYTHON%" -m pip install --disable-pip-version-check --prefer-binary --upgrade %PIP_USER_ARG% pywebview pythonnet pyinstaller pillow
if errorlevel 1 exit /b 1

"%PYTHON%" -c "import webview, clr, PyInstaller, PIL" >nul 2>&1
if errorlevel 1 (
    echo [错误] 打包依赖安装后仍无法导入。
    exit /b 1
)
exit /b 0


:CannotEnter
echo [错误] 无法进入脚本所在目录：%~dp0
pause
exit /b 1


:MissingBuildScript
echo [错误] 未找到 tools\build.py 或 tools\setup\prepare-python.bat。
echo 请在完整的 MAA-PRTS 项目目录中运行本脚本。
pause
exit /b 1


:PrepareFailed
rem 具体原因已由 tools\setup\prepare-python.bat 打印。
pause
exit /b 1


:InstallFailed
echo [错误] 打包依赖安装失败。请确认网络可访问 pypi.org，或检查代理/证书设置后重试。
pause
exit /b %INSTALL_RC%


:BuildFailed
echo.
echo [错误] 打包失败，退出码 %BUILD_RC%。
pause
exit /b %BUILD_RC%
