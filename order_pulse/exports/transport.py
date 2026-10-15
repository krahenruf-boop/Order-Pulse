"""Use normal, guarded page actions; never replay TMS private requests."""
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import time
from .model import ExportError
from ..tms_core.source_time import request_window
from ..tms_core.source_cooldown import SourceCooldown


class EgoExportTransport:
    def __init__(self, root, settings):
        self.settings = settings
        self.cooldown = SourceCooldown(root)
        self.deadline = None

    def execute(self, spec):
        waited = False
        retryable = spec.get('action') in ('query', 'export')
        for attempt in range(2):
            until = self.cooldown.until()
            if until and spec.get('action') != 'cleanup':
                if not retryable or waited:
                    raise ExportError('RATE_LIMITED')
                delay = max(0.0, (until-datetime.now(timezone.utc)).total_seconds()) + 1.0
                if self.deadline and time.monotonic()+delay+5 >= self.deadline:
                    raise ExportError('EXPORT_RUNTIME_LIMIT')
                time.sleep(delay);waited=True
            remaining = min(100, self.deadline-time.monotonic()) if self.deadline else 100
            if remaining <= 0: raise ExportError('EXPORT_RUNTIME_LIMIT')
            run_spec=dict(spec,operation_timeout_ms=max(1,int(remaining*1000)-1000))
            program = 'const INPUT=' + json.dumps(run_spec, ensure_ascii=True) + ';\n'
            program += Path(__file__).with_name('ego_export.js').read_text()
            env = {'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'TMPDIR': '/private/tmp'}
            try:
                p = subprocess.run([str(Path.home()/'.local/bin/ego-browser'), 'nodejs'], input=program,
                                   text=True, capture_output=True, timeout=remaining, env=env, shell=False)
            except subprocess.TimeoutExpired: raise ExportError('EXPORT_TIMEOUT') from None
            except OSError: raise ExportError('WAITING_CONNECTION') from None
            # No raw browser output goes to logs: source errors may contain private data.
            output = p.stdout + '\n' + p.stderr
            lines = [s.strip()[len('ORDER_PULSE_EXPORT:'):] for s in output.splitlines() if s.strip().startswith('ORDER_PULSE_EXPORT:')]
            if p.returncode or len(lines) != 1 or len(output) > 2_000_000:
                error=ExportError('EXPORT_TRANSPORT_FAILED')
                error.details=dict(stage='ego-process',returncode=p.returncode,marker_count=len(lines),
                                   stdout_bytes=len(p.stdout.encode()),stderr_bytes=len(p.stderr.encode()))
                raise error
            try: value = json.loads(lines[0])
            except ValueError: raise ExportError('EXPORT_TRANSPORT_FAILED') from None
            if not value.get('error'):
                return value
            code = value['error']
            if code == 'EXPORT_TIMEOUT' and retryable and not waited and attempt == 0:
                time.sleep(5);waited=True
                continue
            if code == 'RATE_LIMITED':
                until = self.cooldown.record(value.get('retry_after'))
                if retryable and not waited and attempt == 0:
                    delay = max(0.0, (until-datetime.now(timezone.utc)).total_seconds()) + 1.0
                    if self.deadline and time.monotonic()+delay+5 >= self.deadline:
                        raise ExportError('EXPORT_RUNTIME_LIMIT')
                    time.sleep(delay);waited=True
                    continue
            error = ExportError(code)
            error.details = value.get('details')
            raise error
        raise ExportError('RATE_LIMITED')

    def spec(self, action, partition, binding, scope, page_size=50):
        return dict(action=action, partition=partition, space_id=binding['space_id'], page_label=binding['page_label'],
                    account=binding['account'], shop_ids=scope['shop_ids'],
                    range=request_window(partition, scope['start'], scope['end']), page_size=page_size,
                    template=self.settings.template_name,
                    reconciliation_enabled=scope.get('reconciliation_contract')=='CURRENT_FIRST_V1',
                    max_export_rows=(self.settings.max_current_export_rows if partition=='current'
                                     else self.settings.max_completed_export_rows))

    def query(self, partition, binding, scope, page_size):
        return self.execute(self.spec('query', partition, binding, scope, page_size))

    def export(self, partition, binding, scope, page):
        spec = self.spec('export', partition, binding, scope, page['page_size'])
        spec.update(expected_total=page['total'], expected_order_ids=page['order_ids'])
        value = self.execute(spec)
        actual_total=value.pop('exported_count',page['total'])
        actual_ids=value.pop('exported_order_ids',page['order_ids'])
        reconciled=bool(value.pop('reconciliation_required',False))
        # The full signed URL exists only in this transient object, not receipts/DB.
        return dict(value, partition=partition, total=actual_total, start=scope['start'], end=scope['end'],
                    expected_order_ids=actual_ids if partition=='current' else None,
                    target_skus=list(scope['skus']),
                    shop_names=[s['name'] for s in binding['stores']],
                    list_check=page.get('list_check', []),
                    before_count=page.get('before_count',page['total']),
                    pre_export_count=actual_total,
                    reconciliation_required=reconciled)

    def cleanup(self, binding):
        # A bounded page reload removes transient download hooks, including after
        # the process was interrupted. Never navigate if user ownership changed.
        return self.execute(dict(action='cleanup', space_id=binding['space_id'], page_label=binding['page_label']))
