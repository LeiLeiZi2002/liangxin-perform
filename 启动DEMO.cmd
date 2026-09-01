@echo off
setlocal
cd /d "%~dp0"
where pwsh.exe >nul 2>nul
if errorlevel 1 (
    echo PowerShell 7 is required.
    pause
    exit /b 1
)

pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-demo.ps1" %*
set "DEMO_EXIT_CODE=%errorlevel%"
if not "%DEMO_EXIT_CODE%"=="0" pause
exit /b %DEMO_EXIT_CODE%
