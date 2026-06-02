@echo off
chcp 65001 >nul
title 创建游戏监测桌面快捷方式
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$desktop=[Environment]::GetFolderPath('Desktop');" ^
  "$target=Join-Path (Get-Location) '启动游戏监测-Windows.bat';" ^
  "$shortcut=Join-Path $desktop '游戏监测工具.lnk';" ^
  "$shell=New-Object -ComObject WScript.Shell;" ^
  "$lnk=$shell.CreateShortcut($shortcut);" ^
  "$lnk.TargetPath=$target;" ^
  "$lnk.WorkingDirectory=(Get-Location).Path;" ^
  "$lnk.WindowStyle=1;" ^
  "$lnk.Description='启动游戏监测工具';" ^
  "$lnk.Save();" ^
  "Write-Host '已创建桌面快捷方式：' $shortcut"

echo.
echo 如果上面显示“已创建”，以后直接双击桌面的“游戏监测工具”即可。
echo.
pause
