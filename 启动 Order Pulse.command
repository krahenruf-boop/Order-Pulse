#!/bin/zsh
set -eu
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  osascript -e 'display alert "Order Pulse 尚未完成依赖安装" message "请按项目 README 完成首次安装。"'
  exit 1
fi
data_dir="${ORDER_PULSE_HOME:-$HOME/Library/Application Support/Order Pulse}"
mkdir -p "$data_dir/logs"
chmod 700 "$data_dir"
if .venv/bin/python tools/check_running.py; then
  open 'http://127.0.0.1:8786'
  exit 0
fi
if ! .venv/bin/python -c 'import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("127.0.0.1",8786)); s.close()'; then
  osascript -e 'display alert "8786端口已有其他实例" message "请先关闭隔离验收实例，再启动正式数据目录；不会停止旧TikTok OPS。"'
  exit 1
fi
.venv/bin/python tools/start.py
for attempt in {1..30}; do
  if .venv/bin/python tools/check_running.py; then
    open 'http://127.0.0.1:8786'
    exit 0
  fi
  sleep 0.3
done
osascript -e 'display alert "Order Pulse 未能启动" message "请检查8786端口及Order Pulse日志目录。"'
exit 1
