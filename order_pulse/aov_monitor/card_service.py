"""Resumable all-shop collection, yielding the shared browser between batches."""
import asyncio
import time
from copy import deepcopy
from datetime import timedelta
from .cards import discover_products,shop_windows,alert_fingerprint
from .card_store import collection_scope
from .domain import instant,reconcile,calculate,transition,digest,WINDOWS,LOOKBACK_HOURS
from .sources import normalize_miaoshou
from .cycle import order_identity
from . import incremental,refund_estimates

SPLIT_ERRORS={'MIAOSHOU_SLICE_REQUIRED','MIAOSHOU_LIMIT_REACHED','MIAOSHOU_COVERAGE_CHANGED','FAST_TIMEOUT','FAST_PAGINATION_INCOMPLETE'}
TMS_BATCH_SHOPS=20
TMS_STOP_ERRORS={'TMS_BROWSER_NOT_READY','FAST_TRANSPORT_FAILED','WAITING_CONNECTION','AUTH_REQUIRED','PERMISSION_DENIED','BROWSER_BUSY','BROWSER_BINDING_REQUIRED','ACCOUNT_SCOPE_CHANGED','CATALOG_CHANGED','REQUEST_SCOPE_MISMATCH'}
PAUSE_ERRORS={'WAITING_USER','AUTH_REQUIRED','WAITING_CONNECTION','BROWSER_BINDING_REQUIRED','TMS_PAGE_REQUIRED'}

def batch_error(exc):
    if isinstance(exc,dict):code=exc.get('error') or 'SOURCE_FAILED';details=exc.get('diagnostic')
    else:code=getattr(exc,'code',None) or str(exc);details=getattr(exc,'details',None)
    # A failed browser binding cannot be repaired by querying a smaller date
    # range. Keep a distinct actionable cause instead of multiplying requests.
    if code in ('FAST_TRANSPORT_FAILED','FAST_TIMEOUT') and isinstance(details,dict) and details.get('stage') in ('binding','cleanup'):
        code='TMS_BROWSER_NOT_READY'
    return code,details

def halt_message(code):
    if code=='CYCLE_EXPIRED':return '上一检测周期已结束；未完成部分仅保存为记录，不补读旧订单。'
    if code in ('AUTH_REQUIRED','PERMISSION_DENIED'):return '马帮登录或授权已失效；请恢复登录后重试，已完成数据保留。'
    if code in ('ACCOUNT_SCOPE_CHANGED','CATALOG_CHANGED','REQUEST_SCOPE_MISMATCH'):return '马帮账号或查询范围校验未通过，本轮停止，已完成数据保留。'
    if code in ('FAST_HTTP_FAILED','FAST_TRANSPORT_FAILED'):return '马帮订单读取中断，本轮停止，已完成数据保留；可重试未完成批次。'
    return '马帮浏览器连接暂不可用，本轮停止，已完成数据保留；恢复后可重试未完成批次。'

def split_job(job):
    a,b=instant(job['start']),instant(job['end'])
    if b-a<timedelta(minutes=30) or job.get('depth',0)>=8:return []
    m=a+(b-a)/2
    # Reuse inclusive-source boundaries; canonical identities remove overlap.
    return [dict(job,id=job['id']+suffix,start=x.isoformat(),end=y.isoformat(),depth=job.get('depth',0)+1) for suffix,x,y in [('a',a,m),('b',m,b)]]

def split_tms_job(job,unstable=False):
    """Reduce pagination first, isolating a busy shop from the other shops."""
    if unstable and job.get('unstable_depth',0)>=2:return []
    ids=job.get('shop_ids')
    if ids and len(ids)>1:
        middle=len(ids)//2
        children=[dict(job,id=job['id']+suffix,shop_ids=part) for suffix,part in [('a',ids[:middle]),('b',ids[middle:])]]
    else:children=split_job(job)
    if unstable:
        for child in children:child['unstable_depth']=job.get('unstable_depth',0)+1
    return children

def shop_batches(ids):
    ids=sorted(ids)
    return [ids[i:i+TMS_BATCH_SHOPS] for i in range(0,len(ids),TMS_BATCH_SHOPS)]

def tms_ranges(start,end):
    """Bound the first all-shop query instead of waiting for a 14-day timeout."""
    a,b=instant(start),instant(end)
    while a<b:
        stop=min(a+timedelta(days=2),b)
        yield a.isoformat(),stop.isoformat()
        a=stop

