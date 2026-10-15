"""Daily observed refund sample; extrapolation never rewrites order amounts.

The sample is a convenience sample of the linked shops, not a random sample.
Its point estimate is descriptive, with no population confidence guarantee.
Known full refunds still leave the main paid-order calculation immediately.
"""
import json
from datetime import timedelta
from decimal import Decimal
from .domain import instant, number, rounded, digest, PAID, LOOKBACK_HOURS
from .cycle import order_identity

POLICY = 'DAILY_REFUND_SAMPLE_V1'
REFRESH_HOURS = 24


def setup(db):
    db.execute('CREATE TABLE IF NOT EXISTS aov_monitor_refund_samples(key TEXT PRIMARY KEY,payload TEXT NOT NULL)')


def latest(store, shops, cutoff, *, max_age_hours=REFRESH_HOURS):
    """Reuse only real, complete shared reads under unchanged shop identities."""
    required={s['id']:order_identity(s) for s in shops if s.get('source_mode')!='TMS_ONLY'}
    if not required:return None
    with store.db() as db:
        rows=db.execute("""SELECT j.run_id,j.id,j.payload,j.result,r.payload
          FROM aov_monitor_jobs j JOIN aov_monitor_runs r ON r.id=j.run_id
          WHERE j.status='COMPLETE' AND j.id LIKE '30-refunds%'
          ORDER BY r.rowid DESC,j.id DESC""").fetchall()
    for rid,jid,raw,result,context in rows:
        j=json.loads(raw);c=json.loads(context);c=c.get('resume_context',c)
        if j.get('reused') or not j.get('shared') or not j.get('start') or not j.get('end'):continue
        if instant(j['end'])-instant(j['start'])<timedelta(hours=LOOKBACK_HOURS):continue
        age=instant(cutoff)-instant(j['end'])
        if age<timedelta(0) or max_age_hours is not None and age>=timedelta(hours=max_age_hours):continue
        available={s['id']:order_identity(s) for s in c.get('config',{}).get('shops',[]) if s['id'] in j.get('shop_ids',[])}
        if any(available.get(sid)!=identity for sid,identity in required.items()):continue
        value=json.loads(result or '{}')
        if not isinstance(value.get('refunds'),list):continue
        return dict(run_id=rid,job_id=jid,start=j['start'],end=j['end'],observed_at=j.get('observed_at') or j.get('finished_at') or j['end'])
    return None


def result(store,receipt):
    with store.db() as db:
        row=db.execute("SELECT result FROM aov_monitor_jobs WHERE run_id=? AND id=? AND status='COMPLETE'",(receipt['run_id'],receipt['job_id'])).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def describe(store, receipt, shops):
    """Same paid-time interval and unique shop/order identity for both sides."""
    from . import incremental
    from .sources import normalize_miaoshou
    if not receipt:return dict(status='UNAVAILABLE',reason='暂无有效退款样本')
    dual=[s for s in shops if s.get('source_mode')!='TMS_ONLY']
    key=digest([receipt['run_id'],receipt['job_id'],sorted(order_identity(s) for s in dual)])
    with store.db() as db:
        cached=db.execute('SELECT payload FROM aov_monitor_refund_samples WHERE key=?',(key,)).fetchone()
    if cached:return json.loads(cached[0])
    refunds=result(store,receipt).get('refunds')
    if not isinstance(refunds,list):return dict(status='UNAVAILABLE',reason='退款样本未完成')
    n=full=partial=0;total=removed=Decimal(0);loaded={};seen={};sample_shops=0
    for shop in dual:
        raw,gaps=incremental.read(store,shop,'miaoshou',receipt['start'],receipt['end'],loaded)
        if gaps:return dict(status='UNAVAILABLE',reason='样本订单范围未覆盖完整')
        sample_shops+=1
        own=[x for x in refunds if x['shop_id']==shop['miaoshou_shop_id']]
        for row in raw:
            try:r=normalize_miaoshou(row,own)
            except (ValueError,KeyError,TypeError):return dict(status='UNAVAILABLE',reason='退款样本字段待核验')
            identity=(shop['id'],r['platform_order_id'])
            if identity in seen:
                if seen[identity]!=r:return dict(status='UNAVAILABLE',reason='退款样本订单冲突')
                continue
            seen[identity]=r
            # Cancelled/unpaid orders are already excluded independently; do not
            # attribute their effect to refunds or count repeated after-sales rows.
            if r['payment_status'] not in PAID:continue
            amount=number(r['money']['value']);n+=1;total+=amount
            if r['refund_status']=='FULL':full+=1;removed+=amount
            elif r['refund_status']=='PARTIAL':partial+=1
    order_rate=Decimal(full)/n if n else None
    amount_rate=removed/total if total else None
    impact=((1-amount_rate)/(1-order_rate)-1)*100 if amount_rate is not None and order_rate is not None and order_rate<1 else None
    value=dict(status='AVAILABLE' if n>=10 and impact is not None else 'INSUFFICIENT',
        sample_orders=n,sample_shops=sample_shops,full_refunds=full,partial_refunds=partial,
        sample_amount_rmb=rounded(total),full_refund_amount_rmb=rounded(removed),
        order_rate=str(order_rate) if order_rate is not None else None,
        amount_rate=str(amount_rate) if amount_rate is not None else None,
        order_pct=rounded(order_rate*100) if order_rate is not None else None,
        amount_pct=rounded(amount_rate*100) if amount_rate is not None else None,
        aov_impact_pct=rounded(impact) if impact is not None else None,
        observed_at=receipt['observed_at'],start=receipt['start'],end=receipt['end'],
        next_refresh_at=(instant(receipt['end'])+timedelta(hours=REFRESH_HOURS)).isoformat(),
        method='妙手已匹配店铺六天付款订单样本；部分退款保持原付款金额',
        limitation='非随机店铺样本，仅作影响估算；零样本退款不代表全店无退款')
    with store.db() as db:db.execute('INSERT OR REPLACE INTO aov_monitor_refund_samples VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
    return value


def impact(metric,sample):
    """Display-only expected effect. Never synthesize fractional actual orders."""
    if sample.get('status')!='AVAILABLE':return None
    rate=Decimal(sample['order_rate']);amount_rate=Decimal(sample['amount_rate'])
    return dict(estimated=True,order_pct=sample['order_pct'],amount_pct=sample['amount_pct'],
        aov_impact_pct=sample['aov_impact_pct'],
        expected_refund_orders=rounded(Decimal(metric['current_count'])*rate),
        expected_refund_amount_rmb=rounded(Decimal(metric['current_total'])*amount_rate),
        sample_orders=sample['sample_orders'],sample_shops=sample['sample_shops'],observed_at=sample['observed_at'])
