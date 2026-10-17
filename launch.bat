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
echo 没有找到 Python 3.10 或更新版本。请安装 Python 后再次启动。
goto failed

:python_found
set "PYTHONPATH=%~dp0.deps;%PYTHONPATH%"
"%RUST_BREEDER_PYTHON%" %RUST_BREEDER_PYTHON_ARGS% -c "import PySide6.QtWidgets" >nul 2>&1
if not errorlevel 1 goto run_app
echo 首次启动：正在把 Qt6 依赖安装到本项目的 .deps 文件夹，需要联网。
"%RUST_BREEDER_PYTHON%" %RUST_BREEDER_PYTHON_ARGS% -m pip install --target "%~dp0.deps" --upgrade -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo 依赖安装失败。请确认网络和 Python 的 pip 可用，然后重试。
    goto failed
)

:run_app
"%RUST_BREEDER_PYTHON%" %RUST_BREEDER_PYTHON_ARGS% "%~dp0main.py" %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo 程序未能完成。上方信息可用于排查问题。
pause
exit /b 1
