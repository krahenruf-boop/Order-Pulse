"""Pure dual-source reconciliation and rolling-window calculations.

Only normalized operational fields enter this module; buyer data is excluded.
All windows share one UTC cutoff. Reference currency never drives alerts.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json

WINDOWS = {'24h': 24, '72h': 72}
LOOKBACK_HOURS = 2 * max(WINDOWS.values())
RULE_WINDOWS = {'24h': 24, '48h': 48, '72h': 72, '7d': 168, 'cycle':2}
FIELDS = ('paid_at', 'money', 'payment_status', 'refund_status', 'skus')
PAID = {'PAID', 'PARTIALLY_REFUNDED'}
EXCLUDED = {'UNPAID', 'CANCELLED', 'VOID', 'FULL_REFUND'}
DEFAULT_RULE = dict(lower=None, upper=None, drop_pct=None, rise_pct=None, min_orders=10)


def instant(value):
    if not isinstance(value, str): raise ValueError('付款时间未确认')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None: raise ValueError('付款时间缺少时区')
    return result.astimezone(timezone.utc)


def number(value):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('金额必须为十进制字符串')
    try: result = Decimal(value)
    except InvalidOperation: raise ValueError('金额无效') from None
    if not result.is_finite() or result < 0 or result > Decimal('1e16'):
        raise ValueError('金额无效')
    return result


def money(value):
    if not isinstance(value, dict) or value.get('currency') != 'CNY' or value.get('basis') != 'ORDER_TOTAL':
        raise ValueError('订单金额口径未确认')
    return number(value.get('value'))


def rounded(value):
    return format(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), 'f')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def monitor_scope(shops):
    """Display labels do not change collection scope or alert rules."""
    result = deepcopy(shops)
    for shop in result:
        for product in shop.get('products', []):
            product.pop('display_name', None)
    return result


def group_scope_hash(shop, product):
    scope = monitor_scope([shop])[0]
    scope.pop('products', None)
    item = deepcopy(product)
    item.pop('display_name', None)
    return digest([scope, item])


def rule(value):
    if not isinstance(value, dict) or set(value)-set(DEFAULT_RULE): raise ValueError('告警规则字段无效')
    result = dict(DEFAULT_RULE, **value)
    for field in ('lower', 'upper', 'drop_pct', 'rise_pct'):
        result[field] = None if result[field] in ('', None) else format(number(result[field]), 'f')
    if result['lower'] is not None and result['upper'] is not None and number(result['lower']) >= number(result['upper']):
        raise ValueError('金额下限必须小于上限')
    if type(result['min_orders']) is not int or not 1 <= result['min_orders'] <= 100000:
        raise ValueError('最低订单数须为整数且不超过100000，低于10按10执行')
    # Legacy settings may contain a smaller value; the business floor is ten.
    result['min_orders']=max(10,result['min_orders'])
    if result['drop_pct'] is not None and number(result['drop_pct']) > 100:
        raise ValueError('下跌百分比不能超过100')
    return result

def ruleset(value):
    """Keep saved inactive rules for compatibility, but never evaluate them."""
    if not isinstance(value,dict) or set(value)-set(RULE_WINDOWS):raise ValueError('统计窗口无效')
    return {w:rule(value.get(w,{})) for w in RULE_WINDOWS if w in WINDOWS or w in value}


def reconcile(sources, shop, catalog_skus=None, *, tms_only=False, estimate_refunds=False):
    """Return canonical orders after complete coverage of required sources.

    A missing valid identity or conflicting duplicate invalidates the scope;
    no fuzzy matching, no fallback on errors, no invalid-field replacement.
    Explicit TMS-only shops or the daily-estimate policy may display verified
    paid orders with UNKNOWN refunds. The service separately gates estimated
    alert eligibility; missing identities, amounts or coverage still fail.
    """
    orders = {}; notes = dict(tms_orders=0, miaoshou_orders=0, matched=0, filled=0, differences=0, excluded=0,unmapped_skus=[],invalid_orders=[])
    required=('tms',) if tms_only else ('miaoshou','tms')
    notes['coverage']={s:sources.get(s,{}).get('coverage','MISSING') for s in required}
    if tms_only:notes['coverage']['miaoshou']='NOT_APPLICABLE'
    notes['refund_unverified']=0
    errors = []
    for source in required:
        batch = sources.get(source, {})
        if batch.get('coverage') != 'COMPLETE':
            return [], notes, [batch.get('error') or source.upper()+'_INCOMPLETE']
        seen = {}
        for raw in batch.get('orders', []):
            pid = raw.get('platform_order_id')
            if not isinstance(pid, str) or not pid.strip() or len(pid)>256:
                errors.append('ORDER_ID_UNVERIFIED'); continue
            if raw.get('shop_id') != shop[source+'_shop_id']:
                errors.append('SHOP_SCOPE_MISMATCH'); continue
            clean = {f: deepcopy(raw.get(f)) for f in FIELDS}
            if clean.get('paid_at'):
                try:clean['paid_at']=instant(clean['paid_at']).isoformat()
                except (TypeError,ValueError):pass
            if isinstance(clean.get('money'),dict):
                try:clean['money']={**clean['money'],'value':format(money(clean['money']).normalize(),'f')}
                except (TypeError,ValueError):pass
            # SKU aliases are exact and source-specific. Unknown SKUs remain
            # visible to coverage checks; names never infer product membership.
            if isinstance(clean['skus'], list):
                aliases = shop.get('sku_aliases', {}).get(source, {})
                clean['skus'] = sorted({aliases.get(s, s) for s in clean['skus'] if isinstance(s,str) and s})
            if pid in seen:
                if seen[pid] != clean: errors.append('DUPLICATE_ORDER_CONFLICT')
                continue
            seen[pid] = clean; notes[source+'_orders'] += 1
            entry = orders.setdefault(pid, dict(platform_order_id=pid, fields={}, provenance={}, differences=[]))
            if entry['fields']: notes['matched'] += 1
            for field, value in clean.items():
                if value is None or value == '' or field=='skus' and value==[]: continue
                old = entry['fields'].get(field)
                if old is not None and old != value:
                    entry['differences'].append(dict(field=field,tms_value=value,miaoshou_value=old)); notes['differences'] += 1
                entry['fields'][field] = value; entry['provenance'][field] = source
    result = []
    for pid, item in orders.items():
        f = item['fields']
        if f.get('payment_status') in EXCLUDED or f.get('refund_status') == 'FULL':
            notes['excluded'] += 1; continue
        try:
            unknown_refund=(tms_only or estimate_refunds) and f.get('refund_status') in (None,'UNKNOWN')
            if f.get('payment_status') not in PAID or not (f.get('refund_status') in ('NONE', 'PARTIAL') or unknown_refund):
                raise ValueError('ORDER_STATUS_UNVERIFIED')
            paid = instant(f.get('paid_at')); amount = money(f.get('money'))
            if not isinstance(f.get('skus'), list) or not f['skus']: raise ValueError('SKU_UNVERIFIED')
        except (ValueError, TypeError):
            issues=[]
            if f.get('payment_status') not in PAID:issues.append('付款状态缺失' if not f.get('payment_status') else '付款状态未识别')
            if f.get('refund_status') not in ('NONE','PARTIAL'):issues.append('退款状态缺失' if not f.get('refund_status') else '退款状态未识别')
            try:instant(f.get('paid_at'))
            except (ValueError,TypeError):issues.append('付款时间未确认')
            try:money(f.get('money'))
            except (ValueError,TypeError):issues.append('金额口径未确认')
            if not isinstance(f.get('skus'),list) or not f.get('skus'):issues.append('SKU缺失')
            if len(notes['invalid_orders'])<50:notes['invalid_orders'].append(dict(platform_order_id=pid,issues=issues))
            errors.append('ORDER_FIELDS_UNVERIFIED'); continue
        if catalog_skus is not None:
            unknown=set(f['skus'])-set(catalog_skus)
            if unknown:
                errors.append('SKU_MAPPING_REQUIRED');notes['unmapped_skus']=sorted(set(notes['unmapped_skus'])|unknown)[:100]
        fills = [k for k,v in item['provenance'].items() if v=='miaoshou']
        notes['filled'] += len(fills)
        if unknown_refund:notes['refund_unverified']+=1
        result.append(dict(order_key=digest([shop['id'],pid]),platform_order_id=pid, paid_at=paid.isoformat(), amount_rmb=format(amount,'f'),
                           skus=f['skus'], refund_status='UNKNOWN' if unknown_refund else f['refund_status'], provenance=item['provenance'],
                           differences=item['differences']))
    return result, notes, sorted(set(errors))


def calculate(orders, cutoff, skus, rules, fx=None, *, windows=None, previous_complete=True):
    end = instant(cutoff); members = set(skus); values = []
    for o in orders:
        if members.intersection(o['skus']): values.append((instant(o['paid_at']), number(o['amount_rmb'])))
    results = {}
    with localcontext() as ctx:
        ctx.prec = 48
        for key, hours in (windows or WINDOWS).items():
            start = end-timedelta(hours=hours); previous = start-timedelta(hours=hours)
            current = [v for t,v in values if start <= t < end]
            prior = [v for t,v in values if previous <= t < start] if previous_complete else []
            r = rule(rules.get(key, {})); a = sum(current,Decimal(0))/len(current) if current else None
            b = sum(prior,Decimal(0))/len(prior) if prior else None
            change = (a-b)/b*100 if a is not None and b is not None and b else None
            enough = previous_complete and len(current)>=r['min_orders'] and len(prior)>=r['min_orders']
            directions=set();reasons=[]
            if enough:
                for field,test,side in [('lower',lambda x:a<x,'LOW'),('upper',lambda x:a>x,'HIGH'),
                                        ('drop_pct',lambda x:change is not None and change < -x,'LOW'),
                                        ('rise_pct',lambda x:change is not None and change>x,'HIGH')]:
                    if r[field] is not None and test(number(r[field])): directions.add(side);reasons.append(field)
            active = any(r[k] is not None for k in ('lower','upper','drop_pct','rise_pct'))
            valid_fx = fx and fx.get('fx_rate') is not None
            rate = number(fx['fx_rate']) if valid_fx else None
            results[key]=dict(start=start.isoformat(),end=end.isoformat(),previous_start=previous.isoformat(),
                current_count=len(current),previous_count=len(prior) if previous_complete else None,previous_complete=previous_complete,period_hours=hours,partial_refund_count=sum(1 for o in orders if members.intersection(o['skus']) and o.get('refund_status')=='PARTIAL' and start<=instant(o['paid_at'])<end),current_total=rounded(sum(current,Decimal(0))),
                previous_total=rounded(sum(prior,Decimal(0))) if previous_complete else None,aov_rmb=rounded(a) if a is not None else None,
                previous_aov_rmb=rounded(b) if b is not None else None,change_pct=rounded(change) if change is not None else None,
                aov_reference=rounded(a*rate) if a is not None and rate else None,
                previous_aov_reference=rounded(b*rate) if b is not None and rate else None,
                status='INSUFFICIENT' if not enough else 'UNCONFIGURED' if not active else 'ABNORMAL' if directions else 'NORMAL',
                directions=sorted(directions),reasons=reasons,rule=r)
            if key=='cycle':results[key]['detection_interval_hours']=hours
    return results


def transition(before, metric, slot, automatic, revision):
    """Manual observations never alter automatic state or send notifications."""
    if not automatic: return deepcopy(before or {}), None
    state = deepcopy(before or {})
    if state.get('revision') != revision: state = dict(revision=revision)
    if state.get('last_slot') == slot: return state, None
    previous_slot = state.get('last_slot'); state['last_slot'] = slot
    consecutive = previous_slot and instant(slot)-instant(previous_slot)==timedelta(hours=metric.get('detection_interval_hours',2))
    directions = metric.get('directions', []); signature = ','.join(directions)
    if metric.get('status') not in ('NORMAL','ABNORMAL'):
        state.update(candidate=None, streak=0); return state,None
    if not signature:
        recovery = bool(state.get('active'))
        state.update(active=None,candidate=None,streak=0)
        return state,'RECOVERY' if recovery else None
    if state.get('active') == signature: return state,None
    streak = state.get('streak',0)+1 if consecutive and state.get('candidate')==signature else 1
    state.update(candidate=signature,streak=streak)
    if streak < 2: return state,None
    state.update(active=signature, candidate=None, streak=0)
    return state,'ABNORMAL'
