@echo off
setlocal
title Hermes LiveKit Setup

echo Setting up Hermes LiveKit...
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup-HermesLiveKit.ps1" -InstallFfmpeg -InstallAutoStart -RestartGateway -ReverseGeocoderUrl "https://nominatim.openstreetmap.org/reverse"
set "setup_exit=%ERRORLEVEL%"

echo.
if not "%setup_exit%"=="0" (
    echo Setup failed with exit code %setup_exit%.
) else (
    echo Setup completed successfully.
)
echo Press any key to close this window.
pause >nul
exit /b %setup_exit%
