@echo off
cd /d "%~dp0"

if exist "local_settings.bat" call "local_settings.bat"

where uv >nul 2>nul
if errorlevel 1 (
    echo uv is not installed or not on PATH.
    echo Install it first: https://astral.sh/uv/install.ps1 ^(run in PowerShell^)
    pause
    exit /b 1
)

echo Starting DV360 report app...
echo A browser tab will open automatically. Close this window to stop the app.
echo.

uv run streamlit run app.py

pause
