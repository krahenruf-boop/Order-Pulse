"""AOV order collection from the normal TMS order-list JSON responses."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import subprocess
import time

from .exports.model import ExportError
from .tms_core.domain import aware


FAST_ERRORS = {
    'WAITING_USER', 'WAITING_CONNECTION', 'AUTH_REQUIRED', 'PERMISSION_DENIED',
    'ACCOUNT_SCOPE_CHANGED', 'CATALOG_CHANGED', 'BROWSER_BUSY',
    'REQUEST_SCOPE_MISMATCH', 'FAST_PAGE_SIZE_UNSUPPORTED', 'FAST_HTTP_FAILED',
    'FAST_JSON_INVALID', 'FAST_PAGINATION_INCOMPLETE', 'FAST_PAGE_REPEATED',
    'FAST_PAGE_LOOP', 'FAST_ROW_INVALID', 'FAST_SOURCE_UNSTABLE',
    'FAST_ORDER_CONFLICT', 'FAST_QUANTITY_INVALID', 'FAST_TIMEOUT',
    'FAST_CANCELLED', 'FAST_TRANSPORT_FAILED',
}


@dataclass(frozen=True)
class OrderReadSettings:
    page_size: int = 500
    fallback_page_size: int = 200
    max_pages: int = 100
    max_runtime: int = 90
    max_output_bytes: int = 64_000_000
    max_drift_ratio: str = '0.05'

    def __post_init__(self):
        if self.page_size != 500 or self.fallback_page_size != 200:
            raise ValueError('FAST_SETTINGS_INVALID')
        if not 1 <= self.max_pages <= 200 or not 10 <= self.max_runtime <= 90:
            raise ValueError('FAST_SETTINGS_INVALID')
        if Decimal(self.max_drift_ratio) != Decimal('.05'):
            raise ValueError('FAST_SETTINGS_INVALID')


class EgoOrderTransport:
    def __init__(self, settings=None):
        self.settings = settings or OrderReadSettings()

    def collect(self, binding, scope, page_size):
        spec = dict(
            space_id=binding['space_id'], page_label=binding['page_label'],
            account=binding['account'], shop_ids=scope['shop_ids'],
            source_ranges=scope['source_ranges'], page_size=page_size,
            max_pages=self.settings.max_pages,
            operation_timeout_ms=self.settings.max_runtime * 1000,
            aov=scope.get('aov') is True,
            identity_shop_ids=scope.get('identity_shop_ids',scope['shop_ids']),
        )
        script = Path(__file__).with_name('ego_orders.js').read_text()
        program = 'const INPUT=' + json.dumps(spec, ensure_ascii=True) + ';\n' + script
        env = {'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
               'TMPDIR': '/private/tmp'}
        try:
            proc = subprocess.run(
                [str(Path.home()/'.local/bin/ego-browser'), 'nodejs'], input=program,
                text=True, capture_output=True, timeout=self.settings.max_runtime + 5,
                env=env, shell=False,
            )
        except subprocess.TimeoutExpired:
            raise ExportError('FAST_TIMEOUT') from None
        except OSError:
            raise ExportError('WAITING_CONNECTION') from None
        marker = 'ORDER_PULSE_ORDERS:'
        output_size = len(proc.stdout.encode()) + len(proc.stderr.encode())
        lines = [line.strip()[len(marker):] for line in (proc.stdout+'\n'+proc.stderr).splitlines()
                 if line.strip().startswith(marker)]
        if proc.returncode or len(lines) != 1 or output_size > self.settings.max_output_bytes:
            error = ExportError('FAST_TRANSPORT_FAILED')
            error.details = dict(returncode=proc.returncode, marker_count=len(lines),
                                 output_bytes=output_size)
            raise error
        try:
            value = json.loads(lines[0])
        except (ValueError, TypeError):
            raise ExportError('FAST_JSON_INVALID') from None
        if not isinstance(value, dict):
            raise ExportError('FAST_JSON_INVALID')
        if value.get('error'):
            code = value['error'] if value['error'] in FAST_ERRORS else 'FAST_TRANSPORT_FAILED'
            error = ExportError(code); error.details = value.get('details')
            raise error
        return value


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ExportError('FAST_ROW_INVALID')
    return value.strip()


def _quantity(item):
    for name in ('platformQuantity', 'productNum'):
        value = item.get(name)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if number.is_finite() and number > 0 and number == number.to_integral_value():
            return int(number)
    raise ExportError('FAST_QUANTITY_INVALID')


def _paid_at(value, start, end):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or value >= 2**53:
        raise ExportError('FAST_ROW_INVALID')
    try:
        stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)
    except (ValueError, OverflowError):
        raise ExportError('FAST_ROW_INVALID') from None
    if not start <= stamp < end:
        raise ExportError('FAST_ROW_INVALID')
    return stamp.isoformat()


def _amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ExportError('FAST_ROW_INVALID') from None
    if not amount.is_finite() or amount < 0:
        raise ExportError('FAST_ROW_INVALID')
    return str(amount)


def _order(raw, partition, scope):
    if not isinstance(raw, dict):
        raise ExportError('FAST_ROW_INVALID')
    key = next((_identifier(raw[name]) for name in ('platformOrderId', 'originalOrderId', 'orderId')
                if raw.get(name) is not None), None)
    row_id = _identifier(raw.get('id'))
    if key is None:
        raise ExportError('FAST_ROW_INVALID')
    if str(raw.get('shopId')) not in scope['shop_ids'] or raw.get('platformId') != 104:
        raise ExportError('FAST_ROW_INVALID')
    if str(raw.get('companyId')) != scope['account']['customer_id'] or str(raw.get('supplierId')) != scope['account']['supplier_id']:
        raise ExportError('ACCOUNT_SCOPE_CHANGED')
    shop = raw.get('shopName')
    if not scope.get('aov') and (not isinstance(shop, str) or not shop.strip() or len(shop) > 256):
        raise ExportError('FAST_ROW_INVALID')
    items = raw.get('itemList')
    if not isinstance(items, list) or (not items and not scope.get('aov')):
        raise ExportError('FAST_ROW_INVALID')
    clean_items = []
    for item in items:
        if not isinstance(item, dict):
            raise ExportError('FAST_ROW_INVALID')
        stock = item.get('stockSku')
        platform = item.get('platformSku')
        if stock is not None and (not isinstance(stock, str) or len(stock) > 256):
            raise ExportError('FAST_ROW_INVALID')
        if platform is not None and (not isinstance(platform, str) or len(platform) > 256):
            raise ExportError('FAST_ROW_INVALID')
        if not (stock or platform):
            raise ExportError('FAST_ROW_INVALID')
        clean_items.append(dict(stock_sku=stock or '', platform_sku=platform or '',
                                quantity=None if scope.get('aov') else _quantity(item), product_name=''))
    return dict(
        order_key=key, observed_order_key=key, source_row_id=row_id,
        shop_name=(shop or '').strip(), paid_at=None if scope.get('aov') and raw.get('orderTime') in (None,'') else _paid_at(raw.get('orderTime'), aware(scope['start']), aware(scope['end'])+(timedelta(microseconds=1) if scope.get('aov') else timedelta(0))),
        order_amount_rmb=None if scope.get('aov') and raw.get('orderFee') in (None,'') else _amount(raw.get('orderFee')), items=clean_items,
        partition=partition,
    )


def _page_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _validate_partition(batch, scope, page_size, max_drift):
    partition = batch.get('partition')
    if partition not in ('current', 'completed'):
        raise ExportError('FAST_JSON_INVALID')
    pages = batch.get('pages')
    first = batch.get('first_total'); last = batch.get('last_total')
    if (not isinstance(pages, list) or not pages or type(first) is not int or first < 0 or
            type(last) is not int or last < 0):
        raise ExportError('FAST_JSON_INVALID')
    drift = last - first
    ratio = Decimal(abs(drift)) / Decimal(max(first, 1))
    if ratio > max_drift:
        error = ExportError('FAST_SOURCE_UNSTABLE')
        error.details = dict(partition=partition, first_total=first, last_total=last,
                             drift_delta=drift, drift_ratio=str(ratio))
        raise error
    rows, receipts, previous = [], [], None
    raw_row_ids = set()
    for expected_page, page in enumerate(pages, 1):
        if (not isinstance(page, dict) or page.get('page') != expected_page or
                page.get('page_size') != page_size or type(page.get('reported_total')) is not int or
                page['reported_total'] < 0 or not isinstance(page.get('rows'), list) or
                len(page['rows']) > page_size):
            raise ExportError('FAST_PAGE_LOOP')
        current = []
        for raw in page['rows']:
            parsed = _order(raw, partition, scope)
            if parsed['source_row_id'] in current:
                raise ExportError('FAST_PAGE_REPEATED')
            current.append(parsed['source_row_id'])
            raw_row_ids.add(parsed['source_row_id'])
            rows.append(parsed)
        if previous is not None and current and current == previous:
            raise ExportError('FAST_PAGE_REPEATED')
        if (sum(len(x['rows']) for x in pages[:expected_page-1]) + len(current) < page['reported_total']
                and len(current) != page_size):
            raise ExportError('FAST_PAGINATION_INCOMPLETE')
        receipts.append(dict(
            partition=partition, page=expected_page, page_size=page_size,
            reported_total=page['reported_total'], row_count=len(current),
            first_order_key_hash=_page_hash(current[0]) if current else None,
            last_order_key_hash=_page_hash(current[-1]) if current else None,
        ))
        previous = current
    if batch.get('termination') not in ('COUNT_REACHED', 'EMPTY'):
        raise ExportError('FAST_PAGINATION_INCOMPLETE')
    floor = min(first, last)
    if len(raw_row_ids) < floor:
        error = ExportError('FAST_PAGINATION_INCOMPLETE')
        error.details = dict(partition=partition, unique_rows=len(raw_row_ids),
                             first_total=first, last_total=last)
        raise error
    summary = dict(
        partition=partition, page_size=page_size, pages=len(pages), rows=len(rows),
        unique_rows=len(raw_row_ids), first_total=first, last_total=last,
        drift_delta=drift, drift_ratio=str(ratio.quantize(Decimal('.0001'))),
        elapsed_ms=batch.get('elapsed_ms'), page_receipts=receipts,
    )
    return rows, summary


def _merge_orders(rows):
    merged = {}; duplicates = 0
    for row in rows:
        key = row['order_key']
        if key not in merged:
            merged[key] = row; continue
        duplicates += 1
        old = merged[key]
        for field in ('shop_name', 'paid_at', 'order_amount_rmb'):
            if old[field] != row[field]:
                raise ExportError('FAST_ORDER_CONFLICT')
        def score(value):
            return (len(value['items']), sum(bool(i['stock_sku']) for i in value['items']),
                    value['partition'] == 'completed')
        if score(row) > score(old):
            merged[key] = row
    values = []
    for value in merged.values():
        clean = dict(value); clean.pop('partition', None); clean.pop('source_row_id', None)
        values.append(clean)
    values.sort(key=lambda x: x['order_key'])
    return values, duplicates

