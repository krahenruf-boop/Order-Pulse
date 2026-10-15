"""Verified source receipts form a rolling cache; snapshots stay immutable.

Coverage is per source and shop, including empty ranges. A missing receipt is
never inferred from an empty order list. Newer receipts replace overlapping
ranges; historical source rows (including cancellations) are retained verbatim.
"""
import json
from datetime import datetime, timedelta, timezone
from .domain import instant, digest
from .cycle import order_identity

MODE = 'ROLLING_INCREMENTAL'

def is_incremental(config):
    return config.get('collection_mode') == MODE

def setup(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS aov_monitor_cache_receipts(
        run_id TEXT NOT NULL,job_id TEXT NOT NULL,kind TEXT NOT NULL,
        shop_id TEXT NOT NULL,identity_hash TEXT NOT NULL,start TEXT NOT NULL,
        end TEXT NOT NULL,observed_at TEXT NOT NULL,
        PRIMARY KEY(run_id,job_id,shop_id));
      CREATE INDEX IF NOT EXISTS aov_cache_scope ON aov_monitor_cache_receipts(shop_id,kind,identity_hash,end);
      CREATE TABLE IF NOT EXISTS aov_monitor_cache_imports(run_id TEXT NOT NULL,job_id TEXT NOT NULL,PRIMARY KEY(run_id,job_id));
    ''')

def import_receipts(store):
    """Index previously validated raw jobs once, without rewriting their data."""
    with store.db() as db:
        rows=db.execute('''SELECT j.run_id,j.id,j.payload,r.payload,j.result
          FROM aov_monitor_jobs j JOIN aov_monitor_runs r ON r.id=j.run_id
          LEFT JOIN aov_monitor_cache_imports i ON i.run_id=j.run_id AND i.job_id=j.id
          WHERE j.status='COMPLETE' AND i.run_id IS NULL ORDER BY r.rowid,j.id''').fetchall()
        for rid,jid,jraw,craw,result in rows:
            j,c=json.loads(jraw),json.loads(craw)
            c=c.get('resume_context',c)
            # A card run always freezes the verified directory and shop config.
            if j.get('kind') not in ('tms','miaoshou') or not c.get('config') or not c.get('directory'):
                db.execute('INSERT OR IGNORE INTO aov_monitor_cache_imports VALUES(?,?)',(rid,jid));continue
            value=json.loads(result or '{}')
            if ('orders' if j['kind']=='tms' else 'rows') not in value:continue
            selected=set(j.get('shop_ids',c.get('selected',[])))
            for shop in c['config']['shops']:
                if shop['id'] not in selected or j['kind']=='miaoshou' and shop.get('source_mode')=='TMS_ONLY':continue
                db.execute('INSERT OR IGNORE INTO aov_monitor_cache_receipts VALUES(?,?,?,?,?,?,?,?)',
                  (rid,jid,j['kind'],shop['id'],order_identity(shop),instant(j['start']).isoformat(),instant(j['end']).isoformat(),j.get('observed_at') or j.get('finished_at') or c['cutoff']))
            db.execute('INSERT OR IGNORE INTO aov_monitor_cache_imports VALUES(?,?)',(rid,jid))

def segments(store,shop,kind,start,end):
    """Partition the requested interval into newest certified receipts and gaps."""
    a,b=instant(start),instant(end)
    with store.db() as db:
        candidates=db.execute('''SELECT run_id,job_id,start,end,observed_at FROM aov_monitor_cache_receipts
          WHERE shop_id=? AND kind=? AND identity_hash=? AND end>? AND start<?
          ORDER BY observed_at DESC,rowid DESC''',(shop['id'],kind,order_identity(shop),a.isoformat(),b.isoformat())).fetchall()
    candidates=[(r,j,instant(x),instant(y),at) for r,j,x,y,at in candidates]
    boundaries=sorted({a,b}|{max(a,x) for _,_,x,_,_ in candidates}|{min(b,y) for _,_,_,y,_ in candidates})
    result=[]
    for left,right in zip(boundaries,boundaries[1:]):
        receipt=next(((r,j) for r,j,x,y,_ in candidates if x<=left and y>=right),None)
        if result and result[-1][2]==receipt:result[-1]=(result[-1][0],right,receipt)
        else:result.append((left,right,receipt))
    return result

def plan(store,shops,start,end):
    from .card_service import tms_ranges,shop_batches
    from . import refund_estimates
    import_receipts(store)
    scopes={'tms':{},'miaoshou':{}};cached=0;missing=0
    for shop in shops:
        for kind in ('tms','miaoshou') if shop.get('source_mode')!='TMS_ONLY' else ('tms',):
            for a,b,receipt in segments(store,shop,kind,start,end):
                if receipt:cached+=(b-a).total_seconds();continue
                missing+=(b-a).total_seconds()
                for x,y in tms_ranges(a.isoformat(),b.isoformat()):scopes[kind].setdefault((x,y),[]).append(shop['id'])
    jobs=[]
    for kind,spans in scopes.items():
        for (a,b),ids in sorted(spans.items()):
            # Miaoshou reads all shops in a shared UI query; shop_ids limits the
            # certified cache coverage and prevents unrelated ranges being reused.
            for group in shop_batches(ids) if kind=='tms' else [sorted(ids)]:
                jobs.append(dict(id=('10-' if kind=='tms' else '20-')+kind+'-'+digest([a,b,group])[:12],kind=kind,start=a,end=b,shop_ids=group))
    dual=[s['id'] for s in shops if s.get('source_mode')!='TMS_ONLY']
    receipt=refund_estimates.latest(store,shops,end)
    if dual:
        job=dict(id='30-refunds-shared',kind='refunds',shop_ids=dual,shared=True,start=start,end=end)
        if receipt:job.update(reused=True,refund_receipt=receipt,observed_at=receipt['observed_at'],start=receipt['start'],end=receipt['end'])
        jobs.append(job)
    return jobs,dict(cached_source_hours=round(cached/3600,2),missing_source_hours=round(missing/3600,2),order_batches=sum(j['kind']!='refunds' for j in jobs),refund_batches=int(bool(dual) and not receipt),refund_sample_reused=bool(receipt),refund_policy=refund_estimates.POLICY,ranges=[dict(source=k,start=a,end=b,shops=len(ids)) for k,spans in scopes.items() for (a,b),ids in spans.items()])

def paid_at(row,kind):
    if kind=='tms':return instant(row['paid_at'])
    return datetime.strptime(row['paid_at'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)

def read(store,shop,kind,start,end,loaded=None):
    loaded={} if loaded is None else loaded;rows=[];gaps=[];used=set()
    for a,b,receipt in segments(store,shop,kind,start,end):
        if not receipt:gaps.append([a.isoformat(),b.isoformat()]);continue
        if receipt not in loaded:
            with store.db() as db:
                value=db.execute('SELECT result FROM aov_monitor_jobs WHERE run_id=? AND id=? AND status=?',(*receipt,'COMPLETE')).fetchone()
            loaded[receipt]=json.loads(value[0]) if value else {}
        value=loaded[receipt];field='orders' if kind=='tms' else 'rows'
        if field not in value:gaps.append([a.isoformat(),b.isoformat()]);continue
        source_id=shop['id'] if kind=='tms' else shop['miaoshou_shop_id']
        for row in value[field]:
            if str(row['shop_id'])!=source_id:continue
            try:inside=a<=paid_at(row,kind)<b
            except (ValueError,TypeError,KeyError):inside=True # fail closed in domain validation
            if inside:
                # Preserve conflicting duplicate versions inside one source job.
                key=json.dumps(row,sort_keys=True)
                if key not in used:used.add(key);rows.append(row)
    return rows,gaps
