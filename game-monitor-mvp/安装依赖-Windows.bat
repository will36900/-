@echo off
chcp 65001 >nul
title 安装游戏监测依赖
cd /d "%~dp0"

echo ============================================================
echo  安装游戏监测工具依赖
echo ============================================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m pip install -r requirements.txt
  echo.
  echo 基础依赖安装完成。
  echo.
  echo 如果你有 NVIDIA 显卡，或者要使用 LibreHardwareMonitor 增强采集，
  echo 可以继续安装 Windows 可选依赖。
  echo.
  choice /m "是否安装 Windows 可选依赖"
  if %errorlevel%==1 py -3 -m pip install -r requirements-windows-optional.txt
  pause
  exit /b
)

where python >nul 2>nul
if %errorlevel%==0 (
  python -m pip install -r requirements.txt
  echo.
  echo 基础依赖安装完成。
  echo.
  choice /m "是否安装 Windows 可选依赖"
  if %errorlevel%==1 python -m pip install -r requirements-windows-optional.txt
  pause
  exit /b
)

echo 没有找到 Python。
echo 请先安装 Python 3，并勾选 “Add python.exe to PATH”。
pause
