#!/bin/zsh
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DESKTOP_DIR="$HOME/Desktop"
SHORTCUT="$DESKTOP_DIR/游戏监测工具.command"

mkdir -p "$DESKTOP_DIR"

cat > "$SHORTCUT" <<EOF
#!/bin/zsh
cd "$APP_DIR"

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
EOF

chmod +x "$SHORTCUT"

echo
echo "已创建桌面快捷方式：$SHORTCUT"
echo "以后直接双击桌面的“游戏监测工具.command”即可。"
echo
read "?按 Enter 退出..."
