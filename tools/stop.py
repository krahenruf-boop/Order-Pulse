import hashlib
import json
import os
import signal
import subprocess
import urllib.error
import urllib.request

from start import AGENT_PATH, DATA_ROOT, TARGET, loaded, verify_existing_plist


def main():
    if verify_existing_plist():
        if loaded():
            result = subprocess.run(["launchctl", "bootout", TARGET], capture_output=True, text=True)
            if result.returncode:
                raise SystemExit("Order Pulse 后台服务停止失败：" + (result.stderr.strip() or "launchctl 未能停止服务"))
        AGENT_PATH.unlink()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8786/api/health", timeout=2) as response:
            data = json.load(response)
    except (OSError, urllib.error.URLError):
        return
    if data.get("app") != "order-pulse" or data.get("data_root_id") != hashlib.sha256(str(DATA_ROOT).encode()).hexdigest():
        raise SystemExit("Refusing to stop a different service or data root")
    os.kill(data["pid"], signal.SIGTERM)


if __name__ == "__main__":
    main()
