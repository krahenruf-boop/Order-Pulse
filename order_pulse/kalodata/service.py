from __future__ import annotations
import asyncio
import base64
import json
import re
import time
import uuid
from datetime import datetime,timezone,timedelta
from .models import KalodataError,ACTIVE,TERMINAL,extract_request,validate_context,is_id,now_iso,iso_time
from .exports import export_items
from .connectors.link_resolver import detail_url,verified_link
from .connectors.page_context import validate_page

class Service:
    def __init__(self,repository,connector,enabled=False,task_budget=1800):
        self.repo=repository;self.connector=connector;self.enabled=enabled;self.task_budget=task_budget
        self.tasks={};self.preflights={};self._start_lock=None;self._preflight_lock=None
    def _locks(self):
        if self._start_lock is None:self._start_lock=asyncio.Lock()
        if self._preflight_lock is None:self._preflight_lock=asyncio.Lock()
    def cooldown(self):
        until=self.repo.state('cooldown_until')
        if not until:return None,0
        try:remaining=max(0,int((iso_time(until)-datetime.now(timezone.utc)).total_seconds()+.999))
        except KalodataError:return None,0
        return until,remaining
    async def capabilities(self):
        if self.enabled:cap=await self.connector.capabilities()
        else:cap=dict(transport_ready=False,session_label='Kalodata Plugin',session_ref=None,supported_operations=[],blocked_reason={'code':'MODULE_DISABLED','message':'素材模块默认关闭；已有结果仍可查看。'})
        until,seconds=self.cooldown()
        return dict(cap,enabled=self.enabled,cooldown_until=until,cooldown_seconds=seconds)
    async def available(self):
        if not self.enabled:raise KalodataError('MODULE_DISABLED',503)
        until,seconds=self.cooldown()
        if seconds:raise KalodataError('RATE_LIMITED',429,{'cooldown_until':until,'cooldown_seconds':seconds})
        cap=await self.connector.capabilities()
        if not cap.get('transport_ready'):
            code=(cap.get('blocked_reason') or {}).get('code')
            if code in {'DETAIL_QUOTA_EXHAUSTED','PERMISSION_DENIED','LOGIN_REQUIRED','KALODATA_LOGIN_REQUIRED'}:raise KalodataError('LOGIN_REQUIRED' if code=='KALODATA_LOGIN_REQUIRED' else code,403)
            raise KalodataError(code if code in {'PLUGIN_OFFLINE','PLUGIN_RELOAD_REQUIRED','PLUGIN_VERSION_MISMATCH'} else 'PLUGIN_OFFLINE',503)

    async def extract_product(self,payload):
        """Persist a job before doing any external work; HTTP/page lifetime is irrelevant."""
        self._locks()
        keys={'product_url','period','start_page','end_page','client_request_id'}
        if not isinstance(payload,dict) or set(payload)!=keys:raise KalodataError('INVALID_REQUEST',422)
        client_id=payload['client_request_id']
        if not isinstance(client_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}',client_id):raise KalodataError('INVALID_REQUEST',422)
        request=extract_request({k:v for k,v in payload.items() if k!='client_request_id'}|{'session_ref':self.connector.transport.session_ref})
        request['transport']='plugin'
        async with self._start_lock:
            existing=self.repo.by_client(client_id)
            if existing:return existing
            if self.tasks or self._preflight_lock.locked():raise KalodataError('KALODATA_BUSY',409)
            if not self.enabled:raise KalodataError('MODULE_DISABLED',503)
            ident=uuid.uuid4().hex
            run=self.repo.create(ident,client_id,request,{'visible_pages':None})
            task=asyncio.create_task(self._prepare_product(ident))
            self.tasks[ident]=task;task.add_done_callback(lambda _,i=ident:self.tasks.pop(i,None))
            return run

    async def _prepare_product(self,run_id):
        run=self.repo.get_run(run_id);gen=run['generation'];request=run['request'];started=time.monotonic()
        try:
            self.repo.stage(run_id,gen,'preflighting')
            await self.available()
            context=validate_context(await self.connector.preflight(request),request)
            self._assert_active(run_id,gen,started)
            context=await self.connector.revalidate(request,context,run_id,gen)
            self.repo.set_context(run_id,gen,context)
            await self.execute(run_id)
        except asyncio.CancelledError:
            self.repo.finish(run_id,'interrupted','INTERRUPTED')
        except KalodataError as error:
            self.note_error(error);self.repo.finish(run_id,'failed',error.code,error.details)
        except Exception:
            self.repo.finish(run_id,'failed','SOURCE_LOAD_FAILED')
    def note_error(self,error):
        if error.code=='RATE_LIMITED' and error.details.get('cooldown_until'):
            self.repo.set_state('cooldown_until',error.details['cooldown_until'])
    async def preflight(self,payload):
        self._locks()
        request=extract_request(payload);await self.available()
        if self._preflight_lock.locked() or self._start_lock.locked() or self.tasks:raise KalodataError('CONFLICT',409)
        async with self._preflight_lock:
            try:context=validate_context(await self.connector.preflight(request),request)
            except KalodataError as e:self.note_error(e);raise
            if not self.enabled:raise KalodataError('MODULE_DISABLED',503)
            ident=uuid.uuid4().hex;expiry=datetime.now(timezone.utc)+timedelta(minutes=5)
            self.preflights={k:v for k,v in self.preflights.items() if v['expires_at']>now_iso()}
            if len(self.preflights)>100:self.preflights.clear()
            result=dict(preflight_id=ident,request=request,query_context=context,expires_at=expiry.isoformat(),visible_pages=context['visible_pages'])
            self.preflights[ident]=result
            return result
    async def start(self,payload,parent=None):
        self._locks()
        if not isinstance(payload,dict) or set(payload)!={'preflight_id','client_request_id'} or not all(isinstance(payload[k],str) and re.fullmatch('[A-Za-z0-9_-]{1,128}',payload[k]) for k in payload):raise KalodataError('INVALID_REQUEST',422)
        await self.available()
        async with self._start_lock:
            existing=self.repo.by_client(payload['client_request_id'])
            if existing:
                if existing['parent_run_id']!=parent:raise KalodataError('CONFLICT',409)
                return existing
            if self.tasks or self._preflight_lock.locked():raise KalodataError('CONFLICT',409)
            if parent:
                prior=self.repo.get_run(parent)
                if prior['state'] not in TERMINAL:raise KalodataError('CONFLICT',409)
            p=self.preflights.get(payload['preflight_id'])
            if not p or iso_time(p['expires_at'])<=datetime.now(timezone.utc):raise KalodataError('PREFLIGHT_EXPIRED',409)
            ident=uuid.uuid4().hex
            try:
                current=validate_context(await self.connector.revalidate(p['request'],p['query_context'],ident,1),p['request'])
                if current['context_fingerprint']!=p['query_context']['context_fingerprint']:raise KalodataError('CONTEXT_CHANGED',409)
            except KalodataError as e:self.note_error(e);raise
            if not self.enabled:raise KalodataError('MODULE_DISABLED',503)
            run=self.repo.create(ident,payload['client_request_id'],p['request'],current,parent)
            self.preflights.pop(payload['preflight_id'],None)
            task=asyncio.create_task(self.execute(ident));self.tasks[ident]=task;task.add_done_callback(lambda _,i=ident:self.tasks.pop(i,None))
            return run
    async def cancel(self,run_id):
        run=self.repo.get_run(run_id)
        if run['state'] in ACTIVE:
            self.repo.finish(run_id,'cancelled','CANCELLED')
            # Invalidate generation before trying to cancel an in-flight browser operation.
            await self.connector.cancel(run_id,run['generation'])
        return self.repo.get_run(run_id)
    async def shutdown(self):
        self.enabled=False;self.preflights.clear()
        for run_id in list(self.tasks):
            try:
                run=self.repo.get_run(run_id)
                if run['state'] in ACTIVE:
                    self.repo.finish(run_id,'cancelled','CANCELLED')
                    try:await asyncio.wait_for(self.connector.cancel(run_id,run['generation']),2)
                    except Exception:pass
            except Exception:pass
        for task in self.tasks.values():task.cancel()
        if self.tasks:
            done,pending=await asyncio.wait(list(self.tasks.values()),timeout=2)
            for task in done:
                if not task.cancelled():task.exception()
        self.tasks.clear()
    def _assert_active(self,run_id,generation,start):
        if time.monotonic()-start>self.task_budget:raise KalodataError('TASK_BUDGET_EXCEEDED',408)
        run=self.repo.get_run(run_id)
        if not self.enabled or run['state'] not in ACTIVE or run['generation']!=generation:raise KalodataError('CANCELLED',409)
    async def execute(self,run_id):
        run=self.repo.get_run(run_id);gen=run['generation'];request=run['request'];context=run['query_context'];started=time.monotonic()
        try:
            if hasattr(self.connector,'begin_run'):await self.connector.begin_run()
            for page in range(request['start_page'],request['end_page']+1):
                self._assert_active(run_id,gen,started);self.repo.stage(run_id,gen,'reading')
                batch=await self.connector.read_page(page,context,run_id,gen)
                self._assert_active(run_id,gen,started);rows,fingerprint=validate_page(batch,page,context)
                self.repo.page(run_id,gen,page,context['page_size'],len(rows),fingerprint,batch['observed_at'])
                for index,raw in enumerate(rows,1):
                    self._assert_active(run_id,gen,started)
                    vid=raw.get('video_id');vid=vid if is_id(vid) else None;detail=None
                    if vid and raw.get('kalodata_video_url'):
                        try:detail=detail_url(raw['kalodata_video_url'],vid)
                        except KalodataError:pass
                    first=self.repo.row(run_id,gen,page,index,vid,detail)
                    if not vid:continue
                    if first and first['status']=='resolved':continue
                    evidence=raw.get('link_evidence')
                    try:
                        if evidence is None:
                            if not detail:raise KalodataError('LINK_NOT_FOUND')
                            self.repo.stage(run_id,gen,'resolving')
                            evidence=await self.connector.resolve(dict(video_id=vid,kalodata_video_url=detail,source_page=page,source_index=index),context,run_id,gen)
                            self._assert_active(run_id,gen,started)
                        url,evidence=verified_link(evidence,vid,page,index,context,detail)
                        self.repo.resolve(run_id,gen,page,index,url,evidence,detail)
                    except KalodataError as e:
                        self.repo.failure(run_id,gen,page,index,e.code)
                        if e.code in ('LINK_NOT_FOUND','LINK_ID_MISMATCH'):
                            pass
                        else:raise
                self.repo.verify_page(run_id,gen,page)
                if batch.get('terminal_page') is True and page<request['end_page']:
                    self.repo.finish(run_id,'partial','RANGE_EXCEEDS_VISIBLE');return
            final=self.repo.get_run(run_id)
            self.repo.finish(run_id,'completed_with_errors' if final['counts']['invalid_rows'] or final['counts']['unresolved_unique'] else 'completed')
            if request.get('transport')=='plugin':
                stamp=now_iso();self.repo.set_state('plugin_last_success_at',stamp)
                self.connector.transport.last_success_at=stamp
        except asyncio.CancelledError:
            try:self.repo.finish(run_id,'interrupted','INTERRUPTED')
            except Exception:pass
        except KalodataError as e:
            try:
                self.note_error(e)
                state='cancelled' if e.code=='CANCELLED' else 'blocked' if e.code in ('RATE_LIMITED','DETAIL_QUOTA_EXHAUSTED','PERMISSION_DENIED','LOGIN_REQUIRED','SESSION_MISMATCH','PLUGIN_OFFLINE','PLUGIN_VERSION_MISMATCH') else 'partial' if self.repo.get_run(run_id)['counts']['visited_rows'] else 'failed'
                self.repo.finish(run_id,state,e.code,e.details)
            except Exception:pass
        except Exception:
            try:self.repo.finish(run_id,'failed','STORAGE_ERROR')
            except Exception:pass
        finally:
            if hasattr(self.connector,'end_run'):await self.connector.end_run()
    def items(self,run_id,cursor=None,limit=100):
        if type(limit)!=int or not 1<=limit<=500:raise KalodataError('INVALID_REQUEST',422)
        run=self.repo.get_run(run_id);offset=0
        if cursor:
            try:
                decoded=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if set(decoded)!={'run_id','revision','offset'} or decoded['run_id']!=run_id or decoded['revision']!=run['result_revision'] or type(decoded['offset'])!=int or decoded['offset']<0:raise ValueError()
                offset=decoded['offset']
            except Exception:raise KalodataError('INVALID_CURSOR',409)
        result=run['items'][offset:offset+limit];next_cursor=None
        if offset+limit<len(run['items']):next_cursor=base64.urlsafe_b64encode(json.dumps({'run_id':run_id,'revision':run['result_revision'],'offset':offset+limit}).encode()).decode()
        return dict(items=result,next_cursor=next_cursor,result_revision=run['result_revision'])
    def links(self,run_id,scope='all'):
        run=self.repo.get_run(run_id)
        return dict(links=[i['tiktok_url'] for i in export_items(run,scope)],result_revision=run['result_revision'],scope=scope)
