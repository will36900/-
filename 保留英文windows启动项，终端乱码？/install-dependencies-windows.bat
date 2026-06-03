@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Install Game Monitor Dependencies
cd /d "%~dp0"

echo ============================================================
echo  Install Game Monitor Dependencies
echo ============================================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m pip install -r requirements.txt
  echo.
  echo Basic dependencies installed.
  echo.
  choice /m "Install optional Windows dependencies for NVIDIA/LibreHardwareMonitor"
  if %errorlevel%==1 py -3 -m pip install -r requirements-windows-optional.txt
  echo.
  pause
  exit /b
)

where python >nul 2>nul
if %errorlevel%==0 (
  python -m pip install -r requirements.txt
  echo.
  echo Basic dependencies installed.
  echo.
  choice /m "Install optional Windows dependencies for NVIDIA/LibreHardwareMonitor"
  if %errorlevel%==1 python -m pip install -r requirements-windows-optional.txt
  echo.
  pause
  exit /b
)

echo Python was not found.
echo.
echo Please install Python 3 and check "Add python.exe to PATH".
echo.
pause