def retry_tms_jobs(jobs,selected=None):
    """Coalesce only unread slices; completed receipts are reused unchanged."""
    keep=[j for j in jobs if j['kind']!='tms' or j['status'] in ('COMPLETE','ARCHIVED')]
    scopes={}
    for j in jobs:
        if j['kind']!='tms' or j['status'] in ('COMPLETE','SPLIT','ARCHIVED'):continue
        ids=j.get('shop_ids',selected)
        key=tuple(sorted(ids)) if ids is not None else None
        scopes.setdefault(key,[]).append((instant(j['start']),instant(j['end'])))
    used={j['id'] for j in keep}
    for ids,spans in scopes.items():
        merged=[]
        for a,b in sorted(spans):
            if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
            else:merged.append((a,b))
        for a,b in merged:
            for start,end in tms_ranges(a.isoformat(),b.isoformat()):
                for group in shop_batches(ids) if ids is not None else [None]:
                    jid='10-tms-retry-'+digest([start,end,group])[:12]
                    if jid in used:raise ValueError('RETRY_QUEUE_CONFLICT')
                    used.add(jid)
                    job=dict(id=jid,kind='tms',start=start,end=end,status='QUEUED')
                    if group is not None:job['shop_ids']=group
                    keep.append(job)
    return keep

def tms_coverage(jobs,sid,start,end):
    """A successful receipt must cover this shop's entire frozen time range."""
    spans=[];errors=[]
    for j in jobs:
        if j['kind']!='tms' or j['status'] in ('SPLIT','ARCHIVED') or sid not in j.get('shop_ids',[sid]):continue
        if j['status']=='COMPLETE':spans.append((instant(j['start']),instant(j['end'])))
        else:errors.append((j.get('result') or {}).get('error') or 'SOURCE_NOT_READ')
    cursor=instant(start);stop=instant(end)
    for a,b in sorted(spans):
        if a>cursor:break
        cursor=max(cursor,b)
    if cursor<stop:return sorted(set(errors or ['TMS_COVERAGE_UNVERIFIED']))
    return []

async def browser_batch(monitor,fn,*args):
    from ..browser import blocking
    while monitor.host.lock.locked():
        if monitor.stopping:raise asyncio.CancelledError
        await asyncio.sleep(.5)
    async with monitor.host.lock:return await blocking(fn,*args)

