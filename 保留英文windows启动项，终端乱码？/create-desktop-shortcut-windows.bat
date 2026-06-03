@echo off
chcp 65001 >nul
title Create Game Monitor Desktop Shortcut
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$desktop=[Environment]::GetFolderPath('Desktop');" ^
  "$target=Join-Path (Get-Location) 'start-game-monitor-windows.bat';" ^
  "$shortcut=Join-Path $desktop 'Game Monitor.lnk';" ^
  "$shell=New-Object -ComObject WScript.Shell;" ^
  "$lnk=$shell.CreateShortcut($shortcut);" ^
  "$lnk.TargetPath=$target;" ^
  "$lnk.WorkingDirectory=(Get-Location).Path;" ^
  "$lnk.WindowStyle=1;" ^
  "$lnk.Description='Start Game Monitor';" ^
  "$lnk.Save();" ^
  "Write-Host 'Created desktop shortcut:' $shortcut"

echo.
echo If the line above says "Created desktop shortcut", use "Game Monitor" on your desktop.
echo.
pause
