@echo off
chcp 65001 >nul
title 游戏监测工具
cd /d "%~dp0"

echo ============================================================
echo  游戏监测工具
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
echo 没有找到 Python。
echo.
echo 请先安装 Python 3：
echo https://www.python.org/downloads/windows/
echo.
echo 安装时请勾选 “Add python.exe to PATH”。
echo.
pause
