@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Game Monitor
cd /d "%~dp0"

echo ============================================================
echo  Game Monitor
echo ============================================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 launcher.py
  exit /b
)

where python >nul 2>nul
if %errorlevel%==0 (
  python launcher.py
  exit /b
)

echo Python was not found.
echo.
echo Please install Python 3 from:
echo https://www.python.org/downloads/windows/
echo.
echo Important: during installation, check "Add python.exe to PATH".
echo.
pause
