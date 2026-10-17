@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "RUST_BREEDER_PYTHON="
set "RUST_BREEDER_PYTHON_ARGS="
set "RUST_BREEDER_BUNDLED=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%RUST_BREEDER_BUNDLED%" (
    set "RUST_BREEDER_PYTHON=%RUST_BREEDER_BUNDLED%"
    goto python_found
)
py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if not errorlevel 1 (
    set "RUST_BREEDER_PYTHON=py"
    set "RUST_BREEDER_PYTHON_ARGS=-3"
    goto python_found
)
python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if not errorlevel 1 (
    set "RUST_BREEDER_PYTHON=python"
    goto python_found
)
echo 没有找到 Python 3.10 或更新版本。
goto failed

:python_found
set "PYTHONPATH=%~dp0.deps;%~dp0.build-deps;%PYTHONPATH%"
echo 正在检查本地构建依赖。
"%RUST_BREEDER_PYTHON%" %RUST_BREEDER_PYTHON_ARGS% -c "import PySide6.QtWidgets" >nul 2>&1
if not errorlevel 1 goto qt_ready
echo 缺少 Qt6，正在安装到本项目的 .deps，需要联网。
"%RUST_BREEDER_PYTHON%" %RUST_BREEDER_PYTHON_ARGS% -m pip install --target "%~dp0.deps" --upgrade -r "%~dp0requirements.txt"
if errorlevel 1 goto failed

:qt_ready
"%RUST_BREEDER_PYTHON%" %RUST_BREEDER_PYTHON_ARGS% -c "import PyInstaller" >nul 2>&1
if not errorlevel 1 goto builder_ready
echo 缺少打包工具，正在安装到本项目的 .build-deps，需要联网。
"%RUST_BREEDER_PYTHON%" %RUST_BREEDER_PYTHON_ARGS% -m pip install --target "%~dp0.build-deps" --upgrade "PyInstaller>=6,<7"
if errorlevel 1 goto failed

:builder_ready
"%RUST_BREEDER_PYTHON%" %RUST_BREEDER_PYTHON_ARGS% -m PyInstaller --noconfirm --clean --distpath "%~dp0dist\build-new" "%~dp0RustSeedLab.spec"
if errorlevel 1 goto failed
"%RUST_BREEDER_PYTHON%" %RUST_BREEDER_PYTHON_ARGS% "%~dp0tools\build_portable.py"
if errorlevel 1 goto failed
echo.
echo 已生成 dist\capture\RustSeedLab\RustSeedLab.exe。
echo 请复制整个 RustSeedLab 文件夹；接收方不需要安装 Python。
pause
exit /b 0

:failed
echo.
echo 构建失败，请查看上方信息。
pause
exit /b 1
