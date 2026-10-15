import time
from pathlib import Path
from urllib.parse import urlparse
import urllib.request
from .model import ExportError, ExportSettings
from .slicing import Budget, CurrentOrdersExportAdapter, CompletedOrdersExportAdapter
from .parser import read_xls, aggregate, aggregate_all
from .temporary import TemporaryExports
from .transport import EgoExportTransport


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def download(url, limit):
    u = urlparse(url)
    if u.scheme != 'https' or u.netloc != 'tms-cos.mabangerp.com' or not u.path.endswith('.xls') or u.username:
        raise ExportError('EXPORT_URL_INVALID')
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(urllib.request.Request(url), timeout=45) as response:
            if response.status != 200: raise ExportError('EXPORT_DOWNLOAD_FAILED')
            if response.headers.get_content_type() != 'application/vnd.ms-excel':
                raise ExportError('EXPORT_MIME_INVALID')
            data, started = bytearray(), time.monotonic()
            while True:
                chunk = response.read(min(65536, limit + 1 - len(data)))
                if not chunk: break
                data.extend(chunk)
                if len(data) > limit: raise ExportError('EXPORT_FILE_LIMIT')
                if time.monotonic() - started > 60: raise ExportError('EXPORT_TIMEOUT')
            return bytes(data)
    except ExportError: raise
    except Exception: raise ExportError('EXPORT_DOWNLOAD_FAILED') from None


class ExportConnector:
    name = 'export'
    fallback_enabled = False

    def __init__(self, root, settings=None, transport=None, downloader=download, temporary=None):
        self.settings = settings or ExportSettings()
        self.transport = transport or EgoExportTransport(root, self.settings)
        self.downloader = downloader
        self.temporary = temporary or TemporaryExports()
        self.current = CurrentOrdersExportAdapter(self.transport, self.settings)
        self.completed = CompletedOrdersExportAdapter(self.transport, self.settings)

    def startup(self): return self.temporary.cleanup_expired()

    def collect(self, binding, scope, still_valid=lambda: True):
        budget = Budget(self.settings, still_valid)
        self.transport.deadline=budget.started+self.settings.max_export_runtime
        batches, receipts = [], []
        if set(scope['partitions']) != {'current', 'completed'}: raise ExportError('EXPORT_PARTITION_MISSING')
        try:
            # The full-order contract freezes one payment deadline and reads the moving
            # source in its safe direction: current first, completed second.
            # A move can then create a duplicate, which the strict order-key
            # union removes.  The filtered export contract remains unchanged.
            adapters=(self.current,self.completed) if scope.get('reconciliation_contract')=='CURRENT_FIRST_V1' else (self.completed,self.current)
            for adapter in adapters:
                b, r = adapter.collect(binding, scope, budget)
                batches.extend(b); receipts.extend(r)
            return dict(batches=batches, receipts=receipts, budget=budget)
        finally:
            self.transport.deadline=None  # bounded cleanup is allowed after expiry
            self.transport.cleanup(binding)

    def _parse_files(self, captured, workspace):
        """Download and parse captured files after the browser lease is released."""
        parsed, size = [], 0
        budget = captured['budget']
        for index, batch in enumerate(captured['batches']):
            budget.check()
            data = self.downloader(batch['url'], self.settings.max_file_bytes)
            filename=Path(urlparse(batch['url']).path).name
            # Inspect only this exact, freshly observed export name. Never scan or
            # delete unrelated Downloads content. A copy is a failed lifecycle.
            if (Path.home()/'Downloads'/filename).exists():
                raise ExportError('EXPORT_DOWNLOAD_NOT_CONTROLLED')
            size += len(data)
            if size > self.settings.max_total_bytes: raise ExportError('EXPORT_FILE_LIMIT')
            path = self.temporary.write(workspace, f'{index:03d}.xls', data)
            parsed_file=read_xls(path, batch, self.settings)
            parsed_file['evidence'].update(download_filename=filename,downloads_copy_exists=False)
            parsed.append(parsed_file)
            budget.check()
        return parsed

    def finalize(self, captured, scope, workspace):
        """Caller MUST release its lease before entering this local phase."""
        parsed = self._parse_files(captured, workspace)
        metrics, orders, files = aggregate(parsed, scope['skus'], scope)
        # Keep only aggregate reconciliation evidence, not whole export contents.
        from collections import defaultdict
        skus_by_order=defaultdict(set)
        for file in parsed:
            for row in file['rows']:skus_by_order[row['order_key']].add(row['item']['stock_sku'])
        reconciliation=dict(source_rows=sum(len(f['rows']) for f in parsed),
            all_export_orders=len(skus_by_order),multi_sku_orders=sum(len(s)>1 for s in skus_by_order.values()),
            target_multi_sku_orders=sum(set(scope['skus'])<=s for s in skus_by_order.values()),
            duplicate_files_idempotent=aggregate(parsed+parsed,scope['skus'],scope)[0]==metrics)
        return dict(metrics=metrics, orders=orders, receipts=captured['receipts'],
                    exports=files, source_mode=self.name,
                    stores=[dict(shop_id=name, name=name) for name in metrics['stores']],
                    reconciliation=reconciliation,
                    browser_downloads_suppressed=sum(b['suppressed_downloads'] for b in captured['batches']))

    def finalize_all(self, captured, scope, workspace):
        """Strict full-order view for shared order consumers."""
        parsed = self._parse_files(captured, workspace)
        orders, files = aggregate_all(parsed, scope)
        all_keys={row['order_key'] for item in parsed for row in item['rows']}
        all_identities=all_keys|{row['order_number'] for item in parsed for row in item['rows']}
        candidates={key for batch in captured['batches'] for key in batch.get('reconciliation_candidate_ids',())}
        unexplained=candidates-all_identities
        if unexplained:
            import hashlib
            error=ExportError('EXPORT_UNEXPLAINED_GAP')
            error.details=dict(stage='final-reconciliation',missing_count=len(unexplained),
                               missing_hashes=sorted(hashlib.sha256(x.encode()).hexdigest()[:12] for x in unexplained)[:20])
            raise error
        by_source={name:{row['order_key'] for item in parsed if item['evidence']['partition']==name for row in item['rows']}
                   for name in ('current','completed')}
        diagnostics=[{key:item['evidence'].get(key) for key in ('source','slice_start','slice_end','expected_count',
                     'exported_count','before_count','after_count','drift_delta','reconciliation_result')}
                     for item in parsed]
        reconciliation=dict(
            reconciliation_result='PASS',
            successful_slices=len(parsed),
            file_count=len(parsed),
            unique_file_hashes=len({item['file_hash'] for item in parsed}),
            unique_order_keys=len(all_keys),
            cross_source_duplicates=len(by_source['current'] & by_source['completed']),
            unexplained_missing_orders=0,
            reconciliation_required_slices=sum(x['reconciliation_result']=='RECONCILIATION_REQUIRED' for x in diagnostics),
            slices=diagnostics,
        )
        return dict(orders=orders, receipts=captured['receipts'], exports=files,
                    source_mode=self.name,
                    reconciliation=reconciliation,
                    browser_downloads_suppressed=sum(b['suppressed_downloads'] for b in captured['batches']))
