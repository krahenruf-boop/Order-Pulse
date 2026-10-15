from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from .store import now_iso, parse_time, SOURCE
from .templates import render_template, format_amount
from ..feishu import FeishuClient, split_messages

STATUS_LABELS={'valid':'有效','stale':'过期','missing':'本次未获取到','failed':'本次采集失败，显示上次成功值','sample':'参考文件样本，非实时','unknown':'未确认'}


def local_time(stamp,tz):
    return parse_time(stamp).astimezone(ZoneInfo(tz)).strftime('%Y-%m-%d %H:%M:%S %Z') if stamp else '未提供'


def account_values(a,settings,**extra):
    v=dict(a)
    v['display_name']=a.get('display_name') or a.get('source_name') or a['asset_id']
    currency=a.get('currency') or settings['currency']
    for key in ('cash_balance','grant_balance','threshold'):
        v[key]=format_amount(a.get(key),currency,settings['amount_format'])
    metric=a.get('alert_metric','cash_balance')
    v['alert_metric']='现金余额' if metric=='cash_balance' else '赠款余额'
    v['current_balance']=format_amount(a.get(metric),currency,settings['amount_format'])
    v['currency']='R$' if settings['amount_format']=='local' and currency=='BRL' else currency
    v['balance_status']={'low':'低余额','normal':'正常','unknown':'尚未判断'}.get(a.get('balance_status'),'尚未判断')
    v['data_status']=STATUS_LABELS.get(a.get('data_status'),'未确认')
    for key in ('collected_at','source_updated_at'):
        v[key]=local_time(a.get(key),settings['timezone'])
    v.update(extra)
    return v


def due_slots(settings,after,now):
    """Return local civil-time identities, so a repeated DST hour executes once."""
    if not settings['setup_complete']:return []
    tz=ZoneInfo(settings['timezone']); start=after.astimezone(tz).date(); end=now.astimezone(tz).date()
    result=[]
    # Compact very long downtime; one delayed summary suffices, latest day is representative.
    if (end-start).days>366:start=end-timedelta(days=366)
    day=start
    while day<=end:
        for schedule in settings['schedules']:
            if not schedule['enabled']:continue
            h,m=map(int,schedule['time'].split(':'))
            instant=datetime(day.year,day.month,day.day,h,m,tzinfo=tz)
            # Skip a nonexistent spring-forward local time, rather than inventing an instant.
            if instant.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)!=instant.replace(tzinfo=None):continue
            if after<instant<=now:
                result.append(dict(key=f'{day.isoformat()}|{schedule["time"]}|{settings["timezone"]}',at=instant.astimezone(timezone.utc).isoformat()))
        day+=timedelta(days=1)
    return sorted(result,key=lambda s:s['at'])


def next_slot(settings,now=None):
    now=now or datetime.now(timezone.utc)
    if settings['paused'] or not settings['setup_complete']:return None
    slots=due_slots(settings,now,now+timedelta(days=3))
    return slots[0]['at'] if slots else None


