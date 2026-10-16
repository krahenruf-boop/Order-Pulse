import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .card_store import CardStore as Store
from .domain import reconcile, calculate, transition, digest, monitor_scope, group_scope_hash, WINDOWS,LOOKBACK_HOURS,instant
from .fx import Rates
from .cycle import is_cycle,cycle_hours
from .incremental import is_incremental
from ..notifications import PulseFeishuClient

ZONE=ZoneInfo('Asia/Shanghai')

def slot_at(at,hours=2):
    local=at.astimezone(ZONE)
    return local.replace(hour=local.hour//hours*hours,minute=0,second=0,microsecond=0).astimezone(timezone.utc)


class Monitor:
    def __init__(self,service,source=None,rates=None,clock=None,client_factory=PulseFeishuClient):
        self.host=service;self.store=Store(service.repo.path);self.clock=clock or (lambda:datetime.now(timezone.utc))
        from .batch_sources import BatchSource
        self.source=source or BatchSource(service);self.rates=rates or Rates(self.store)
        self.client_factory=client_factory;self.worker=None;self.timer=None;self.stopping=False
    def catalog(self):
        value=self.host.repo.active_catalog()
        return value.get('families',{}) if value else {}
    def shops(self):return (self.host.repo.get('binding') or {}).get('stores',[])
    def state(self):
        c=self.store.prepare(self.shops()) if hasattr(self.source,'directory') else self.store.config();now=self.clock()
        if c.get('view_version')==2:
            from .cards import visible_cards
            directory=self.store.directory();mapping=directory.get('mapping',{})
            hours=cycle_hours(c) if is_cycle(c) or is_incremental(c) else 2
            return dict(config=c,active_windows=['cycle'] if is_cycle(c) else list(WINDOWS),lookback_hours=hours if is_cycle(c) else LOOKBACK_HOURS,busy=bool(self.worker and not self.worker.done() or self.store.pending()),latest=self.store.snapshot(),history=self.store.history(),source=self.source.status(),feishu_configured=self.host.secrets.configured(),shop_cards=visible_cards(c,self.store.snapshot(),mapping),directory=directory,sync_progress=self.store.progress(),next_at=(slot_at(now,hours)+timedelta(hours=hours)).isoformat() if c['enabled'] else None,stores=[dict(id=s['id'],name=s['name']) for s in c['shops']])
        verification=self.store.verification(c)
        return dict(config=c,busy=bool(self.worker and not self.worker.done()),latest=self.store.snapshot(),
                    history=self.store.history(),events=self.store.events(),source=self.source.status(),
                    automation=verification,
                    next_at=(slot_at(now)+timedelta(hours=2)).isoformat() if c['enabled'] and verification['ready'] else None,
                    feishu_configured=self.host.secrets.configured(),
                    catalog=[{'family_id':k,**v} for k,v in self.catalog().items()],
                    stores=[{'id':str(s['shop_id']),'name':s['name']} for s in self.shops() if s.get('platform_id')==104])
    def settings(self,value):
        if not isinstance(value,dict):raise ValueError('监控设置无效')
        if value.get('view_version')==2:
            if 'changes' in value:
                if set(value)-{'view_version','revision','enabled','changes','cycle_hours'} or not isinstance(value['changes'],list):raise ValueError('设置无效')
                current=self.store.config();shops={s['id']:s for s in current['shops']}
                for s in value['changes']:
                    if not isinstance(s,dict) or not isinstance(s.get('id'),str) or s.get('id') not in shops:raise ValueError('店铺不在授权目录')
                    shops[s['id']]=s
                value=dict(current,revision=value.get('revision'),enabled=value.get('enabled',current['enabled']),cycle_hours=value.get('cycle_hours',current.get('cycle_hours',2)),shops=list(shops.values()))
            from .card_store import collection_scope
            if self.worker and not self.worker.done() and collection_scope(value)!=collection_scope(self.store.config()):raise ValueError('同步中，请完成后修改规则或匹配；仍可隐藏店铺')
            return self.store.save_cards(value,self.catalog(),self.shops())
        if self.worker and not self.worker.done():raise ValueError('检测中，请完成后修改设置')
        return self.store.save_config(value,self.catalog(),self.shops())
    async def startup(self):
        self.store.recover();self.stopping=False
        pending=self.store.pending()
        if pending and pending[1].get('card_run'):
            from .card_service import run_cards
            c=self.store.prepare(self.shops());hours=cycle_hours(c) if is_cycle(c) or is_incremental(c) else 2
            if self.store.skip_unstarted(*pending,slot_at(self.clock(),hours)):
                if self.store.config()['enabled']:await self.start(True)
            elif pending[1].get('phase') not in {'WAITING_USER','AUTH_REQUIRED','WAITING_CONNECTION','BROWSER_BINDING_REQUIRED','TMS_PAGE_REQUIRED'}:
                context=self.store.narrow_run(*pending)
                if context.get('collection_mode')=='CYCLE' and context.get('directory') and instant(context['cutoff'])<slot_at(self.clock(),hours):context.update(finalize_only=True,halt_error='CYCLE_EXPIRED')
                self.worker=asyncio.create_task(run_cards(self,pending[0],context))
        self.timer=asyncio.create_task(self.scheduler())
    async def resume(self):
        """An explicit click resumes the saved task without creating a duplicate."""
        if self.worker and not self.worker.done():raise ValueError('检测正在运行')
        pending=self.store.pending()
        if not pending or not pending[1].get('card_run'):raise ValueError('没有等待恢复的检测任务')
        from .card_service import run_cards
        context=self.store.narrow_run(*pending)
        self.worker=asyncio.create_task(run_cards(self,pending[0],context))
        return dict(run_id=pending[0],status='RUNNING')
    async def shutdown(self):
        self.stopping=True
        if self.timer:self.timer.cancel();await asyncio.gather(self.timer,return_exceptions=True)
        # Browser calls have bounded deadlines; retain the lock until they stop.
        if self.worker:await asyncio.gather(self.worker,return_exceptions=True)
    async def start(self,automatic=False,shop_ids=None,retry_id=None):
        if self.worker and not self.worker.done() or self.store.pending():raise ValueError('已有客单价检测在运行或等待浏览器')
        c=self.store.prepare(self.shops()) if hasattr(self.source,'directory') else self.store.config()
        if c.get('view_version')==2:
            from .card_service import run_cards
            if automatic and not c['enabled']:return None
            hours=cycle_hours(c) if is_cycle(c) or is_incremental(c) else 2
            at=self.clock().replace(microsecond=0);slot=slot_at(at,hours).isoformat() if automatic else None
            if automatic and (is_cycle(c) or is_incremental(c)):at=slot_at(at,hours)
            if shop_ids and (automatic or not isinstance(shop_ids,list) or any(x not in {s['id'] for s in c['shops']} for x in shop_ids)):raise ValueError('店铺范围无效')
            if retry_id:
                previous=self.store.snapshot(retry_id)
                if is_incremental(c) and previous and previous.get('collection_mode')!='ROLLING_INCREMENTAL':raise ValueError('旧口径记录仅供查看，请开始滚动增量检测')
                if automatic or not previous or previous.get('status') not in ('INCOMPLETE','PARTIAL') :raise ValueError('该任务不可重试')
                if is_cycle(c) and previous.get('collection_mode')!='CYCLE':raise ValueError('旧范围记录仅供查看，请开始当前周期检测')
                if is_cycle(c) and instant(previous['cutoff'])<slot_at(self.clock(),hours):raise ValueError('该周期已结束，旧结果保留为记录；请检测当前周期')
                context=deepcopy(previous.get('resume_context') or dict(card_run=True,cutoff=previous['cutoff'],start=previous['start'],directory=self.store.directory(),config=c,selected=[j['shop_id'] for j in self.store.jobs(retry_id) if j['kind']=='refunds'],matched=sum(s['mapping_confirmed'] for s in c['shops']),shop_count=len(c['shops'])))
                from .card_store import collection_scope
                context['scope_hash']=digest(collection_scope(c));context['slot']=None
                context['retry_from']=retry_id
                context['directory_seconds']=0
                context.pop('halt_error',None)
                context.pop('finalize_only',None)
            else:context=dict(card_run=True,cutoff=at.isoformat(),start=(at-timedelta(hours=hours if is_cycle(c) else LOOKBACK_HOURS)).isoformat(),active_windows=['cycle'] if is_cycle(c) else list(WINDOWS),lookback_hours=hours if is_cycle(c) else LOOKBACK_HOURS,collection_mode=c.get('collection_mode','ROLLING'),cycle_hours=hours,slot=slot,shop_ids=shop_ids)
            rid=self.store.start((('incremental:' if is_incremental(c) else 'cycle:')+str(hours)+':'+slot) if slot and (is_cycle(c) or is_incremental(c)) else slot,context)
            if not rid:return None
            if retry_id:
                retry_jobs=self.store.jobs(retry_id)
                from .card_service import retry_tms_jobs
                retry_jobs=retry_tms_jobs(retry_jobs,context['selected'])
                for job in retry_jobs:
                    copied={k:v for k,v in job.items() if k not in ('status','result','started_at','finished_at','elapsed_seconds')}
                    if job['status']=='COMPLETE':copied.update(reused=True,observed_at=job.get('observed_at') or job.get('finished_at') or context['cutoff'])
                    self.store.add_jobs(rid,[copied])
                    if job['status'] in ('COMPLETE','SPLIT','ARCHIVED'):self.store.job_done(rid,job['id'],job.get('result'),job['status'])
            context=self.store.narrow_run(rid,context)
            self.worker=asyncio.create_task(run_cards(self,rid,context));return dict(run_id=rid,status='RUNNING')
        if not any(s['monitor'] for s in c['shops']):raise ValueError('请先配置需要监控的店铺和产品')
        at=self.clock().replace(microsecond=0);slot=slot_at(at).isoformat() if automatic else None
        if automatic and not c['enabled']:return None
        if automatic:
            ready=set(self.store.verification(c)['ready'])
            if not ready:return None
            c=deepcopy(c)
            for shop in c['shops']:
                shop['products']=[p for p in shop['products'] if shop['id']+':'+p['family_id'] in ready]
                shop['monitor']=shop['monitor'] and bool(shop['products'])
        rid=self.store.start(slot,dict(trigger='automatic' if automatic else 'manual',revision=c['revision']))
        if not rid:return None
        self.worker=asyncio.create_task(self.run(rid,c,at,slot));return {'run_id':rid,'status':'RUNNING'}
    def scheduler_delay(self):
        # Fast retries belong only to an unfinished task waiting for TMS.
        # Idle / user-login waits sleep until the next configured collection slot.
        pending=self.store.pending()
        if self.worker and not self.worker.done() or pending and pending[1].get('phase') in {'TMS_PAGE_REQUIRED','WAITING_CONNECTION','BROWSER_BINDING_REQUIRED'}:
            return 20
        c=self.store.config()
        if not c['enabled']:return 2*60*60
        hours=cycle_hours(c) if is_cycle(c) or is_incremental(c) else 2
        now=self.clock()
        return max(1,(slot_at(now,hours)+timedelta(hours=hours)-now).total_seconds())
    async def scheduler(self):
        while not self.stopping:
            pending=self.store.pending()
            if pending and pending[1].get('card_run') and not (self.worker and not self.worker.done()):
                c=self.store.config();hours=cycle_hours(c) if is_cycle(c) or is_incremental(c) else 2
                if self.store.skip_unstarted(*pending,slot_at(self.clock(),hours)):
                    if c['enabled']:await self.start(True)
                    await asyncio.sleep(self.scheduler_delay());continue
                if pending[1].get('collection_mode')=='CYCLE' and pending[1].get('directory') and instant(pending[1]['cutoff'])<slot_at(self.clock(),hours):
                    from .card_service import run_cards
                    pending[1].update(finalize_only=True,halt_error='CYCLE_EXPIRED')
                    self.worker=asyncio.create_task(run_cards(self,*pending))
                    await asyncio.sleep(self.scheduler_delay());continue
                from .card_service import browser_batch
                # This is an unfinished authorized task, so it may reclaim its
                # uniquely verified Space after restart. Idle scheduling never does.
                readiness=await browser_batch(self,self.source.readiness) if hasattr(self.source,'readiness') else {'ready':await browser_batch(self,self.source.available),'error':'WAITING_USER'}
                if readiness['ready']:
                    c=self.store.config();hours=cycle_hours(c) if is_cycle(c) or is_incremental(c) else 2
                    if self.store.skip_unstarted(*pending,slot_at(self.clock(),hours)):
                        if self.store.config()['enabled']:await self.start(True)
                        await asyncio.sleep(self.scheduler_delay());continue
                    pending[1].pop('waiting_since',None);pending[1].pop('wait_error',None)
                    from .card_service import run_cards
                    self.worker=asyncio.create_task(run_cards(self,*pending))
                else:self.store.pause(*pending,readiness['error'])
                await asyncio.sleep(self.scheduler_delay());continue
            if self.store.config()['enabled'] and (not hasattr(self.source,'directory') or self.store.directory().get('status')=='COMPLETE') and not (self.worker and not self.worker.done()):
                try:
                    # A completed full manual refresh can be newer than the
                    # current scheduled slot. Wait for the next slot rather
                    # than moving the dashboard backwards after restart.
                    if not self.current_slot_covered():await self.start(True)
                except Exception:pass  # Failed runs remain visible; next slot may retry.
            await asyncio.sleep(self.scheduler_delay())
    def current_slot_covered(self):
        c=self.store.config();latest=self.store.snapshot() or {};context=latest.get('resume_context') or {}
        if not is_incremental(c) or latest.get('status')!='SUCCEEDED' or not is_incremental(latest) or context.get('shop_ids'):return False
        from .card_store import collection_scope
        return context.get('scope_hash')==digest(collection_scope(c)) and instant(latest['cutoff'])>=slot_at(self.clock(),cycle_hours(c))
    async def run(self,rid,c,at,slot):
        from ..browser import blocking
        cutoff=at.isoformat();start=(at-timedelta(hours=LOOKBACK_HOURS)).isoformat();states=self.store.alert_states();events=[];groups=[]
        try:
            catalog=self.catalog()
            for shop in c['shops']:
                for p in shop['products']:
                    if p['family_id'] not in catalog or sorted(catalog[p['family_id']]['member_skus'])!=p['skus']:raise ValueError('CATALOG_CHANGED')
            if self.host.lock.locked():raise ValueError('BROWSER_BUSY')
            async with self.host.lock:
                runner=getattr(self.source,'run_browser_task',None)
                sources=await blocking(runner,self.source.collect,c,start,cutoff) if runner else await blocking(self.source.collect,c,start,cutoff)
            saved_orders={}
            currencies={s['currency'] for s in c['shops'] if s['monitor'] and all(sources.get(s['id'],{}).get(k,{}).get('coverage')=='COMPLETE' for k in ('tms','miaoshou'))}
            fx={k:await blocking(self.rates.get,k,at) for k in sorted(currencies)}
            previous=self.store.snapshot() or {};oldgroups={g['key']:g for g in previous.get('groups',[])}
            for shop in c['shops']:
                if not shop['monitor']:continue
                orders,notes,errors=reconcile(sources.get(shop['id'],{}),shop,{sku for family in catalog.values() for sku in family['member_skus']})
                saved_orders[shop['id']]=orders
                if not getattr(self.source,'real_verified',False):errors=errors+['SOURCE_CONTRACT_PENDING']
                for product in shop['products']:
                    key=shop['id']+':'+product['family_id'];old=oldgroups.get(key)
                    rate=fx.get(shop['currency'],dict(fx_rate=None,fx_status='UNAVAILABLE',fx_fetched_at=None))
                    revision=group_scope_hash(shop,product);windows=calculate(orders,cutoff,product['skus'],product['rules'],rate) if not errors else {}
                    display_name=product.get('display_name') or product['name']
                    g=dict(key=key,shop_id=shop['id'],shop_name=shop['name'],family_id=product['family_id'],product_name=display_name,
                           currency=shop['currency'],fx=rate,windows=windows,diagnostics=notes,errors=errors,verification_hash=group_scope_hash(shop,product),
                           status='INCOMPLETE' if errors else 'COMPLETE',last_complete=deepcopy(old.get('last_complete') or old) if errors and old else None)
                    if g.get('last_complete'):g['last_complete'].pop('last_complete',None)
                    changes=[]
                    for w in WINDOWS:
                        sk=key+':'+w;m=windows.get(w,{'status':'INCOMPLETE'})
                        state,event=transition(states.get(sk),m,slot,bool(slot),revision)
                        if slot:states[sk]=state
                        if event and shop['push']:changes.append(dict(window=w,kind=event,metric=m))
                    if changes:
                        events.append(dict(id=digest([rid,key])[:32],run_id=rid,group=key,shop_name=shop['name'],product_name=display_name,currency=shop['currency'],at=cutoff,changes=changes))
                    groups.append(g)
            status='SUCCEEDED' if groups and all(g['status']=='COMPLETE' for g in groups) else 'PARTIAL' if any(g['status']=='COMPLETE' for g in groups) else 'INCOMPLETE'
            payload=dict(run_id=rid,status=status,cutoff=cutoff,start=start,groups=groups,scope_hash=digest(monitor_scope(c['shops'])),
                         real_sources_verified=bool(getattr(self.source,'real_verified',False)),trigger='automatic' if slot else 'manual')
            if self.store.config()['revision']!=c['revision']:raise ValueError('INPUT_CHANGED')
            self.store.finish(rid,payload,states if slot else None,events,saved_orders)
            for event in events:await self.send(event)
        except Exception as exc:
            for key,state in states.items():state.update(candidate=None,streak=0)
            self.store.finish(rid,dict(run_id=rid,status='FAILED',cutoff=cutoff,groups=groups or [dict(g,status='INCOMPLETE',last_complete=g.get('last_complete') or {k:v for k,v in g.items() if k!='last_complete'},errors=['DETECTION_FAILED']) for g in (self.store.snapshot() or {}).get('groups',[])],error='本次检测未完成，已保留上次完整结果。',error_code=str(exc) if str(exc).isupper() else 'DETECTION_FAILED'),states if slot else None)
    async def send(self,event):
        config=self.host.secrets.get()
        if not config.get('webhook'):
            self.store.event_status(event['id'],'failed','飞书未配置');return
        self.store.event_status(event['id'],'sending')
        try:
            text=render_message(event)
            response=await self.client_factory(config['webhook'],config.get('secret',''),config.get('keyword',''),max_retries=0).send(text,event['id'],still_valid=lambda:not self.stopping)
            self.store.event_status(event['id'],response.get('status','uncertain'),response.get('reason'))
        except Exception:self.store.event_status(event['id'],'uncertain','发送结果待核对，不自动重发')


def render_message(event):
    lines=['客单价监控',event['shop_name']+' · '+event['product_name']]
    estimate=event.get('refund_estimation') or {}
    if estimate.get('estimated'):lines.append(f"退款抽样估算口径：已知全额退款已剔除，其余按付款订单监控；样本全退 {estimate['order_pct']}%，客单价影响估计 {estimate['aov_impact_pct']}%。非逐笔退款净额。")
    for e in event['changes']:
        m=e['metric'];r=m['rule'];label='恢复正常' if e['kind']=='RECOVERY' else '连续两次越界'
        lines.extend([f"{e['window']} · {label}",f"订单：{m['current_count']} / 前期 {m['previous_count']}",
          f"客单价：¥{m['aov_rmb']} / 前期 ¥{m['previous_aov_rmb']}",
          f"参考 {event['currency']}：{m['aov_reference'] or '—'} / 前期 {m['previous_aov_reference'] or '—'}",
          f"涨跌：{m['change_pct'] if m['change_pct'] is not None else '—'}%",
          f"金额区间：{r['lower'] or '不限'} ～ {r['upper'] or '不限'}；下跌 {r['drop_pct'] or '未设置'}% / 上涨 {r['rise_pct'] or '未设置'}%",
          f"本期：{m['start']} → {m['end']}",f"前期：{m['previous_start']} → {m['start']}"])
    lines.append('检测时间：'+event['at']);return '\n'.join(lines)
