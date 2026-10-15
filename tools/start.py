"""Start the single Order Pulse backend under the user's launchd session."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path


LABEL = "local.order-pulse.service"
PROJECT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("ORDER_PULSE_HOME", Path.home() / "Library/Application Support/Order Pulse")).expanduser().resolve()
AGENT_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
TARGET = f"gui/{os.getuid()}/{LABEL}"


def desired_plist():
    log = DATA_ROOT / "logs" / "service.log"
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "order_pulse.server"],
        "WorkingDirectory": str(PROJECT),
        "EnvironmentVariables": {"ORDER_PULSE_HOME": str(DATA_ROOT)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def verify_existing_plist():
    if not AGENT_PATH.exists():
        return False
    try:
        current = plistlib.loads(AGENT_PATH.read_bytes())
    except (OSError, ValueError, TypeError):
        raise SystemExit("Order Pulse 后台启动配置无法读取；未覆盖该文件") from None
    expected = desired_plist()
    if any(current.get(key) != expected[key] for key in ("Label", "ProgramArguments", "WorkingDirectory", "EnvironmentVariables")):
        raise SystemExit("同名后台启动配置与当前 Order Pulse 项目或数据根不一致；未覆盖该文件")
    return True


def loaded():
    return subprocess.run(["launchctl", "print", TARGET], capture_output=True).returncode == 0


def main():
    (DATA_ROOT / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA_ROOT.chmod(0o700)
    AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not verify_existing_plist():
        fd = os.open(AGENT_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            plistlib.dump(desired_plist(), file)
    if loaded():
        command = ["launchctl", "kickstart", "-k", TARGET]
    else:
        command = ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(AGENT_PATH)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit("Order Pulse 后台服务启动失败：" + (result.stderr.strip() or "launchctl 未能启动服务"))


if __name__ == "__main__":
    main()
