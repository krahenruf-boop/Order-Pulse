"""Bounded working data: preserve summaries, pending work and live rolling receipts."""
import json,sqlite3
from datetime import datetime,timedelta,timezone
from . import incremental,refund_estimates
from .domain import instant

def cleanup(store,now=None,compact=False):
    now=now or datetime.now(timezone.utc);start=(now-timedelta(hours=144)).isoformat();end=now.isoformat()
    incremental.import_receipts(store)
    protected=set()
    for shop in store.config()['shops']:
        for kind in ('tms','miaoshou') if shop.get('source_mode')!='TMS_ONLY' else ('tms',):
            for _,_,receipt in incremental.segments(store,shop,kind,start,end):
                if receipt:protected.add(receipt)
    refund=refund_estimates.latest(store,store.config()['shops'],end)
    if refund:protected.add((refund['run_id'],refund['job_id']))
    with store.db() as d:
        active={r[0] for r in d.execute("SELECT id FROM aov_monitor_runs WHERE status IN ('RUNNING','WAITING','PAUSED','WAITING_BROWSER','WAITING_USER','WAITING_CONNECTION')")}
        latest=d.execute('SELECT run_id FROM aov_monitor_snapshots ORDER BY rowid DESC LIMIT 1').fetchone();detail=active|({latest[0]} if latest else set())
        protected_runs=active|({latest[0]} if latest else set())
        removed_orders=0
        for (rid,) in d.execute('SELECT DISTINCT run_id FROM aov_monitor_orders').fetchall():
            if rid not in detail:
                removed_orders+=d.execute('DELETE FROM aov_monitor_orders WHERE run_id=?',(rid,)).rowcount
                d.execute('DELETE FROM aov_monitor_cycle_coverage WHERE run_id=?',(rid,))
        removed_jobs=0
        for rid,jid in d.execute('SELECT run_id,id FROM aov_monitor_jobs WHERE result IS NOT NULL').fetchall():
            if rid in protected_runs or (rid,jid) in protected:continue
            d.execute('UPDATE aov_monitor_jobs SET result=NULL WHERE run_id=? AND id=?',(rid,jid));removed_jobs+=1
            d.execute('DELETE FROM aov_monitor_cache_receipts WHERE run_id=? AND job_id=?',(rid,jid))
        # Refund samples are tiny aggregates; only retain the latest 7 daily samples.
        if d.execute("SELECT 1 FROM sqlite_master WHERE name='aov_monitor_refund_samples'").fetchone():
            d.execute('DELETE FROM aov_monitor_refund_samples WHERE rowid NOT IN (SELECT rowid FROM aov_monitor_refund_samples ORDER BY rowid DESC LIMIT 7)')
    with sqlite3.connect(store.path,timeout=30) as d:
        pages=d.execute("PRAGMA page_count").fetchone()[0];free=d.execute("PRAGMA freelist_count").fetchone()[0]
        compact=compact or free>32768 and free>pages*.3
    if compact:
        with sqlite3.connect(store.path,timeout=30) as d:
            d.execute('PRAGMA wal_checkpoint(TRUNCATE)');d.execute('VACUUM')
    return dict(removed_order_details=removed_orders,cleared_source_batches=removed_jobs,retained_source_batches=len(protected))
