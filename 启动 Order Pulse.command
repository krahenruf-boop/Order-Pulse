#!/bin/zsh
set -eu
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  osascript -e 'display alert "Order Pulse 尚未完成依赖安装" message "请按项目 README 完成首次安装。"'
  exit 1
fi
# Each manager checks its own saved identity, port and data root.
# Starting an already healthy workstation does not restart its running tasks.
if ! .venv/bin/python tools/start.py; then
  osascript -e 'display alert "Order Pulse 未能启动" message "请检查 8786 / 8797 端口及对应工作台的日志；不会停止其他服务。"'
  exit 1
fi
open 'http://127.0.0.1:8786/#balance'