class Engine:
    def __init__(self,store,bridge,secrets):
        self.store=store;self.bridge=bridge;self.secrets=secrets
        self.cycle=None;self.intents=None;self.stopped=False
        self.dispatch_lock=None;self.scheduler_task=None

    def status(self):
        collection=self.store.get('collection',{})
        settings=self.store.settings()
        accounts=self.store.accounts()
        source=collection.get('source_updated_at')
        valid=[a for a in accounts if a['data_status']=='valid']
        problems=[a['data_status'] for a in accounts if a['enabled'] and a['data_status']!='valid']
        data_status=('failed' if collection.get('status')=='failed' else
                     (problems[0] if problems else ('valid' if valid else (accounts[0]['data_status'] if accounts else 'unknown'))))
        return dict(busy=bool((self.cycle and not self.cycle.done()) or getattr(self.bridge,'job',None)),paused=settings['paused'],
                    last_success=self.store.get('last_success'),last_error=self.store.get('cleanup_error') or self.store.get('last_error'),
                    next_send=next_slot(settings),source_updated_at=source,refresh_mode=collection.get('refresh_mode'),
                    data_status=data_status,
                    export_cooldown_seconds=self.bridge.cooldown_remaining() if hasattr(self.bridge,'cooldown_remaining') else 0,
                    last_collection=collection,parse_count=collection.get('parse_count',0))

    def request(self,trigger,summary=False,alerts=False,slots=None):
        if hasattr(self.bridge,'cooldown_remaining') and self.bridge.cooldown_remaining():
            if trigger in ('query','push'):self.bridge.check_export_cooldown()
            return False
        if self.cycle and not self.cycle.done():
            if trigger in ('query','push'):return False
            self.intents['alerts'] |= alerts
            self.intents['summary'] |= summary
            merged={s['key']:s for s in self.intents['slots']}
            merged.update({s['key']:s for s in slots or []})
            self.intents['slots']=list(merged.values())
            self.intents['triggers'].add(trigger)
            return True
        if getattr(self.bridge,'job',None):return False
        self.intents=dict(summary=summary,alerts=alerts,slots=list(slots or []),triggers={trigger})
        self.cycle=asyncio.create_task(self._run_cycle(self.intents))
        return True

    async def _run_cycle(self,intents):
        task_id=uuid.uuid4().hex;started=now_iso();started_clock=time.monotonic()
        self.store.log('collection','开始采集',task_id=task_id,triggers=sorted(intents['triggers']),started_at=started)
        result=None;collection_succeeded=False
        try:
            result,meta,evidence=await self.bridge.collect()
            missing=self.store.ingest(result,meta)
            missing=[a['asset_id'] for a in self.store.accounts() if a['enabled'] and a['data_status']=='missing']
            previous=self.store.get('collection',{})
            self.store.set('collection',dict(meta,status='success',parse_count=len(result['accounts']),task_id=task_id,started_at=started,ended_at=now_iso()))
            self.store.set('last_success',meta['collected_at']);self.store.set('last_error',None)
            self.store.log('collection','余额采集成功',task_id=task_id,parse_count=len(result['accounts']),
                           source_updated_at=meta['source_updated_at'],refresh_mode=meta['refresh_mode'],
                           page_count=evidence['page_count'],data_source=evidence.get('data_source','xlsx'),request_count=evidence.get('request_count'),warnings=result['warnings'],
                           previous_count=previous.get('parse_count'),missing_count=len(missing),ended_at=now_iso())
            automatic=intents['alerts'] or intents['summary']
            if automatic:
                self.incident('collection',None)
                self.incident('missing',f'{len(missing)} 个已启用账户本次未获取到，保留上次值；请核对关联和导出范围' if missing else None)
            if intents['alerts'] and not self.store.settings()['paused']:self.evaluate_alerts()
            if intents['summary'] and ('push' in intents['triggers'] or not self.store.settings()['paused']):
                self.queue_summary(intents['slots'],delayed=any((datetime.now(timezone.utc)-parse_time(s['at'])).total_seconds()>90 for s in intents['slots']),manual='push' in intents['triggers'])
            collection_succeeded=True
        except asyncio.CancelledError:
            self.store.set('last_error','采集已取消；本轮结果未覆盖上次成功数据')
            previous=self.store.get('collection',{})
            self.store.set('collection',dict(previous,status='failed',error='采集已取消',task_id=task_id,started_at=started,ended_at=now_iso()))
            self.store.log('collection','采集已取消，未覆盖余额',level='warning',task_id=task_id,ended_at=now_iso())
        except Exception as exc:
            message=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else '采集内部异常，请检查运行记录并重新连接 ego lite'
            previous=self.store.get('collection',{})
            explicit_code=getattr(exc,'code',None)
            matched=re.search(r'\b(SOURCE_TIMEOUT|SOURCE_UNAVAILABLE|HTTP_ERROR|AUTH_REQUIRED|PAGINATION_FAILED|DATA_INVALID)\b',message)
            code=(explicit_code if isinstance(explicit_code,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{1,63}',explicit_code)
                  else matched.group(1) if matched else 'HTTP_ERROR' if re.search(r'\b520\b',message) else 'COLLECTION_FAILED')
            diagnostics=dict(error_code=code,error_stage='balance_collection',safe_error_message=message,
                retry=previous.get('status')=='failed',elapsed_ms=round((time.monotonic()-started_clock)*1000),
                started_at=started,ended_at=now_iso())
            self.store.set('collection',dict(previous,**dict(status='failed',error=message,task_id=task_id,**diagnostics)))
            self.store.set('last_error',message)
            self.store.log('collection',message,level='error',task_id=task_id,status='FAILED',**diagnostics)
            if (intents['alerts'] or intents['summary']) and ('push' in intents['triggers'] or not self.store.settings()['paused']):self.incident('collection',message)
        finally:
            if intents['alerts']:self.store.set('last_detection',now_iso())
        # Raw XLSX has already been released before any network message is sent.
        # A failed collection must neither notify nor flush older queued balances.
        if collection_succeeded and (intents['alerts'] or intents['summary']):await self.dispatch()

    def evaluate_alerts(self):
        current={(a['source'],a['asset_id']):a for a in self.store.accounts()}
        for r in self.store.raw_accounts():
            a=current[(r['source'],r['asset_id'])];c=r['config'];alert=r['alert']
            if not c['enabled'] or not c['alert_enabled'] or c['threshold'] is None or a['data_status']!='valid':continue
            value=a.get(c['alert_metric'])
            if value is None:continue
            low=Decimal(value)<Decimal(c['threshold']);state='low' if low else 'normal'
            old=alert.get('state','unknown')
            reason='预警设置变更' if alert.get('settings_changed') else ('首次启用预警' if alert.get('needs_initial') or old=='unknown' else '成功采集发现余额状态变化')
            notify=(low and (old!='low' or alert.get('needs_initial'))) or (not low and old=='low' and c['recovery_enabled'])
            alert.update(state=state,settings_changed=False,needs_initial=False,last_evaluated_at=now_iso())
            if notify:alert['generation']=alert.get('generation',0)+1
            # State transition and durable pending event commit atomically.
            with self.store.transaction() as db:
                self.store.set_alert(r['source'],r['asset_id'],alert,db)
                if notify:
                    kind='alert' if low else 'recovery'
                    self.store.event(kind,dict(asset_id=a['asset_id'],source=a['source'],generation=alert['generation'],
                        revision=c['revision'],reason=reason,expires_at=(datetime.now(timezone.utc)+timedelta(minutes=self.store.settings()['max_age_minutes'])).isoformat()),db=db)
            if notify:self.store.log('alert',reason,asset_id=a['asset_id'],state=state)

    def incident(self,key,error):
        incident=self.store.get('incident:'+key)
        if error and not incident:
            generation=uuid.uuid4().hex
            self.store.set('incident_generation:'+key,generation)
            self.store.set('incident:'+key,dict(error=error,at=now_iso(),generation=generation))
        elif not error and incident:
            self.store.set('incident:'+key,None)

    def queue_summary(self,slots=None,delayed=False,manual=False):
        slots=slots or [];keys={s['key'] for s in slots}
        with self.store.lock:
            done={r[0] for r in self.store.db.execute('SELECT key FROM balance_schedules')}
        fresh=keys-done
        if keys and not fresh:return
        rows=[a for a in self.store.accounts() if a['enabled'] and a['include_summary'] and a['data_status']=='valid']
        recipients=[dict(source=a['source'],asset_id=a['asset_id'],revision=a['revision']) for a in rows]
        with self.store.transaction() as db:
            event_id=self.store.event('summary',dict(recipients=recipients,delayed=delayed,slot_count=len(fresh),manual=manual,
                settings_revision=self.store.get('settings_revision',0),source_updated_at=self.store.get('collection',{}).get('source_updated_at'),
                expires_at=(datetime.now(timezone.utc)+timedelta(minutes=self.store.settings()['max_age_minutes'])).isoformat()),db=db)
            for s in slots:
                if s['key'] in fresh:db.execute('INSERT OR IGNORE INTO balance_schedules(key,at,status,event_id) VALUES(?,?,?,?)',(s['key'],s['at'],'queued',event_id))
        self.store.log('schedule','延迟汇总已合并' if delayed else '汇总已加入发送队列',event_id=event_id,account_count=len(recipients),slots=len(fresh))

    def valid_event(self,event):
        p=event['payload'];settings=self.store.settings();kind=event['kind']
        if p.get('expires_at') and parse_time(p['expires_at'])<datetime.now(timezone.utc):return False
        if p.get('settings_revision',0)!=self.store.get('settings_revision',0):return False
        if kind in ('alert','recovery'):
            if settings['paused']:return False
            r=next((r for r in self.store.raw_accounts() if r['asset_id']==p['asset_id'] and r['source']==p['source']),None)
            a=next((a for a in self.store.accounts() if a['asset_id']==p['asset_id'] and a['source']==p['source']),None)
            if not r or not a or a['data_status']!='valid':return False
            current_value=a.get(a['alert_metric'])
            if current_value is None or a['threshold'] is None:return False
            actual_low=Decimal(current_value)<Decimal(a['threshold'])
            return bool(a['enabled'] and a['alert_enabled'] and a['revision']==p['revision'] and actual_low==(kind=='alert')
                        and r['alert'].get('generation')==p['generation'] and r['alert']['state']==('low' if kind=='alert' else 'normal'))
        if kind=='summary':
            if settings['paused'] and not p.get('manual'):return False
            if p.get('settings_revision')!=self.store.get('settings_revision',0):return False
            current={(a['source'],a['asset_id']):a for a in self.store.accounts()}
            return all((a:=current.get((r['source'],r['asset_id']))) and a['enabled'] and a['include_summary'] and a['revision']==r['revision'] and a['data_status']=='valid' for r in p['recipients'])
        return kind=='test'

    def messages(self,event,settings=None):
        if event['kind'] not in ('summary','alert','recovery','test'):return []
        settings=settings or self.store.settings();p=event['payload'];kind=event['kind'];eid=event['id'];fields=settings['fields'];tpl=settings['templates']
        rows=self.store.accounts()
        if kind=='summary':
            ids={(r['source'],r['asset_id']) for r in p['recipients']}
            rows=[a for a in rows if (a['source'],a['asset_id']) in ids]
            vals=account_values(rows[0],settings,event_id=eid) if rows else {}
            title=render_template(tpl['title'],vals,fields)
            collection=self.store.get('collection',{})
            header=f'{title}\n采集时间：{local_time(collection.get("collected_at"),settings["timezone"])}\n来源更新时间：{local_time(collection.get("source_updated_at"),settings["timezone"])}\n数据状态：有效；' + ('缓存有效' if collection.get('refresh_mode')=='cache' else '本轮更新')
            if p.get('delayed'):header+=f'\n延迟补发：合并 {p.get("slot_count",1)} 个错过的时间点'
            excluded=[a for a in self.store.accounts() if a['enabled'] and a['include_summary'] and a['data_status']!='valid']
            if excluded:header+=f'\n{len(excluded)} 个账户本次数据异常，未纳入余额汇总'
            blocks=[render_template(tpl['account'],account_values(a,settings),fields) for a in rows]
            return split_messages(blocks,header,eid,settings['keyword'])
        if kind in ('alert','recovery'):
            a=next(a for a in rows if a['asset_id']==p['asset_id'] and a['source']==p['source'])
            body=render_template(tpl[kind],account_values(a,settings,reason=p['reason'],event_id=eid),fields)
        elif kind=='test':body='Order Pulse 余额飞书连接测试\n这是一条测试消息，不包含账户余额。'
        return split_messages([body],'',eid,settings['keyword'])

    async def dispatch(self):
        if self.dispatch_lock is None:self.dispatch_lock=asyncio.Lock()
        if self.dispatch_lock.locked():return
        async with self.dispatch_lock:
            for event in self.store.pending_events():
                if event['status']!='pending':continue
                if not self.valid_event(event):
                    self.store.update_event(event['id'],'cancelled','通知已失效，取消旧通知');continue
                credentials=self.secrets.get()
                if not credentials.get('webhook'):
                    self.store.update_event(event['id'],'failed','未配置飞书 Webhook');self.store.set('feishu_failure','未配置飞书 Webhook');continue
                try:
                    messages=self.messages(event)
                    if not messages:
                        self.store.update_event(event['id'],'cancelled','没有可发送的已启用账户');continue
                    client=FeishuClient(credentials['webhook'],credentials.get('secret',''),self.store.settings()['keyword'],self.store.settings()['max_retries'])
                    credential_revision=self.store.get('credentials_revision',0)
                    attempts=0;p=event['payload'];p['parts']=[]
                    self.store.update_event(event['id'],'sending','发送中',payload=p)
                    outcome=None
                    for index,message in enumerate(messages):
                        outcome=await client.send(message,event['id'],still_valid=lambda:self.valid_event(event) and self.store.get('credentials_revision',0)==credential_revision)
                        attempts+=outcome['attempts'];p['parts'].append(dict(index=index+1,**outcome))
                        self.store.update_event(event['id'],'sending',f'已处理 {index+1}/{len(messages)} 条',attempts,payload=p)
                        if outcome['status']!='success':break
                    succeeded=sum(part['status']=='success' for part in p['parts'])
                    if outcome['status']!='success' and succeeded:
                        outcome['reason']=f'已成功发送 {succeeded}/{len(messages)} 条；'+outcome['reason']
                    self.store.update_event(event['id'],outcome['status'],outcome['reason'],attempts,payload=p)
                    if outcome['status']=='success':
                        self.store.set('feishu_success',now_iso());self.store.set('feishu_failure',None)
                    elif outcome['status']!='cancelled':self.store.set('feishu_failure',outcome['reason'])
                    self.store.log('notification',outcome['reason'],level='info' if outcome['status']=='success' else 'error',event_id=event['id'],status=outcome['status'],attempts=attempts,parts=len(messages))
                except Exception as exc:
                    reason=str(exc) if isinstance(exc,ValueError) else '消息生成或发送发生内部异常'
                    self.store.update_event(event['id'],'failed',reason);self.store.set('feishu_failure',reason)
                    self.store.log('notification',reason,level='error',event_id=event['id'])

    async def scheduler(self):
        previous=self.store.get('heartbeat')
        if previous and (datetime.now(timezone.utc)-parse_time(previous)).total_seconds()>60:
            self.store.log('interruption','后台曾停止、休眠或中断，恢复后将先采集再合并补发',level='warning',previous_heartbeat=previous)
        while not self.stopped:
            try:
                now=datetime.now(timezone.utc);settings=self.store.settings()
                self.store.set('heartbeat',now.isoformat())
                if settings['setup_complete'] and not settings['paused']:
                    cursor=parse_time(self.store.get('schedule_cursor')) or now
                    slots=due_slots(settings,cursor,now)
                    with self.store.lock:done={r[0] for r in self.store.db.execute('SELECT key FROM balance_schedules')}
                    slots=[s for s in slots if s['key'] not in done]
                    last=parse_time(self.store.get('last_detection'))
                    due=not last or (now-last).total_seconds()>=settings['interval_minutes']*60
                    # On failures retry on the detection interval, without consuming schedule identities.
                    if due:self.request('scheduled' if slots else 'periodic',summary=bool(slots),alerts=True,slots=slots)
                    elif slots and not (self.cycle and not self.cycle.done()):
                        attempted=parse_time(self.store.get('slot_attempt'))
                        if not attempted or (now-attempted).total_seconds()>=60:
                            self.store.set('slot_attempt',now.isoformat());self.request('scheduled',summary=True,alerts=True,slots=slots)
                    elif slots and self.cycle and not self.cycle.done():self.request('scheduled',summary=True,alerts=True,slots=slots)
                    if not slots:self.store.set('schedule_cursor',now.isoformat())
                self.store.purge()
                await asyncio.sleep(2)
            except asyncio.CancelledError:break
            except Exception:
                self.store.log('scheduler','调度内部错误，将在下一轮重试',level='error')
                await asyncio.sleep(5)

    async def tick(self, now=None):
        """Run one balance scheduling decision from the shared Order Pulse scheduler."""
        now=now or datetime.now(timezone.utc);settings=self.store.settings()
        self.store.set('heartbeat',now.isoformat())
        if settings['setup_complete'] and not settings['paused']:
            cursor=parse_time(self.store.get('schedule_cursor')) or now
            slots=due_slots(settings,cursor,now)
            with self.store.lock:done={r[0] for r in self.store.db.execute('SELECT key FROM balance_schedules')}
            slots=[s for s in slots if s['key'] not in done]
            last=parse_time(self.store.get('last_detection'))
            due=not last or (now-last).total_seconds()>=settings['interval_minutes']*60
            if due:self.request('scheduled' if slots else 'periodic',summary=bool(slots),alerts=True,slots=slots)
            elif slots and not (self.cycle and not self.cycle.done()):
                attempted=parse_time(self.store.get('slot_attempt'))
                if not attempted or (now-attempted).total_seconds()>=60:
                    self.store.set('slot_attempt',now.isoformat());self.request('scheduled',summary=True,alerts=True,slots=slots)
            elif slots and self.cycle and not self.cycle.done():self.request('scheduled',summary=True,alerts=True,slots=slots)
            if not slots:self.store.set('schedule_cursor',now.isoformat())
        self.store.purge()

    async def shutdown(self):
        self.stopped=True
        if self.scheduler_task:self.scheduler_task.cancel()
        if self.cycle and not self.cycle.done():
            self.cycle.cancel()
            await asyncio.gather(self.cycle,return_exceptions=True)
