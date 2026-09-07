@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\download_minimax_h3.ps1"
if errorlevel 1 (
  echo.
  echo Download did not complete. Read the error above and run this file again to resume.
)
pause
