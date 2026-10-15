"""Batch transport over the existing normal-UI collectors, with bounded slices."""
import json
from pathlib import Path
import subprocess
import re
from decimal import Decimal
from .sources import DualSource, normalize_tms, normalize_miaoshou
from ..tms_orders import EgoOrderTransport, _validate_partition
from ..tms_core.source_time import request_window

class BatchSource(DualSource):
    def _node(self,program,marker,timeout):
        try:p=subprocess.run([str(Path.home()/'.local/bin/ego-browser'),'nodejs'],input=program,text=True,capture_output=True,timeout=timeout)
        except subprocess.TimeoutExpired:
            exc=ValueError('SOURCE_TIMEOUT');exc.details={'stage':marker.strip(':').lower(),'timeout_seconds':timeout};raise exc from None
        lines=[x[len(marker):] for x in (p.stdout+'\n'+p.stderr).splitlines() if x.startswith(marker)]
        if p.returncode or len(lines)!=1:
            output=p.stdout+'\n'+p.stderr
            match=re.search(r'\b(MIAOSHOU_[A-Z_]+|WAITING_USER|AUTH_REQUIRED|ACCOUNT_SCOPE_CHANGED)\b',output)
            code=match.group(1) if match else 'SOURCE_TRANSPORT_FAILED'
            kind=next((x for x in ('ElementResolutionError','PageEvaluationTimeout','TimeoutError','PageClosedError') if x in output),'BrowserError')
            exc=ValueError(code);exc.details={'stage':marker.strip(':').lower(),'process_exit':p.returncode,'error_kind':kind};raise exc
        return json.loads(lines[0])
    def available(self):
        return self.readiness().get('ready',False)
    def readiness(self):
        try:self.resolve(self.host.browser_space.get() or {});return {'ready':True}
        except Exception as exc:
            code=str(exc)
            return {'ready':False,'error':code if code in ('WAITING_USER','AUTH_REQUIRED','TMS_PAGE_REQUIRED','BROWSER_BINDING_REQUIRED','WAITING_CONNECTION') else 'WAITING_CONNECTION'}
    def directory(self):
        binding=self.host.repo.get('binding') or {};target=self.resolve(self.host.browser_space.get() or {})
        with self.host.browser_space.lock:
            js='const INPUT='+json.dumps(target)+';\n'+Path(__file__).with_name('directory.js').read_text()
            ms=self._node(js,'AOV_DIRECTORY:',120)
            js='const x='+json.dumps({**target,'account':binding['account']})+''';const p=(await taskSpace(x.space_id)).page(x.page_label);
const rows=await p.evaluate(x=>{const e=document.querySelector('#app')?.__vue__?.$store?.state?.employee;const id=v=>typeof v==='string'?v:Number.isSafeInteger(v)?String(v):null;const a=e?{employee_id:id(e.id),supplier_id:id(e.supplierId),customer_id:id(e.customerId)}:null;if(!a||Object.keys(x.account).some(k=>a[k]!==x.account[k]))throw Error('ACCOUNT_SCOPE_CHANGED');let v=document.querySelector('input[placeholder*="SKU"]')?.closest('.el-input')?.__vue__;for(let i=0;v&&i<20;i++,v=v.$parent)if(Array.isArray(v.shopList))return v.shopList.filter(s=>s.platformId===104&&String(s.companyId)===x.account.customer_id).map(s=>({id:String(s.id),name:s.name,company_id:String(s.companyId),platform_shop_id:typeof s.platformShopId==='string'?s.platformShopId:null}));throw Error('TMS_DIRECTORY_REQUIRED')},x);console.log('AOV_TMS_DIRECTORY:'+JSON.stringify(rows));'''
            tms=self._node(js,'AOV_TMS_DIRECTORY:',20)
        expected={str(s['shop_id']) for s in binding.get('stores',[]) if s.get('platform_id')==104}
        if not tms or not expected.issubset({s['id'] for s in tms}):raise ValueError('TMS_DIRECTORY_IDENTITY_CHANGED')
        # Account verification remains in the existing TMS collector on every batch.
        self.real_verified=self.status()['verified']
        return dict(tms=tms,miaoshou=ms,target=target)
    def batch(self,job,shops,target):
        binding=self.host.repo.get('binding') or {}
        target=self.resolve(self.host.browser_space.get() or {})
        with self.host.browser_space.lock:
            kind=job['kind']
            if kind=='tms':
                scope=dict(start=job['start'],end=job['end'],account=binding['account'],shop_ids=[s['id'] for s in shops],identity_shop_ids=sorted(s['id'] for s in self._directory['tms']),source_ranges={p:request_window(p,job['start'],job['end']) for p in ('current','completed')},aov=True)
                try:value=EgoOrderTransport().collect({**target,'account':binding['account']},scope,500)
                except Exception as exc:
                    if str(exc)=='FAST_TIMEOUT':
                        # The killed transport can leave late DOM work behind.
                        # Reload this owned page before any smaller query starts.
                        fresh=self.resolve(self.host.browser_space.get() or {})
                        program='const x='+json.dumps(fresh)+''';const p=(await taskSpace(x.space_id)).page(x.page_label);await p.reload();await p.waitForFunction(()=>document.readyState==='complete',undefined,{timeout:15000});console.log('AOV_BROWSER_RESET:'+JSON.stringify({reset:true}));'''
                        try:self._node(program,'AOV_BROWSER_RESET:',35)
                        except Exception:
                            error=ValueError('TMS_BROWSER_NOT_READY');error.details={'stage':'reset','reason':'PAGE_UNRESPONSIVE'}
                            raise error from None
                    raise
                if value.get('account')!=binding['account'] or [b.get('partition') for b in value.get('batches',[])]!=['current','completed']:raise ValueError('TMS_COVERAGE_UNVERIFIED')
                rows=[]
                for batch in value['batches']:
                    _validate_partition(batch,scope,500,Decimal(0))
                    for p in batch['pages']:rows.extend(normalize_tms(r) for r in p['rows'])
                return dict(orders=rows,metrics={'pages':sum(len(b['pages']) for b in value['batches']),'page_size':500,'source_rows':len(rows)})
            if kind=='miaoshou':
                spec=dict(space_id=target['space_id'],shop_name='',orders_only=True,range=request_window('current',job['start'],job['end']))
                js='const INPUT='+json.dumps(spec)+';\n'+Path(__file__).with_name('miaoshou.js').read_text()
                value=self._node(js,'AOV_SOURCE:',200)
                if value.get('coverage')!='COMPLETE':
                    error=ValueError(value.get('error','MIAOSHOU_SOURCE_FAILED'));error.details={'stage':value.get('stage'),'kind':value.get('error_kind'),'filters':value.get('filter_diff'),**(value.get('diagnostic') or {})};raise error
                selected={s['miaoshou_shop_id'] for s in shops}
                return dict(rows=[r for r in value['rows'] if r['shop_id'] in selected and r['platform']=='tiktok'],source_total=value['total'],metrics={'pages':value['pages']+1,'page_size':value['page_size'],'source_rows':value['total']})
            if kind=='refunds':
                shop=shops[0] if job.get('shared') else next(s for s in shops if s['id']==job['shop_id'])
                spec=dict(shared=bool(job.get('shared')),shop_ids=[s['miaoshou_shop_id'] for s in shops],space_id=target['space_id'],shop_name=shop['miaoshou_shop_name'],shop_id=shop['miaoshou_shop_id'],range=request_window('current',job['start'],job['end']) if job.get('start') and job.get('end') else None)
                js=Path(__file__).with_name('miaoshou_refunds.js').read_text()+'\nconst x='+json.dumps(spec)+''';const t=await taskSpace(x.space_id);const tb=(await t.tabs()).find(p=>p.url.startsWith('https://erp.91miaoshou.com/'));const p=tb.label?t.page(tb.label):await t.adopt(tb.page);let value,stage='shop-health';try{if(!x.shared)await verifyMiaoshouShop(p,x);stage='refunds';value={refunds:await readMiaoshouRefunds(p,x),metrics:await p.evaluate(()=>window.__aovRefundMetrics)}}catch(e){value={error:/^(MIAOSHOU_|WAITING_|AUTH_REQUIRED)/.test(e.message)?e.message:'SOURCE_TRANSPORT_FAILED',diagnostic:{stage:e.aovStage||stage,error_kind:e.name}}}finally{await p.goto('https://erp.91miaoshou.com/order/package/all?appPackageTab=all')}console.log('AOV_REFUNDS:'+JSON.stringify(value));'''
                value=self._node(js,'AOV_REFUNDS:',190)
                if 'refunds' not in value:
                    exc=ValueError(value.get('error','MIAOSHOU_REFUND_INCOMPLETE'));exc.details=value.get('diagnostic');raise exc
                return value
        raise ValueError('UNKNOWN_BATCH')
