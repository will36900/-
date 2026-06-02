#!/bin/zsh
cd "$(dirname "$0")"

echo "============================================================"
echo " 游戏监测工具"
echo "============================================================"
echo

if command -v python3 >/dev/null 2>&1; then
  python3 launcher.py
else
  echo "没有找到 python3。请先安装 Python 3。"
  echo
  read "?按 Enter 退出..."
fi
