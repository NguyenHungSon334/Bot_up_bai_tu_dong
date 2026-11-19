@echo off
rem Start bot: checks/installs dependencies, then runs web UI + scheduler. Keep this window open.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 (
  echo.
  echo Bot dung lai - xem loi o tren.
)
pause
