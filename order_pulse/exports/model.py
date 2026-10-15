from dataclasses import dataclass
from typing import Protocol


class ExportError(Exception):
    """Public codes only: never include a cell, response body or download URL."""


@dataclass(frozen=True)
class ExportSettings:
    max_current_export_rows: int = 500
    max_completed_export_rows: int = 2000
    max_export_slices: int = 32
    max_export_runtime: int = 900
    max_file_bytes: int = 50_000_000
    max_total_bytes: int = 200_000_000
    max_rows: int = 100_000
    template_name: str = '1'

    def __post_init__(self):
        bounds = {'max_current_export_rows': (1, 500), 'max_completed_export_rows': (1, 2000),
                  'max_export_slices': (1, 128),
                  'max_export_runtime': (30, 1800), 'max_file_bytes': (1024, 50_000_000),
                  'max_total_bytes': (1024, 400_000_000), 'max_rows': (1, 200_000)}
        for key, (low, high) in bounds.items():
            n = getattr(self, key)
            if type(n) is not int or not low <= n <= high:
                raise ValueError('EXPORT_SETTINGS_INVALID')
        if self.template_name != '1':
            raise ValueError('EXPORT_TEMPLATE_UNVERIFIED')

    @classmethod
    def load(cls, root):
        import json
        import os
        from pathlib import Path
        from dataclasses import asdict
        path=Path(root)/'config/export.json'
        if not path.exists():
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as f:json.dump(asdict(cls()),f,indent=2)
        if path.is_symlink() or path.stat().st_size>4096:raise ValueError('EXPORT_SETTINGS_INVALID')
        try:
            value=json.loads(path.read_text())
            return cls(**value)
        except (TypeError,json.JSONDecodeError):raise ValueError('EXPORT_SETTINGS_INVALID') from None


class OrderSource(Protocol):
    """Browser phase ends before finalize performs local work."""
    name: str
    def collect(self, binding, scope, still_valid): ...
    def finalize(self, captured, scope, workspace): ...
