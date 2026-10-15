"""Transport-neutral UI observer. Default construction is intentionally BLOCKED.
No selectors/endpoints are invented; a separately verified, authorized deployment
adapter must supply correlated observations of normal user-visible operations.
"""
import asyncio
import time
import math
import re
from datetime import datetime,timezone,timedelta
from email.utils import parsedate_to_datetime
from ..models import KalodataError, validate_context, now_iso

SESSION_LABEL='Kalodata Plugin'
class BrowserProvider:
    def __init__(self, transport=None, min_interval=2.0, operation_timeout=30.0):
        self.transport=transport;self.min_interval=min_interval;self.operation_timeout=operation_timeout
        self._last=0.;self._lock=None;self._session_ref=None;self._op=0;self._bindings={};self._cancelled=set()
    async def capabilities(self):
        base=dict(transport_ready=False,session_label=SESSION_LABEL,session_ref=None,supported_operations=[],blocked_reason={'code':'PLUGIN_OFFLINE','message':KalodataError('PLUGIN_OFFLINE').message})
        if self.transport is None:return base
        try:cap=await asyncio.wait_for(self.transport.probe_session(),self.operation_timeout)
        except Exception:return base
        if isinstance(cap,dict) and cap.get('state') in {'PLUGIN_OFFLINE','PLUGIN_RELOAD_REQUIRED','PLUGIN_VERSION_MISMATCH','KALODATA_LOGIN_REQUIRED','DETAIL_QUOTA_EXHAUSTED','PERMISSION_DENIED','LOGIN_REQUIRED'}:
            error=KalodataError(cap['state'],403)
            return dict(base,blocked_reason={'code':error.code,'message':error.message})
        if not isinstance(cap,dict) or cap.get('deployable') is not True or cap.get('authorized') is not True:
            return base
        if cap.get('browser')!='plugin' or not isinstance(cap.get('session_ref'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',cap['session_ref']) or cap.get('operations')!=['bind_product','apply_period','verify_context','read_visible_page','resolve_observed_link']:
            return dict(base,blocked_reason={'code':'PLUGIN_VERSION_MISMATCH','message':KalodataError('PLUGIN_VERSION_MISMATCH').message})
        self._session_ref=cap['session_ref']
        return dict(base,transport_ready=True,session_ref=self._session_ref,supported_operations=cap['operations'],blocked_reason=None)
    async def _call(self, operation, payload):
        if self._lock is None:self._lock=asyncio.Lock()
        if not self.transport or not self._session_ref: raise KalodataError('PLUGIN_OFFLINE',503)
        binding=self._bindings.get((payload.get('run_id'),payload.get('generation')))
        if binding and binding!=self._session_ref:raise KalodataError('SESSION_MISMATCH',403)
        async with self._lock:
            self._op+=1;op_id=str(self._op)
            envelope=dict(payload,operation_id=op_id,session_ref=self._session_ref)
            for attempt in range(2):
                if (payload.get('run_id'),payload.get('generation')) in self._cancelled:raise KalodataError('CANCELLED',409)
                await asyncio.sleep(max(0,self.min_interval-(time.monotonic()-self._last)))
                if (payload.get('run_id'),payload.get('generation')) in self._cancelled:raise KalodataError('CANCELLED',409)
                self._last=time.monotonic()
                try:response=await asyncio.wait_for(self.transport.observe(operation,envelope),self.operation_timeout)
                except (asyncio.TimeoutError,ConnectionError):
                    if attempt==0:continue
                    raise KalodataError('NETWORK_ERROR',502,retryable=True)
                if (payload.get('run_id'),payload.get('generation')) in self._cancelled:raise KalodataError('CANCELLED',409)
                if not isinstance(response,dict):raise KalodataError('SCHEMA_CHANGED',502)
                if response.get('session_ref')!=self._session_ref:raise KalodataError('SESSION_MISMATCH',403)
                if response.get('operation_id')!=op_id or response.get('run_id')!=payload.get('run_id') or response.get('generation')!=payload.get('generation'):raise KalodataError('CONTEXT_CHANGED',409)
                status=response.get('http_status',200)
                if type(status)!=int:raise KalodataError('SCHEMA_CHANGED',502)
                if status==429:
                    raw=response.get('retry_after');seconds=60
                    try:
                        if isinstance(raw,(int,float)) or (isinstance(raw,str) and raw.isdigit()):seconds=max(1,math.ceil(float(raw)))
                        else:seconds=max(1,math.ceil((parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds()))
                    except (ValueError,TypeError,OverflowError):pass
                    try:until=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()
                    except OverflowError:until=datetime.max.replace(tzinfo=timezone.utc).isoformat()
                    raise KalodataError('RATE_LIMITED',429,{'cooldown_until':until,'cooldown_seconds':seconds})
                if status==401 or response.get('login_required') or response.get('challenge') or response.get('error_code') in ('LOGIN_REQUIRED','KALODATA_LOGIN_REQUIRED'):raise KalodataError('LOGIN_REQUIRED',403)
                if response.get('error_code')=='DETAIL_QUOTA_EXHAUSTED':raise KalodataError('DETAIL_QUOTA_EXHAUSTED',403)
                if status==403 or response.get('entitlement_denied'):raise KalodataError('PERMISSION_DENIED',403)
                if status>=400:raise KalodataError('NETWORK_ERROR',502)
                if response.get('success') is not True:
                    code=response.get('error_code')
                    allowed={'LINK_NOT_FOUND','LINK_ID_MISMATCH','SOURCE_LOAD_FAILED','SOURCE_LIST_NOT_READY','PAGINATION_SCHEMA_CHANGED','VIDEO_ITEM_SCHEMA_CHANGED','PRODUCT_NOT_FOUND','PERIOD_UNAVAILABLE','DATE_CONTROL_NOT_FOUND','DATE_CONTEXT_UNVERIFIED','SOURCE_REFRESH_TIMEOUT','CONTEXT_CHANGED','PAGE_MISMATCH','SCHEMA_CHANGED','RANGE_EXCEEDS_VISIBLE','PLUGIN_OFFLINE','PLUGIN_RELOAD_REQUIRED','PLUGIN_VERSION_MISMATCH','EXTRACTION_FAILED'}
                    diagnostic=(response.get('data') or {}).get('diagnostic',{})
                    safe_fields=('expected_range','observed_range','observed_start','observed_end')
                    details={k:v[:80] for k,v in diagnostic.items() if k in safe_fields and isinstance(v,str)} if isinstance(diagnostic,dict) else {}
                    raise KalodataError(code if code in allowed else 'UPSTREAM_BUSINESS_ERROR',502,details)
                data=response.get('data')
                if not isinstance(data,dict):raise KalodataError('SCHEMA_CHANGED',502)
                return data
    async def preflight(self,request):
        cap=await self.capabilities()
        if not cap['transport_ready']:
            code=(cap.get('blocked_reason') or {}).get('code')
            raise KalodataError(code if code in {'PLUGIN_RELOAD_REQUIRED','PLUGIN_VERSION_MISMATCH'} else 'PLUGIN_OFFLINE',503)
        if request['session_ref']!=self._session_ref:raise KalodataError('SESSION_MISMATCH',403)
        await self._call('bind_product',{'product_url':request['product_url'],'run_id':None,'generation':0})
        raw=await self._call('apply_period',{'period':request['period'],'run_id':None,'generation':0})
        return validate_context(raw,request)
    async def revalidate(self,request,context,run_id,generation):
        if request['session_ref']!=self._session_ref:raise KalodataError('SESSION_MISMATCH',403)
        self._bindings[(run_id,generation)]=self._session_ref
        raw=await self._call('verify_context',{'context_fingerprint':context['context_fingerprint'],'period':request['period'],'run_id':run_id,'generation':generation})
        actual=validate_context(raw,request)
        # Pagination is live metadata, not product/sort identity. validate_context has
        # already checked the requested page range against this final observation.
        if actual['context_fingerprint']!=context['context_fingerprint']:raise KalodataError('CONTEXT_CHANGED',409)
        return actual
    async def read_page(self,page,context,run_id,generation):
        return await self._call('read_visible_page',{'source_page':page,'context_fingerprint':context['context_fingerprint'],'run_id':run_id,'generation':generation})
    async def begin_run(self):
        if hasattr(self.transport,'begin_run'):await self.transport.begin_run()
    async def end_run(self):
        if hasattr(self.transport,'end_run'):await self.transport.end_run()
    async def resolve(self,row,context,run_id,generation):
        return await self._call('resolve_observed_link',{'video_id':row['video_id'],'observed_detail_url':row['kalodata_video_url'],'source_page':row['source_page'],'source_index':row['source_index'],'context_fingerprint':context['context_fingerprint'],'run_id':run_id,'generation':generation})
    async def cancel(self,run_id,generation):
        self._cancelled.add((run_id,generation));self._bindings.pop((run_id,generation),None)
        if self.transport:
            try:await asyncio.wait_for(self.transport.cancel(run_id,generation),self.operation_timeout)
            except Exception:pass
