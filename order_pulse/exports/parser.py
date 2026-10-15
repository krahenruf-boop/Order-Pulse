"""Legacy BIFF XLS only. Strict column allowlist before inspecting any row."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import xlrd
from .model import ExportError
from ..tms_core.domain import aware, BUSINESS_TZ

BASE = ('订单金额', '付款时间', '店铺', '订单编号', '平台交易号', '快递单号',
        '库存SKU', '平台SKU', '数量', '商品名称')
EXTRA = ('币种', '平台ID', '原始订单金额', '汇率')
MONEY_CONTRACT = 'TMS_RMB_ONLY_V1'


def decimal(value, code='EXPORT_NUMBER_INVALID'):
    try: n = Decimal(str(value))
    except (InvalidOperation, ValueError): raise ExportError(code) from None
    if not n.is_finite(): raise ExportError(code)
    return n


def identifier(cell):
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK): return ''
    if cell.ctype != xlrd.XL_CELL_TEXT: raise ExportError('EXPORT_NUMERIC_ID')
    return cell.value.strip()


def fingerprint(item):
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def current_list_payment_times(batch, start, end):
    """Return the guarded Current-list payment timestamps by order number.

    Current exports are selected by the exact IDs returned by the filtered list.
    TMS has been observed to render a different payment clock in XLS for a small
    subset of those same IDs.  The list timestamp is the filter's native field,
    so it is authoritative only when its complete ID set still matches the
    selected/exported ID set.  Completed condition exports keep using XLS time.
    """
    if batch.get('partition') != 'current':
        return None
    expected = batch.get('expected_order_ids')
    checks = batch.get('list_check')
    if expected is None or not isinstance(checks, list):
        raise ExportError('EXPORT_SOURCE_CHANGED')
    result = {}
    for value in checks:
        if not isinstance(value, dict):
            raise ExportError('EXPORT_SOURCE_CHANGED')
        order_number, raw = value.get('order_number'), value.get('paid_at')
        if not isinstance(order_number, str) or not order_number or type(raw) is not int:
            raise ExportError('EXPORT_SOURCE_CHANGED')
        if order_number in result:
            raise ExportError('EXPORT_SOURCE_CHANGED')
        try:
            paid = datetime.fromtimestamp(raw / 1000, tz=timezone.utc).astimezone(BUSINESS_TZ)
        except (OverflowError, OSError, ValueError):
            raise ExportError('EXPORT_PAYMENT_INVALID') from None
        if not start <= paid < end:
            error = ExportError('EXPORT_PAYMENT_SCOPE')
            error.details = dict(stage='list_payment_scope', partition='current',
                                 paid_minute=paid.replace(second=0, microsecond=0).isoformat(),
                                 window_start=start.isoformat(), window_end=end.isoformat())
            raise error
        result[order_number] = paid
    if set(result) != set(expected):
        raise ExportError('EXPORT_ID_SET_CHANGED')
    return result


def read_xls(path, batch, settings):
    path = Path(path)
    if path.suffix.lower() != '.xls' or path.stat().st_size > settings.max_file_bytes:
        raise ExportError('EXPORT_FILE_INVALID')
    data = path.read_bytes()
    if data[:8] != bytes.fromhex('d0cf11e0a1b11ae1'): raise ExportError('EXPORT_FILE_INVALID')
    try: book = xlrd.open_workbook(file_contents=data)
    except Exception: raise ExportError('EXPORT_FILE_INVALID') from None
    try:
        if book.nsheets != 1: raise ExportError('EXPORT_SCHEMA_CHANGED')
        sheet = book.sheet_by_index(0)
        if sheet.nrows < 1 or sheet.nrows - 1 > settings.max_rows: raise ExportError('EXPORT_ROW_LIMIT')
        headers = [str(sheet.cell_value(0, c)).strip() for c in range(sheet.ncols)]
        required = set(BASE) | {'币种', '平台ID'}
        if len(headers) != len(set(headers)) or not required <= set(headers) or set(headers) - set(BASE + EXTRA):
            raise ExportError('EXPORT_SCHEMA_CHANGED')
        start, end = aware(batch['start']), aware(batch['end'])
        list_payment_times = current_list_payment_times(batch, start, end)
        rows, observed_ids = [], set()
        unmapped_sku_rows = 0
        xls_payment_mismatch_orders = set()
        shops = set(batch['shop_names'])
        for r in range(1, sheet.nrows):
            values = {h: sheet.cell_value(r, c) for c, h in enumerate(headers)}
            ids = {h: identifier(sheet.cell(r, headers.index(h))) for h in ('订单编号', '平台交易号', '快递单号')}
            key = ids['平台交易号'] or ids['订单编号']
            if not key or not ids['订单编号']: raise ExportError('EXPORT_ID_MISSING')
            if str(values['平台ID']).strip().lower() != 'tiktok': raise ExportError('EXPORT_NON_TIKTOK')
            if values['店铺'] not in shops: raise ExportError('EXPORT_SHOP_SCOPE')
            if str(values['币种']).strip().upper() not in ('RMB', 'CNY'): raise ExportError('EXPORT_CURRENCY_UNVERIFIED')
            try: xls_paid = datetime.strptime(values['付款时间'], '%Y-%m-%d %H:%M').replace(tzinfo=BUSINESS_TZ)
            except (ValueError, TypeError): raise ExportError('EXPORT_PAYMENT_INVALID') from None
            paid = list_payment_times.get(ids['订单编号']) if list_payment_times is not None else xls_paid
            if paid is None:
                raise ExportError('EXPORT_ID_SET_CHANGED')
            if list_payment_times is not None:
                if xls_paid != paid.replace(second=0, microsecond=0):
                    xls_payment_mismatch_orders.add(ids['订单编号'])
            # Completed condition exports have only the minute-precision XLS
            # value. Current selected-ID exports use the exact timestamp from
            # the already guarded list response, validated above.
            elif not paid < end or paid + timedelta(minutes=1) <= start:
                error = ExportError('EXPORT_PAYMENT_SCOPE')
                # Time values and partition only: enough to diagnose a source
                # boundary contract without retaining an order id or buyer data.
                error.details = dict(
                    stage='xls_payment_scope', partition=batch['partition'],
                    paid_minute=paid.isoformat(), window_start=start.isoformat(),
                    window_end=end.isoformat(), xls_row_index=r,
                )
                raise error
            quantity = decimal(values['数量'])
            if quantity < 0 or quantity != quantity.to_integral_value(): raise ExportError('EXPORT_QUANTITY_INVALID')
            item = dict(stock_sku=str(values['库存SKU']).strip(), platform_sku=str(values['平台SKU']).strip(),
                        quantity=int(quantity), product_name=str(values['商品名称']))
            if not item['stock_sku'] and (not item['platform_sku'] or item['platform_sku'] in batch.get('target_skus', ())):
                error = ExportError('EXPORT_SKU_MISSING')
                error.details = dict(stage='xls_row', partition=batch['partition'], xls_row_index=r,
                                     platform_sku_present=bool(item['platform_sku']),
                                     order_hash=hashlib.sha256(key.encode()).hexdigest()[:12])
                raise error
            if not item['stock_sku']:
                # The export covers every TikTok order, including products that
                # have no TMS inventory mapping.  They remain in the order/file
                # completeness checks but cannot match an exact target stock SKU.
                unmapped_sku_rows += 1
            amount = decimal(values['订单金额'])
            original = decimal(values['原始订单金额']) if '原始订单金额' in values else None
            rate = decimal(values['汇率']) if '汇率' in values else None
            rows.append(dict(order_key=key, order_number=ids['订单编号'], shop_name=values['店铺'],
                             paid_at=paid.isoformat(), payment_precision='minute', item=item,
                             order_amount_rmb=str(amount), original_amount_unverified=str(original) if original is not None else None,
                             execution_fx_rate=str(rate) if rate is not None else None))
            observed_ids.add(ids['订单编号'])
        expected_counts=set(batch.get('acceptable_order_counts') or [batch['total']])
        if len(observed_ids) not in expected_counts:
            error=ExportError('EXPORT_INCOMPLETE')
            error.details=dict(stage='xls_count',partition=batch['partition'],expected_counts=sorted(expected_counts),
                               export_order_count=len(observed_ids),xls_row_count=len(rows))
            raise error
        if batch.get('expected_order_ids') is not None and set(batch['expected_order_ids']) != observed_ids:
            raise ExportError('EXPORT_ID_SET_CHANGED')
        result=batch.get('reconciliation_result','STABLE')
        if len(observed_ids)!=batch['total']:result='RECONCILIATION_REQUIRED'
        return dict(file_hash=hashlib.sha256(data).hexdigest(), rows=rows,
                    evidence=dict(headers=headers, rows=len(rows), orders=len(observed_ids),
                                  unmapped_sku_rows=unmapped_sku_rows,
                                  byte_count=len(data), partition=batch['partition'],
                                  start=batch['start'], end=batch['end'],
                                  source=batch['partition'],slice_start=batch['start'],slice_end=batch['end'],
                                  expected_count=batch['total'],exported_count=len(observed_ids),
                                  before_count=batch.get('before_count',batch['total']),
                                  after_count=batch.get('after_count',batch['total']),
                                  drift_delta=batch.get('after_count',batch['total'])-batch.get('before_count',batch['total']),
                                  reconciliation_result=result,
                                  payment_time_source=('CURRENT_LIST_JSON' if list_payment_times is not None else 'XLS'),
                                  xls_payment_mismatch_orders=len(xls_payment_mismatch_orders)))
    finally: book.release_resources()


def aggregate(files, target_skus, scope):
    """Multiset union: retain repeated item occurrences within a complete export,
    but never accumulate the same occurrence again from retries or another file.
    A changed quantity under the same item signature is ambiguous and fails closed.
    """
    orders, seen_files, evidence = {}, set(), []
    for file in files:
        if file['file_hash'] in seen_files: continue
        seen_files.add(file['file_hash']); evidence.append(dict(file['evidence'], file_hash=file['file_hash']))
        per_file = {}
        for row in file['rows']:
            key = row['order_key']
            entry = per_file.setdefault(key, dict(rows=[], items=Counter(), item_values={}, quantities={}, sku_versions={}))
            entry['rows'].append(row)
            item = row['item']; fp = fingerprint(item)
            entry['items'][fp] += 1; entry['item_values'][fp] = item
            signature = fingerprint({k: v for k, v in item.items() if k != 'quantity'})
            entry['quantities'].setdefault(signature, set()).add(item['quantity'])
            sku_pair = (item['stock_sku'], item['platform_sku'])
            entry['sku_versions'].setdefault(sku_pair, Counter())[fp] += 1
        for key, group in per_file.items():
            for row in group['rows']:
                o = orders.setdefault(key, dict(order_key=key, observed_order_key=key,
                    shop_name=row['shop_name'], paid_at=row['paid_at'], payment_precision='minute',
                    amounts=set(), rates=set(), originals=set(), item_counts=Counter(), item_values={}, quantities={}, sku_versions={}))
                if o['shop_name'] != row['shop_name'] or o['paid_at'] != row['paid_at']:
                    raise ExportError('EXPORT_ORDER_CONFLICT')
                o['amounts'].add(decimal(row['order_amount_rmb']))
                if row.get('execution_fx_rate') is not None: o['rates'].add(decimal(row['execution_fx_rate']))
                if row.get('original_amount_unverified') is not None: o['originals'].add(decimal(row['original_amount_unverified']))
            for signature, quantities in group['quantities'].items():
                if signature in o['quantities'] and o['quantities'][signature] != quantities:
                    raise ExportError('EXPORT_ITEM_CONFLICT')
                o['quantities'][signature] = quantities
            for sku_pair, version in group['sku_versions'].items():
                if sku_pair in o['sku_versions'] and o['sku_versions'][sku_pair] != version:
                    raise ExportError('EXPORT_ITEM_CONFLICT')
                o['sku_versions'][sku_pair] = version
            o['item_counts'] |= group['items']
            o['item_values'].update(group['item_values'])
    def empty(): return dict(sku_order_count=0, sku_quantity=0, quantity_estimated=False)
    def metric(): return dict(product_order_count=0, sku_quantity=0, skus={s: empty() for s in target_skus},
                             amount_rmb='0', amount_brl=None, amount_conflict=False)
    overall = metric(); stores = {}; result = []
    for key, order in sorted(orders.items()):
        paid = aware(order['paid_at'])
        if not paid < aware(scope['end']) or paid + timedelta(minutes=1) <= aware(scope['start']): continue
        items = [order['item_values'][fp] for fp, n in sorted(order['item_counts'].items()) for _ in range(n)]
        selected = [i for i in items if i['stock_sku'] in target_skus]
        if not selected: continue
        conflict = any(len(order[k]) > 1 for k in ('amounts', 'rates', 'originals'))
        amount = None if conflict else next(iter(order['amounts']))
        shop = order['shop_name']; store = stores.setdefault(shop, metric())
        for m in (overall, store):
            m['product_order_count'] += 1
            m['sku_quantity'] += sum(i['quantity'] for i in selected)
            m['amount_conflict'] |= conflict
            m['amount_rmb'] = None if m['amount_conflict'] else str(decimal(m['amount_rmb']) + amount)
            for sku in target_skus:
                matching = [i for i in selected if i['stock_sku'] == sku]
                m['skus'][sku]['sku_order_count'] += bool(matching)
                m['skus'][sku]['sku_quantity'] += sum(i['quantity'] for i in matching)
        result.append(dict(order_key=key, observed_order_key=key, shop_name=shop, paid_at=order['paid_at'],
                           payment_precision='minute', items=items, amount_conflict=conflict,
                           warnings=['AMOUNT_CONFLICT'] if conflict else [],
                           order_amount_rmb=str(amount) if amount is not None else None,
                           original_amount_brl=None, execution_fx_rate=None,
                           money_contract=MONEY_CONTRACT))
    overall.update(stores=dict(sorted(stores.items(), key=lambda kv: (-kv[1]['product_order_count'], kv[0]))),
                   quantity_estimated_order_count=0, money_contract=MONEY_CONTRACT,
                   amount_note='金额数据异常' if overall['amount_conflict'] else '原币金额待验证',
                   warnings=['AMOUNT_CONFLICT'] if overall['amount_conflict'] else [],
                   coverage_note='当前订单 + 已完成已发货；忽略退货、作废、归档等状态。')
    return overall, result, evidence


def aggregate_all(files, scope):
    """Return every strictly validated order for shared order consumers.

    This deliberately mirrors the already accepted cross-file multiset rules in
    ``aggregate``.  It does not apply a target-SKU filter and fails
    closed when an order cannot provide one unambiguous whole-order amount.
    """
    orders, seen_files, evidence = {}, set(), []
    for file in files:
        if file['file_hash'] in seen_files:
            continue
        seen_files.add(file['file_hash'])
        evidence.append(dict(file['evidence'], file_hash=file['file_hash']))
        per_file = {}
        for row in file['rows']:
            key = row['order_key']
            entry = per_file.setdefault(key, dict(rows=[], items=Counter(), item_values={},
                                                   quantities={}, sku_versions={}))
            entry['rows'].append(row)
            item = row['item']; fp = fingerprint(item)
            entry['items'][fp] += 1; entry['item_values'][fp] = item
            signature = fingerprint({k: v for k, v in item.items() if k != 'quantity'})
            entry['quantities'].setdefault(signature, set()).add(item['quantity'])
            pair = (item['stock_sku'], item['platform_sku'])
            entry['sku_versions'].setdefault(pair, Counter())[fp] += 1
        for key, group in per_file.items():
            for row in group['rows']:
                order = orders.setdefault(key, dict(
                    order_key=key, observed_order_key=key, shop_name=row['shop_name'],
                    paid_at=row['paid_at'], payment_precision='minute', amounts=set(),
                    item_counts=Counter(), item_values={}, quantities={}, sku_versions={}))
                if order['shop_name'] != row['shop_name'] or order['paid_at'] != row['paid_at']:
                    raise ExportError('EXPORT_ORDER_CONFLICT')
                order['amounts'].add(decimal(row['order_amount_rmb']))
            for signature, quantities in group['quantities'].items():
                if signature in order['quantities'] and order['quantities'][signature] != quantities:
                    raise ExportError('EXPORT_ITEM_CONFLICT')
                order['quantities'][signature] = quantities
            for pair, version in group['sku_versions'].items():
                if pair in order['sku_versions'] and order['sku_versions'][pair] != version:
                    raise ExportError('EXPORT_ITEM_CONFLICT')
                order['sku_versions'][pair] = version
            order['item_counts'] |= group['items']
            order['item_values'].update(group['item_values'])

    start, end = aware(scope['start']), aware(scope['end'])
    result = []
    for key, order in sorted(orders.items()):
        paid = aware(order['paid_at'])
        if not paid < end or paid + timedelta(minutes=1) <= start:
            continue
        if len(order['amounts']) != 1:
            raise ExportError('EXPORT_ORDER_CONFLICT')
        items = [order['item_values'][fp] for fp, count in sorted(order['item_counts'].items())
                 for _ in range(count)]
        if not items:
            raise ExportError('EXPORT_ITEM_MISSING')
        result.append(dict(order_key=key, observed_order_key=key,
                           shop_name=order['shop_name'], paid_at=order['paid_at'],
                           payment_precision='minute', items=items,
                           order_amount_rmb=str(next(iter(order['amounts']))),
                           money_contract=MONEY_CONTRACT))
    return result, evidence
