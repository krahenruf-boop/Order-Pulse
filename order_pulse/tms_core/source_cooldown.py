"""Origin-wide persistent cooldown for the diagnostic connector, not a timer."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import tempfile
from .domain import aware


class SourceCooldown:
    def __init__(self, root):
        self.root=Path(root)/'modules/tms_orders'
        self.path=self.root/'source-cooldown.json'

    def check(self):
        if any(p.is_symlink() for p in (self.root.parent,self.root,self.path)):
            raise ValueError('TMS冷却路径不能使用符号链接')

    def until(self, now=None):
        self.check()
        if not self.path.exists():return None
        if self.path.stat().st_size>4096:raise ValueError('TMS冷却状态不可读')
        try:value=json.loads(self.path.read_text()); until=aware(value['until'])
        except (ValueError,KeyError,TypeError):raise ValueError('TMS冷却状态不可读') from None
        return until if until>(now or datetime.now(timezone.utc)) else None

    def record(self, retry_after, now=None):
        self.check(); now=now or datetime.now(timezone.utc)
        # Missing/malformed server duration: conservative 15min; never zero.
        until=now+timedelta(minutes=15)
        try:
            if isinstance(retry_after,str) and retry_after.isascii() and retry_after.isdigit():
                until=now+timedelta(seconds=max(1,int(retry_after)))
            elif isinstance(retry_after,str):
                candidate=parsedate_to_datetime(retry_after)
                if candidate.tzinfo is not None and candidate>now:until=candidate.astimezone(timezone.utc)
        except (ValueError,OverflowError,TypeError):pass
        previous=self.until(now)
        if previous and previous>until:until=previous
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.cooldown-',dir=self.root)
        try:
            with os.fdopen(fd,'w') as f:
                json.dump({'origin':'https://tms.mabangerp.com','until':until.isoformat()},f)
                f.flush();os.fsync(f.fileno())
            os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)
        return until
