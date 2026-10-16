"""Independent dual-source adapter using existing TMS pagination and normal Miaoshou UI.

Source fields fail closed; unknown refund meanings are never guessed. Read-only
source verification is recorded separately from successful arithmetic tests.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import time
import threading
from .domain import instant, number
from ..tms_orders import EgoOrderTransport, _validate_partition
from ..tms_core.source_time import request_window

PAID={'AWAITING_SHIPMENT','AWAITING_COLLECTION','IN_TRANSIT','DELIVERED','COMPLETED','PARTIALLY_SHIPPING','ON_HOLD'}
CANCELLED={'CANCELLED','CANCELED','CLOSED'}

def payment(value):
    return 'PAID' if value in PAID else 'UNPAID' if value=='UNPAID' else 'CANCELLED' if value in CANCELLED else 'UNKNOWN'

def normalize_tms(raw):
    pid=raw.get('platformOrderId') or raw.get('originalOrderId')
    if not isinstance(pid,str):raise ValueError('TMS_PLATFORM_ID_UNVERIFIED')
    when=raw.get('orderTime');amount=raw.get('orderFee')
    when=None if when=='' else when;amount=None if amount=='' else amount
    if when is not None and (type(when) not in (int,float) or not 0<when<2**53):raise ValueError('TMS_PAYMENT_TIME_UNVERIFIED')
    return dict(platform_order_id=pid,shop_id=str(raw['shopId']),
        paid_at=None if when is None else datetime.fromtimestamp(when/1000,timezone.utc).isoformat(),
        money=None if amount is None else dict(value=str(amount),currency='CNY',basis='ORDER_TOTAL'),
        payment_status=None if raw.get('platformOrderStatus') in (None,'') else payment(raw.get('platformOrderStatus')),
        refund_status=None if raw.get('refundStatus') in (None,'') and raw.get('refundAmount') in (None,'') else 'UNKNOWN',
        skus=sorted({i.get('stockSku') or i.get('platformSku') for i in raw['itemList'] if i.get('stockSku') or i.get('platformSku')}))

def refund_status(raw, refunds):
    # Order history reports appRefundStatus=0 even for completed refunds.
    # Only a complete read of both after-sales tabs can establish NONE.
    if refunds is None:return 'UNKNOWN'
    matching=[r for r in refunds if r['platform_order_id']==raw['platform_order_id']]
    completed=[]
    for r in matching:
        if r['shop_id']!=raw['shop_id']:raise ValueError('MIAOSHOU_REFUND_ID_UNVERIFIED')
        if r.get('kind')=='cancellation':
            if r['status']=='cancelled' and r['platform_status']=='CANCELLATION_REQUEST_COMPLETE':return 'FULL'
            raise ValueError('MIAOSHOU_REFUND_STATUS_UNVERIFIED')
        if r['status']=='success':
            if r['platform_status']!='RETURN_OR_REFUND_REQUEST_COMPLETE':raise ValueError('MIAOSHOU_REFUND_STATUS_UNVERIFIED')
            if r['currency']!=raw['currency'] or number(r['pay_amount'])!=number(raw['pay_amount']):raise ValueError('MIAOSHOU_REFUND_AMOUNT_UNVERIFIED')
            completed.append(number(r['refund_amount']))
        elif (r['status'],r['platform_status']) not in {
            ('closed','RETURN_OR_REFUND_REQUEST_CANCEL'),
            ('processing','REFUND_OR_RETURN_REQUEST_REJECT'),
            ('wait_process','AWAITING_BUYER_SHIP'),
            ('wait_process','BUYER_SHIPPED_ITEM'),
        }:raise ValueError('MIAOSHOU_REFUND_STATUS_UNVERIFIED')
    amount=sum(completed,Decimal(0))
    if not amount:return 'NONE'
    paid=number(raw['pay_amount'])
    if not paid or amount>paid:raise ValueError('MIAOSHOU_REFUND_AMOUNT_UNVERIFIED')
    return 'FULL' if amount==paid else 'PARTIAL'

def normalize_miaoshou(raw, refunds=None):
    if raw['platform']!='tiktok':raise ValueError('MIAOSHOU_PLATFORM_UNVERIFIED')
    if raw.get('split') not in (None,False,0,'0'):raise ValueError('MIAOSHOU_SPLIT_AMOUNT_UNVERIFIED')
    # Observed UI says ORDER TOTAL in CNY; reject inconsistent package/order amounts.
    original=number(raw['order_amount']);rate=number(raw['exchange_rate']);cny=number(str(raw['cny_total']))
    if not rate or abs(original/rate-cny)>Decimal('.02'):raise ValueError('MIAOSHOU_AMOUNT_UNVERIFIED')
    refund=refund_status(raw,refunds)
    when=datetime.strptime(raw['paid_at'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=8))) if raw.get('paid_at') else None
    return dict(platform_order_id=raw['platform_order_id'],shop_id=raw['shop_id'],
        paid_at=when.isoformat() if when else None,money=dict(value=str(cny),currency='CNY',basis='ORDER_TOTAL'),
        payment_status=payment(raw['status']),refund_status=refund,skus=raw['skus'])

class DualSource:
    def __init__(self,host):self.host=host;self.real_verified=False;self._control=threading.local()
    def run_browser_task(self,fn,*args):
        """Real work may reclaim immediately; retain normal Agent control afterward."""
        self._control.active=True
        try:return fn(*args)
        finally:
            self._control.active=False
            self._control.claimed=None
    def _remember_claim(self,output,shared):
        if isinstance(output,bytes):output=output.decode('utf-8',errors='replace')
        if 'AOV_CONTROL:claimed' in (output or '').splitlines():self._control.claimed=dict(shared)
    def status(self):
        contract=self.host.repo.get('aov_source_verification') or {}
        return dict(verified=contract.get('verified') is True,
            note=contract.get('note') or '待完成双来源金额、时区及退款覆盖核验；自动监控暂不可启用。',
            checked_at=contract.get('checked_at'),details=contract.get('details',[]))
    def resolve(self,shared):
        script='const s='+json.dumps(dict(shared,task_active=getattr(self._control,'active',False)))+''';
const entries=await listTaskSpaces();
const matches=entries.filter(e=>e.name===s.expected_name&&e.profileId===s.profile_id&&e.profileName===s.profile_name);
const candidate=matches.length===1?matches[0]:null;
const e=candidate&&(candidate.id===s.space_id||candidate.taskId===s.expected_name)?candidate:null;
let browser;
let result;
if(!e)result={error:'BROWSER_BINDING_REQUIRED'};
else {
 let task;
 if(e.ownership==='agent')task=await taskSpace(e.id);
 else if(s.task_active===true&&s.auto_takeover===true&&['user','agentDelegatedToUser'].includes(e.ownership)) {
  try { task=e.ownership==='agentDelegatedToUser'?await takeOverTaskSpace(e.id):await claimTaskSpace(e.id);if(task.spaceId===e.id)console.log('AOV_CONTROL:claimed'); }
  catch { result={error:'WAITING_USER'}; }
 } else result={error:'WAITING_USER'};
 if(!result&&task.spaceId!==e.id)result={error:'BROWSER_BINDING_REQUIRED'};
 if(!result) {
 browser={space_id:e.id,space_name:e.name,profile_id:e.profileId,profile_name:e.profileName};
 const all=await task.tabs(),tabs=all.filter(t=>t.url.startsWith('https://tms.mabangerp.com/#/myorder'));
 if(tabs.length!==1)result={error:all.some(t=>t.url.startsWith('https://tms.mabangerp.com/#/login'))?'AUTH_REQUIRED':'TMS_PAGE_REQUIRED'};
 else {const p=tabs[0].label?task.page(tabs[0].label):await task.adopt(tabs[0].page);result={space_id:e.id,page_label:p.label};}
 }
}
console.log('AOV_BINDING:'+JSON.stringify({...result,...(browser?{browser}:{})}));
'''
        try:p=subprocess.run([str(Path.home()/'.local/bin/ego-browser'),'nodejs'],input=script,text=True,capture_output=True,timeout=15)
        except subprocess.TimeoutExpired as exc:
            self._remember_claim(exc.stdout,shared)
            raise ValueError('WAITING_CONNECTION') from None
        self._remember_claim(p.stdout,shared)
        lines=[x[len('AOV_BINDING:'):] for x in (p.stdout+'\n'+p.stderr).splitlines() if x.startswith('AOV_BINDING:')]
        if p.returncode or len(lines)!=1:
            raise ValueError('WAITING_CONNECTION')
        value=json.loads(lines[0])
        browser=value.pop('browser',None)
        if self.host is not None:
            if browser:
                try:self.host.browser_space.record_space(shared,browser)
                except ValueError:raise ValueError('BROWSER_BINDING_REQUIRED') from None
            elif not value.get('error'):raise ValueError('BROWSER_BINDING_REQUIRED')
        if value.get('error'):raise ValueError(value['error'])
        return value
    def collect(self,config,start,end):
        binding=self.host.repo.get('binding') or {};target={}
        shared=self.host.browser_space.get() or {}
        if not binding.get('account') or not shared:
            return {s['id']:{'tms':{'coverage':'INCOMPLETE','error':'BROWSER_BINDING_REQUIRED'}} for s in config['shops'] if s['monitor']}
        try:target=self.resolve(shared)
        except Exception:return {s['id']:{'tms':{'coverage':'INCOMPLETE','error':'BROWSER_BINDING_REQUIRED'}} for s in config['shops'] if s['monitor']}
        self.real_verified=self.status()['verified']
        result={};started=time.monotonic()
        # The caller holds Service.lock. Each assigned space has its own thread mutex.
        with self.host.browser_space.lock:
            for shop in config['shops']:
                if not shop['monitor']:continue
                pair={};result[shop['id']]=pair
                if time.monotonic()-started>600:
                    pair['tms']={'coverage':'INCOMPLETE','error':'RUN_TIME_LIMIT'};continue
                scope=dict(start=start,end=end,account=binding['account'],shop_ids=[shop['id']],
                    identity_shop_ids=sorted(str(s['shop_id']) for s in binding['stores'] if s.get('platform_id')==104),
                    source_ranges={p:request_window(p,start,end) for p in ('current','completed')},aov=True)
                try:
                    captured=EgoOrderTransport().collect({**target,'account':binding['account']},scope,500)
                    if captured.get('account')!=binding['account'] or [b.get('partition') for b in captured.get('batches',[])]!=['current','completed']:raise ValueError('TMS_COVERAGE_UNVERIFIED')
                    orders=[]
                    for batch in captured['batches']:
                        _validate_partition(batch,scope,500,Decimal(0))
                        for page in batch['pages']:
                            orders.extend(normalize_tms(r) for r in page['rows'])
                    pair['tms']={'coverage':'COMPLETE','orders':orders}
                except Exception as e:pair['tms']={'coverage':'INCOMPLETE','error':str(e) if str(e).isupper() else 'TMS_SOURCE_FAILED'}
                try:
                    spec=dict(space_id=target['space_id'],shop_name=shop['miaoshou_shop_name'],shop_id=shop['miaoshou_shop_id'],range=request_window('current',start,end))
                    script=Path(__file__).with_name('miaoshou_refunds.js').read_text()+'\nconst INPUT='+json.dumps(spec)+';\n'+Path(__file__).with_name('miaoshou.js').read_text()
                    p=subprocess.run([str(Path.home()/'.local/bin/ego-browser'),'nodejs'],input=script,text=True,capture_output=True,timeout=340)
                    lines=[x[len('AOV_SOURCE:'):] for x in (p.stdout+'\n'+p.stderr).splitlines() if x.startswith('AOV_SOURCE:')]
                    if p.returncode or len(lines)!=1:raise ValueError('MIAOSHOU_SOURCE_FAILED')
                    raw=json.loads(lines[0]);pair['miaoshou']=raw
                    if raw.get('coverage')=='COMPLETE':
                        # Keyword can match several shops; identity uses confirmed ID only.
                        rows=[o for o in raw['rows'] if o['shop_id']==shop['miaoshou_shop_id']]
                        if raw['rows'] and not rows:raise ValueError('MIAOSHOU_SHOP_MAPPING_CHANGED')
                        orders=[normalize_miaoshou(r,raw.get('refunds')) for r in rows]
                        if any(o['paid_at'] and not instant(start)<=instant(o['paid_at'])<=instant(end) for o in orders):raise ValueError('MIAOSHOU_TIME_UNVERIFIED')
                        pair['miaoshou']={'coverage':'COMPLETE','orders':orders,'pages':raw['pages']}
                        if raw.get('sync_health')=='UNVERIFIED':pair['miaoshou'].update(coverage='INCOMPLETE',error='MIAOSHOU_SYNC_HEALTH_UNVERIFIED')
                except Exception as e:pair['miaoshou']={'coverage':'INCOMPLETE','error':str(e) if str(e).isupper() else 'MIAOSHOU_SOURCE_FAILED'}
        return result
