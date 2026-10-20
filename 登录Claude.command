#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -x .venv/bin/python ]; then
    python3 install.py
fi
claude_program=$(.venv/bin/python -c 'from app2.backends import find; print(find("claude") or "")')
if [ -z "$claude_program" ]; then
    echo '未找到 Claude Code。请先按照 docs/MACOS.md 安装。'
    read -r answer
    exit 1
fi
"$claude_program" auth login
printf '\n登录完成后，重新打开群聊。按回车关闭。\n'
read -r answer
