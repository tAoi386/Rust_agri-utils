@echo off
cd /d "%~dp0"
if exist "%~dp0dist\capture\RustSeedLab\RustSeedLab.exe" (
    start "" "%~dp0dist\capture\RustSeedLab\RustSeedLab.exe"
    exit /b 0
)
call "%~dp0launch.bat"
