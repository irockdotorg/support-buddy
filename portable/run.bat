@echo off
REM SupportBuddy Portable Launcher (Windows)
REM Detects the OS and runs the correct binary.
REM Put this in the same folder as the platform binaries.

set SCRIPT_DIR=%~dp0

if not exist "%SCRIPT_DIR%support-buddy-win.exe" (
    echo Error: Windows binary not found at %SCRIPT_DIR%support-buddy-win.exe
    echo Download it from the SupportBuddy releases page.
    pause
    exit /b 1
)

REM Check for admin rights
net session >nul 2>&1
if %errorLevel% == 0 (
    REM Running as admin
    "%SCRIPT_DIR%support-buddy-win.exe" %*
) else (
    echo SupportBuddy — run as normal user or as administrator?
    echo   [1] Normal user (no admin — some cleanups will be limited)
    echo   [2] Run as administrator
    echo   [Enter] Normal user (default)
    set /p choice="Choice: "
    if "%choice%"=="2" (
        powershell -Command "Start-Process '%SCRIPT_DIR%support-buddy-win.exe' -Verb RunAs -ArgumentList '%*'"
    ) else (
        "%SCRIPT_DIR%support-buddy-win.exe" %*
    )
)
