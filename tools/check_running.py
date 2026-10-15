import json
import sys
import urllib.request
import os
import hashlib
from pathlib import Path
try:
    with urllib.request.urlopen('http://127.0.0.1:8786/api/health',timeout=1) as response:
        data=json.load(response)
    root=Path(os.environ.get('ORDER_PULSE_HOME',Path.home()/'Library/Application Support/Order Pulse')).expanduser().resolve()
    correct=data.get('data_root_id')==hashlib.sha256(str(root).encode()).hexdigest()
    sys.exit(0 if data.get('app')=='order-pulse' and data.get('status')=='ok' and correct else 1)
except Exception:
    sys.exit(1)
