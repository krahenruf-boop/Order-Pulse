"""Only delete marked, same-user, expired UUID task directories in our root."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import time
import uuid
from .model import ExportError

OWNER = 'order-pulse-export-v1'


class TemporaryExports:
    def __init__(self, root=Path('/tmp/order-pulse')):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        s = self.root.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid():
            raise ExportError('EXPORT_TEMP_UNSAFE')
        self.root.chmod(0o700)

    @contextmanager
    def task(self):
        directory = self.root / str(uuid.uuid4())
        directory.mkdir(mode=0o700)
        lock = None
        try:
            lock = (directory / '.lock').open('x')
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.write(directory, '.owner.json', json.dumps({'owner': OWNER, 'created_at': time.time()}).encode())
            yield directory
        finally:
            # The directory was created by this context, not discovered by name.
            try: shutil.rmtree(directory)
            except OSError: raise ExportError('EXPORT_CLEANUP_FAILED') from None
            finally:
                if lock: lock.close()

    @staticmethod
    def write(directory, name, data):
        if Path(name).name != name: raise ExportError('EXPORT_TEMP_UNSAFE')
        path = directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as f: f.write(data)
        return path

    def cleanup_expired(self, max_age=3600, now=None):
        now = time.time() if now is None else now
        removed = 0
        for directory in self.root.iterdir():
            try:
                uuid.UUID(directory.name)
                s = directory.lstat()
                if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid(): continue
                marker = directory / '.owner.json'
                if marker.is_symlink() or marker.stat().st_size > 256: continue
                evidence = json.loads(marker.read_text())
                if evidence.get('owner') != OWNER or now - evidence['created_at'] <= max_age: continue
                # A still-running task, including another instance, owns this lock.
                fd = os.open(directory / '.lock', os.O_RDWR | os.O_NOFOLLOW)
                with os.fdopen(fd, 'r+') as lock:
                    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError: continue
                    if any(p.is_symlink() or not p.is_file() for p in directory.iterdir()): continue
                    shutil.rmtree(directory)
                    removed += 1
            except (ValueError, TypeError, KeyError, FileNotFoundError): continue
        return removed
