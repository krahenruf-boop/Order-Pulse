"""Bounded rendering lease for the existing MV3 collector's hidden tabs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .ego_runtime import EgoRuntimeError


class MaterialBackground:
    def __init__(self, binding):
        self.binding = binding
        self.process = None
        self.directory = None

    def start(self):
        self.directory = tempfile.TemporaryDirectory(prefix='order-pulse-material-')
        directory = Path(self.directory.name)
        self.lease = directory / 'lease'
        ready = directory / 'ready'
        self.lease.touch(mode=0o600)
        spec = dict(binding=self.binding, lease=str(self.lease), ready=str(ready), owner_pid=os.getpid())
        program = 'const INPUT=' + json.dumps(spec) + ';\n' + Path(__file__).with_name('ego_material_background.js').read_text()
        try:
            self.process = subprocess.Popen(
                [str(Path.home()/'.local/bin/ego-browser'), 'nodejs'],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                text=True, env={'HOME':str(Path.home()), 'PATH':'/usr/bin:/bin:/usr/sbin:/sbin',
                                'TMPDIR':'/private/tmp'})
            self.process.stdin.write(program)
            self.process.stdin.close()
            until = time.monotonic() + 8
            while time.monotonic() < until and self.process.poll() is None:
                if ready.exists():
                    if ready.read_text() == 'READY':
                        return
                    break
                time.sleep(.05)
            raise EgoRuntimeError('WAITING_CONNECTION')
        except Exception:
            self.stop()
            raise

    def stop(self):
        if self.directory:
            self.lease.unlink(missing_ok=True)
        if self.process and self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # This is our own bounded helper, never the browser or another app.
                self.process.terminate()
                try:self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill();self.process.wait(timeout=2)
        if self.directory:
            self.directory.cleanup()
            self.directory = None