async def run_cards(monitor,rid,context):
    store=monitor.store;states=store.alert_states();at=instant(context['cutoff']);slot=context.get('slot');events=[]
    cycle=context.get('collection_mode')=='CYCLE';hours=context.get('cycle_hours',2)
    inc=incremental.is_incremental(context)
    windows={'cycle':hours} if cycle else WINDOWS
    context.pop('waiting_since',None);context.pop('wait_error',None)
    try:
        if 'directory' not in context:
            started=time.monotonic()
            directory=await browser_batch(monitor,monitor.source.directory)
            context['directory_seconds']=round(time.monotonic()-started,2)
            c=store.apply_directory(directory)
            context.update(directory=store.directory(),config=c,matched=sum(s['mapping_confirmed'] for s in c['shops']),tms_only=sum(s.get('source_mode')=='TMS_ONLY' for s in c['shops']),shop_count=len(c['shops']),phase='ORDERS')
            selected=[s for s in c['shops'] if (s['mapping_confirmed'] or s.get('source_mode')=='TMS_ONLY') and (not context.get('shop_ids') or s['id'] in context['shop_ids'])]
            context['selected']=[s['id'] for s in selected]
            context['scope_hash']=digest(collection_scope(c));store.context(rid,context)
            dual=[s for s in selected if s.get('source_mode')!='TMS_ONLY']
            ranges=list(tms_ranges(context['start'],context['cutoff']))
            jobs=[dict(id=f'10-tms-{i:02}-{g:02}',kind='tms',start=a,end=b,shop_ids=ids) for i,(a,b) in enumerate(ranges) for g,ids in enumerate(shop_batches([s['id'] for s in selected]))]
            if dual:jobs.extend(dict(id=f'20-miaoshou-{i:02}',kind='miaoshou',start=a,end=b) for i,(a,b) in enumerate(tms_ranges(context['start'],context['cutoff'])))
            jobs.extend(dict(id='30-refunds-'+s['id'],kind='refunds',shop_id=s['id'],start=context['start'],end=context['cutoff']) for s in dual)
            if inc:
                jobs,context['cache']=incremental.plan(store,selected,context['start'],context['cutoff'])
                store.context(rid,context)
            store.add_jobs(rid,jobs)
        c=context['config'];selected=[s for s in c['shops'] if s['id'] in context['selected']]
        monitor.source._directory=context['directory'];monitor.source.real_verified=monitor.source.status()['verified']
        # Old versions may have split a binding failure into many queued jobs.
        # End that broken round once; retain every job receipt and prior result.
        broken=any(j['kind']=='tms' and j['status']=='FAILED' and batch_error(j.get('result') or {})[0]=='TMS_BROWSER_NOT_READY' for j in store.jobs(rid))
        while True:
            if context.get('finalize_only'):break
            if broken:context['halt_error']='TMS_BROWSER_NOT_READY';break
            pending=next((j for j in store.jobs(rid) if j['status']=='QUEUED'),None)
            if not pending:break
            if monitor.stopping:return
            if pending.get('refund_receipt'):
                # A daily sample reuse is local, does not acquire the browser,
                # and preserves its original observation time through retries.
                store.job_done(rid,pending['id'],refund_estimates.result(store,pending['refund_receipt']))
                continue
            context.update(current=pending['id'],phase='REFUNDS' if pending['kind']=='refunds' else 'ORDERS');store.context(rid,context)
            try:
                # Revalidate the space binding, ownership and actual TMS tab each
                # time, without holding the shared browser while waiting.
                batch_shops=[s for s in selected if s['id'] in pending.get('shop_ids',context['selected'])] if pending['kind']=='tms' else [s for s in selected if s.get('source_mode')!='TMS_ONLY' and s['id'] in pending.get('shop_ids',context['selected'])]
                def capture():
                    store.job_started(rid,pending['id'])
                    return monitor.source.batch(pending,batch_shops,context['directory']['target'])
                value=await browser_batch(monitor,capture)
                store.job_done(rid,pending['id'],value)
            except Exception as exc:
                code,details=batch_error(exc)
                if code in PAUSE_ERRORS:
                    store.pause(rid,context,code);return
                if pending['kind']=='tms' and code=='FAST_HTTP_FAILED' and pending.get('http_retries',0)<2:
                    pending={k:v for k,v in pending.items() if k not in ('status','result')};pending.update(id=pending['id']+'r',http_retries=pending.get('http_retries',0)+1)
                    store.add_jobs(rid,[pending]);store.job_done(rid,context['current'],{'retry':True,'error':code,'diagnostic':details},'SPLIT');await asyncio.sleep(pending['http_retries']*2);continue
                children=(split_tms_job(pending,code=='FAST_SOURCE_UNSTABLE') if pending['kind']=='tms' else split_job(pending)) if pending['kind']!='refunds' and (code in SPLIT_ERRORS or pending['kind']=='tms' and code=='FAST_SOURCE_UNSTABLE') else []
                if code=='MIAOSHOU_COVERAGE_CHANGED' and pending.get('depth',0)>=2:children=[]
                if children and sum(j['status']!='ARCHIVED' for j in store.jobs(rid))>=200:children=[]
                if children:store.add_jobs(rid,children);store.job_done(rid,pending['id'],{'split':True},'SPLIT')
                else:store.job_done(rid,pending['id'],{'error':code if code.isupper() else 'SOURCE_FAILED','diagnostic':details},'FAILED')
                if pending['kind']=='tms' and code in TMS_STOP_ERRORS:
                    context['halt_error']=code;break
            await asyncio.sleep(.2)  # give other module queues the released lock
        if monitor.stopping:return
        context.update(current=None,phase='CALCULATING');store.context(rid,context)
        calculation_started=time.monotonic()
        jobs=store.jobs(rid);tms=[];raw=[];refunds={};source_errors={'tms':[],'miaoshou':[]}
        for j in jobs:
            if j['status'] in ('SPLIT','ARCHIVED'):continue
            if j['kind']=='refunds':
                for sid in j.get('shop_ids',[j.get('shop_id')]):refunds[sid]=j['result']
            elif j['status']!='COMPLETE':source_errors[j['kind']].append(context.get('halt_error') or (j.get('result') or {}).get('error') or 'SOURCE_NOT_READ')
            elif j['kind']=='tms':tms.extend(j['result']['orders'])
            elif j['kind']=='miaoshou':raw.extend(j['result']['rows'])
        sample={};refund_job=None;refund_facts_only=False
        if inc:
            incremental.import_receipts(store)
            refund_job=next((j for j in jobs if j['kind']=='refunds' and j['status']=='COMPLETE'),None)
            receipt=(refund_job.get('refund_receipt') or dict(run_id=rid,job_id=refund_job['id'],start=refund_job['start'],end=refund_job['end'],observed_at=refund_job.get('observed_at') or refund_job.get('finished_at') or context['cutoff'])) if refund_job else None
            sample=refund_estimates.describe(store,receipt,selected)
            if not refund_job:
                # Failure/expiry invalidates the estimate, not previously
                # confirmed refund facts. Never resurrect a known full refund.
                known=refund_estimates.latest(store,selected,context['cutoff'],max_age_hours=None)
                if known:
                    facts=refund_estimates.result(store,known)
                    for shop in selected:
                        if shop.get('source_mode')!='TMS_ONLY':refunds[shop['id']]=facts
                    refund_facts_only=True
        cached_jobs={}
        catalog=monitor.catalog();previous=store.snapshot() or {};before={s['id']:s for s in previous.get('shop_cards',[])}
        groups=[];cards=[];saved={};fx={};active_config=store.config()
        from .service import slot_at
        alert_auto=bool(slot) and active_config['enabled'] and instant(slot)>=slot_at(monitor.clock(),hours if cycle or inc else 2)
        changed={s['id'] for s in c['shops'] if collection_scope({'shops':[s]})!=collection_scope({'shops':[next((x for x in active_config['shops'] if x['id']==s['id']),s)]})}
        for shop in c['shops']:
            sid=shop['id'];old=before.get(sid);single=shop.get('source_mode')=='TMS_ONLY'
            errors=tms_coverage(jobs,sid,context['start'],context['cutoff'])+([] if single else source_errors['miaoshou'])
            if sid not in context['selected']:
                if old:cards.append(deepcopy(old))
                else:cards.append(dict(id=sid,status='NOT_COLLECTED' if shop['mapping_confirmed'] or single else 'UNMATCHED',products=[],windows={},updated_at=None))
                continue
            if inc:
                local_tms,tms_gaps=incremental.read(store,shop,'tms',context['start'],context['cutoff'],cached_jobs)
                local_ms,ms_gaps=([],[]) if single else incremental.read(store,shop,'miaoshou',context['start'],context['cutoff'],cached_jobs)
                errors=(['TMS_CACHE_GAP'] if tms_gaps else [])+(['MIAOSHOU_CACHE_GAP'] if ms_gaps else [])
            else:local_tms=[r for r in tms if r['shop_id']==sid];local_ms=raw;tms_gaps=[];ms_gaps=[]
            if sid in changed:errors.append('INPUT_CHANGED')
            if cycle and (active_config.get('collection_mode')!='CYCLE' or active_config.get('cycle_hours',2)!=hours):errors.append('INPUT_CHANGED')
            value=refunds.get(sid,{}) or {}
            if not inc and not single and 'refunds' not in value:errors.append(value.get('error','MIAOSHOU_REFUND_INCOMPLETE'))
            ms=[];currencies=set()
            for r in ([] if single else local_ms):
                if r['shop_id']!=shop['miaoshou_shop_id']:continue
                if r.get('currency'):currencies.add(r['currency'])
                try:
                    known=[x for x in value.get('refunds',[]) if x['shop_id']==shop['miaoshou_shop_id']]
                    normalized=normalize_miaoshou(r,known if 'refunds' in value else None)
                    # A cached absence is not a fresh NONE. Confirmed full and
                    # partial refunds remain effective until the next check.
                    if inc and (refund_facts_only or refund_job and refund_job.get('refund_receipt')) and normalized['refund_status']=='NONE':normalized['refund_status']='UNKNOWN'
                    ms.append(normalized)
                except Exception as exc:errors.append(str(exc) if str(exc).isupper() else 'ORDER_FIELDS_UNVERIFIED')
            pair={source:dict(coverage='COMPLETE',orders=rows) for source,rows in [('tms',local_tms),('miaoshou',ms)]}
            if single:pair.pop('miaoshou')
            if errors:
                tms_missing=tms_gaps if inc else tms_coverage(jobs,sid,context['start'],context['cutoff'])
                ms_missing=(ms_gaps if inc else source_errors['miaoshou']) or 'refunds' not in value
                orders=[];notes=dict(coverage={k:('INCOMPLETE' if (tms_missing if k=='tms' else ms_missing) else 'COMPLETE') for k in pair},tms_orders=len(pair['tms']['orders']),miaoshou_orders=len(ms));invalid=[]
            else:orders,notes,invalid=reconcile(pair,shop,tms_only=single,estimate_refunds=inc)
            if inc:notes['cache']=dict(tms_gaps=tms_gaps,miaoshou_gaps=ms_gaps,mode='增量订单＋本地滚动窗口',refund_scope_hours=LOOKBACK_HOURS)
            errors.extend(invalid)
            if not monitor.source.real_verified:errors.append('SOURCE_CONTRACT_PENDING')
            if errors:
                # Empty/failed capture never replaces valid counts with zero.
                card=deepcopy(old or dict(id=sid,products=[],windows={},updated_at=None));card.update(status='INCOMPLETE',errors=sorted(set(errors)),attempted_at=context['cutoff'],diagnostics=notes)
                cards.append(card)
                if slot:
                    for key,state in list(states.items()):
                        if key.startswith(sid+':'):states[key]=transition(state,{'status':'INCOMPLETE'},slot,True,state.get('revision'))[0]
                continue
            currency=shop['currency'] if shop['currency']!='AUTO' else next(iter(currencies)) if len(currencies)==1 else None
            rate=dict(fx_rate=None,fx_status='UNAVAILABLE',fx_fetched_at=None)
            if currency:
                if currency not in fx:
                    from ..browser import blocking
                    fx[currency]=await blocking(monitor.rates.get,currency,at)
                rate=fx[currency]
            if cycle:orders=[o for o in orders if instant(context['start'])<=instant(o['paid_at'])<at]
            products=discover_products(orders,catalog,shop);saved[sid]=orders
            estimated=inc and bool(notes.get('refund_unverified'))
            current_eligible=not notes.get('refund_unverified') or estimated and sample.get('status')=='AVAILABLE';identity=order_identity(shop)
            if inc and refund_facts_only:current_eligible=False
            if inc:notes['refund_estimation']=dict(policy=refund_estimates.POLICY,estimated=estimated,status=sample.get('status','UNAVAILABLE'),sample_orders=sample.get('sample_orders',0),observed_at=sample.get('observed_at'),refresh_hours=refund_estimates.REFRESH_HOURS)
            comparison=store.previous_cycle(sid,(instant(context['start'])-timedelta(hours=hours)).isoformat(),context['start'],identity) if cycle else {'complete':True,'orders':[],'alert_eligible':True,'runs':[]}
            metrics_orders=orders+comparison['orders'];calc_options={'windows':windows,'previous_complete':comparison['complete']}
            eligible=current_eligible and comparison['alert_eligible']
            notes.update(previous_complete=comparison['complete'],previous_runs=comparison['runs'])
            card=dict(id=sid,status='COMPLETE',source_mode='TMS_ONLY' if single else 'DUAL',current_alert_eligible=current_eligible,alert_eligible=eligible,order_identity=identity,errors=[],currency=currency,fx=rate,updated_at=context['cutoff'],run_id=rid,windows=shop_windows(metrics_orders,context['cutoff'],shop,rate,**calc_options),products=[],diagnostics=notes)
            if inc:
                for metric in card['windows'].values():metric['refund_estimate']=refund_estimates.impact(metric,sample)
            if not eligible and slot:
                for key,state in list(states.items()):
                    if key.startswith(sid+':'):states[key]=transition(state,{'status':'INCOMPLETE'},slot,True,state.get('revision'))[0]
            card['diagnostics']['unmapped_skus']=sorted(sku for p in products if p['unknown'] for sku in p['skus'])
            for p in products:
                metrics=calculate(metrics_orders,context['cutoff'],p['skus'],p['rules'],rate,**calc_options);key=sid+':'+p['family_id']
                if inc:
                    for metric in metrics.values():metric['detection_interval_hours']=hours
                if cycle:p['period_hours']=hours
                revision=alert_fingerprint(shop,p)
                if inc:revision=digest([revision,refund_estimates.POLICY])
                g=dict(key=key,shop_id=sid,shop_name=shop.get('display_name') or shop['name'],family_id=p['family_id'],product_name=p['display_name'] or p['name'],status='COMPLETE',alert_eligible=eligible,currency=currency or '—',fx=rate,windows=metrics,diagnostics=notes,errors=[],verification_hash=revision,observed_skus=p['observed_skus'])
                groups.append(g);card['products'].append(dict(p,windows=metrics))
                changes=[]
                pushing=next((s['push'] for s in active_config['shops'] if s['id']==sid),False)
                for w in windows:
                    state,event=transition(states.get(key+':'+w),metrics[w],slot,alert_auto and pushing and eligible,revision)
                    if alert_auto and pushing and eligible:states[key+':'+w]=state
                    if event:changes.append(dict(window=w,kind=event,metric=metrics[w]))
                pushing=next((s['push'] for s in active_config['shops'] if s['id']==sid),False)
                if changes and pushing and alert_auto and eligible:events.append(dict(id=digest([rid,key])[:32],run_id=rid,group=key,shop_name=g['shop_name'],product_name=g['product_name'],currency=g['currency'],at=context['cutoff'],changes=changes,refund_estimation=dict(estimated=estimated,order_pct=sample.get('order_pct'),aov_impact_pct=sample.get('aov_impact_pct'),sample_orders=sample.get('sample_orders'))))
            card['mapping']=context['directory'].get('mapping',{}).get(sid,dict(status='MATCHED',method='SAVED',miaoshou_shop_id=shop['miaoshou_shop_id'],miaoshou_shop_name=shop['miaoshou_shop_name']))
            cards.append(card)
        complete=sum(x['status']=='COMPLETE' for x in cards if x['id'] in context['selected']);matched=len(context['selected'])
        status='SUCCEEDED' if complete==matched and matched else 'PARTIAL' if complete else 'INCOMPLETE'
        payload=dict(run_id=rid,status=status,cutoff=context['cutoff'],start=context['start'],active_windows=list(windows),lookback_hours=hours if cycle else LOOKBACK_HOURS,collection_mode=context.get('collection_mode','ROLLING'),cycle_hours=hours,groups=groups,shop_cards=cards,view_version=2,trigger='automatic' if slot else 'manual',real_sources_verified=monitor.source.real_verified,resume_context=context,progress=dict(phase='DONE',start=context['start'],cutoff=context['cutoff'],active_windows=list(windows),lookback_hours=hours if cycle else LOOKBACK_HOURS,total=sum(j['status'] not in ('SPLIT','ARCHIVED') for j in jobs),completed=sum(j['status']=='COMPLETE' for j in jobs),failed=sum(j['status']=='FAILED' for j in jobs),shops=len(c['shops']),matched=context['matched'],tms_only=context.get('tms_only',0),captured=complete,selected=matched,unmatched=len(c['shops'])-context['matched']-context.get('tms_only',0)))
        payload['calculation_seconds']=round(time.monotonic()-calculation_started,2)
        if inc:payload['cache']=context.get('cache',{})
        if inc:payload['refund_estimation']=dict(sample,policy=refund_estimates.POLICY,reused=bool(refund_job and refund_job.get('refund_receipt')),refresh_hours=refund_estimates.REFRESH_HOURS)
        if context.get('halt_error'):payload.update(error_code=context['halt_error'],error=halt_message(context['halt_error']))
        store.finish(rid,payload,states if slot else None,events,saved)
        for e in events:await monitor.send(e)
    except asyncio.CancelledError:raise
    except Exception as exc:
        # Leave historical snapshots untouched; the failed run reports the error.
        previous=store.snapshot() or {}
        if str(exc) in PAUSE_ERRORS:
            store.pause(rid,context,str(exc));return
        code=str(exc) if str(exc).isupper() else 'DETECTION_FAILED'
        cards=[dict(s,status='INCOMPLETE',errors=[code],attempted_at=context['cutoff']) for s in previous.get('shop_cards',[])]
        if slot:
            for key,state in states.items():states[key]=transition(state,{'status':'INCOMPLETE'},slot,True,state.get('revision'))[0]
        store.finish(rid,dict(run_id=rid,status='FAILED',cutoff=context['cutoff'],groups=[],shop_cards=cards,error='本次同步未完成，已保留上次完整结果。',error_code=code,diagnostic={'stage':context.get('phase','DIRECTORY'),'kind':type(exc).__name__}),states if slot else None)
