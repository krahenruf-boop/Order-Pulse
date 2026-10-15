"""Spike only: inspect legacy XLS in memory; emit schema and aggregates, never rows."""
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

# Isolated spike dependency, deliberately not an application/runtime requirement.
sys.path.insert(0, '/tmp/order-pulse-spike-deps')
import xlrd

BASE = ['订单金额', '付款时间', '店铺', '订单编号', '平台交易号', '快递单号',
        '库存SKU', '平台SKU', '数量', '商品名称']
ALLOWED = set(BASE + ['币种', '平台ID', '平台类型'])
TYPES = {0: 'empty', 1: 'text', 2: 'number', 3: 'date', 4: 'boolean', 5: 'error', 6: 'blank'}


def identifier(cell):
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return ''
    if cell.ctype != xlrd.XL_CELL_TEXT or not isinstance(cell.value, str):
        raise ValueError('NUMERIC_IDENTIFIER_REJECTED')
    return cell.value.strip()


def number(value):
    n = Decimal(str(value))
    if not n.is_finite():
        raise ValueError('INVALID_NUMBER')
    return n


def inspect_sheet(sheet, expected=None, scope=None):
    scope = scope or {}
    headers = [str(sheet.cell_value(0, c)).strip() for c in range(sheet.ncols)]
    if len(headers) != len(set(headers)) or not set(BASE) <= set(headers):
        raise ValueError('SCHEMA_CHANGED')
    if set(headers) - ALLOWED:
        raise ValueError('UNEXPECTED_COLUMNS_STOP_BEFORE_ROW_INSPECTION')
    rows = []
    id_columns = ['订单编号', '平台交易号', '快递单号']
    for r in range(1, sheet.nrows):
        row = dict(zip(headers, [sheet.cell_value(r, c) for c in range(sheet.ncols)]))
        for h in id_columns:
            row[h] = identifier(sheet.cell(r, headers.index(h)))
        if not (row['平台交易号'] or row['订单编号']):
            raise ValueError('ORDER_KEY_MISSING')
        qty = number(row['数量'])
        if qty < 0 or qty != qty.to_integral_value():
            raise ValueError('QUANTITY_INVALID')
        rows.append(row)
    groups = defaultdict(list)
    for row in rows:
        groups[row['平台交易号'] or row['订单编号']].append(row)
    amounts = {k: {number(r['订单金额']) for r in values} for k, values in groups.items()}
    conflicts = sum(len(values) > 1 for values in amounts.values())
    result = {
        'rows': len(rows), 'columns': sheet.ncols, 'headers': headers,
        'cell_types': {h: dict(Counter(TYPES[c.ctype] for c in sheet.col(i)[1:])) for i, h in enumerate(headers)},
        'id_text_only': {h: True for h in id_columns},
        'id_lengths': {h: dict(Counter(len(r[h]) for r in rows if r[h])) for h in id_columns},
        'unique_orders': len(groups), 'multi_row_orders': sum(len(v) > 1 for v in groups.values()),
        'repeated_amount_orders': sum(len(v) > 1 and len(amounts[k]) == 1 for k, v in groups.items()),
        'amount_conflict_orders': conflicts,
        'warnings': ['AMOUNT_CONFLICT'] if conflicts else [],
        'shops': dict(Counter(r['店铺'] for r in rows)),
        'payment_min': min((r['付款时间'] for r in rows), default=None),
        'payment_max': max((r['付款时间'] for r in rows), default=None),
        'quantities': dict(Counter(str(r['数量']) for r in rows)),
        'currencies': dict(Counter(r['币种'] for r in rows)) if '币种' in headers else None,
        'export_platforms': dict(Counter(str(r.get('平台类型', r.get('平台ID'))) for r in rows)) if set(headers) & {'平台ID', '平台类型'} else None,
        'row_amount_sum': str(sum((number(r['订单金额']) for r in rows), Decimal(0))),
        # Conflicted orders have no trusted amount: do not choose one silently.
        'order_amount_sum_once': None if conflicts else str(sum((next(iter(v)) for v in amounts.values()), Decimal(0))),
    }
    if expected is not None:
        wanted = {str(e['orderId']) for e in expected}
        actual = {r['订单编号'] for r in rows}
        result.update(selected_order_ids_exact=actual == wanted, selected_count=len(wanted),
                      missing_selected=len(wanted - actual), extra_orders=len(actual - wanted))
        if scope.get('all_filtered'):
            result.update(
                all_filtered_unique_count_matches_total=len(groups) == scope['total'],
                loaded_first_page_is_subset=wanted <= actual,
                payment_within_requested_window=all(scope['source_window'][0][:16] <= r['付款时间'] < scope['source_window'][1][:16] for r in rows),
                shops_in_authorized_tiktok_catalog=all(r['店铺'] in scope['shopNames'] for r in rows),
            )
        by_id = {str(e['orderId']): e for e in expected}
        compared = [r for r in rows if r['订单编号'] in by_id]
        result['payment_matches_source_utc_minus3_to_minute'] = all(
            r['付款时间'] == datetime.fromtimestamp(by_id[r['订单编号']]['orderTime'] / 1000,
            tz=timezone(timedelta(hours=-3))).strftime('%Y-%m-%d %H:%M') for r in compared)
        result['shop_names_match_source'] = all(r['店铺'] == by_id[r['订单编号']].get('shopName') for r in compared)
        expected_qty, actual_qty = defaultdict(Decimal), defaultdict(Decimal)
        for e in expected:
            for i in e.get('items', []):
                if i.get('productNum') is not None:
                    expected_qty[(str(e['orderId']), str(i.get('stockSku')))] += number(i['productNum'])
        for r in compared:
            actual_qty[(r['订单编号'], r['库存SKU'])] += number(r['数量'])
        result['quantity_matches_source_productNum_for_loaded_orders'] = actual_qty == expected_qty
        result['source_item_groups'] = len(expected_qty)
    result['status'] = 'PASS'  # Parsing status only; contract checks are separate booleans.
    return result


def main():
    data = Path(sys.argv[1]).read_bytes()
    result = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'magic_hex': data[:8].hex()}
    try:
        book = xlrd.open_workbook(file_contents=data)
        loaded = json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 else None
        expected = loaded.get('rows') if isinstance(loaded, dict) else loaded
        scope = loaded.get('scope', {}) if isinstance(loaded, dict) else {}
        result.update(sheet_names=book.sheet_names(), **inspect_sheet(book.sheet_by_index(0), expected, scope))
        book.release_resources()
    except Exception as exc:
        # Never include raw values, paths, response headers, or cell content in error output.
        result.update(status='FAIL', error_type=type(exc).__name__, error='XLS_VALIDATION_FAILED')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
