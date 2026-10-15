import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from order_pulse.aov_monitor.domain import *
from order_pulse.aov_monitor.store import Store
from order_pulse.aov_monitor.service import Monitor,slot_at
from order_pulse.aov_monitor.sources import normalize_miaoshou,normalize_tms,refund_status
from order_pulse.aov_monitor.fx import Rates

AT=datetime(2026,10,13,4,tzinfo=timezone.utc)
RULE={w:dict(lower='5',upper='15',min_orders=10) for w in WINDOWS}
SHOP=dict(id='1',name='店铺',tms_shop_id='1',miaoshou_shop_id='m1',miaoshou_shop_name='妙手店铺',monitor=True,push=True,mapping_confirmed=True,currency='BRL',sku_aliases={'tms':{},'miaoshou':{}},products=[dict(family_id='f',name='眼霜',skus=['A'],rules=RULE)])
CAT={'f':dict(product_name='眼霜',member_skus=['A'])}
STORES=[dict(shop_id=1,name='店铺',platform_id=104)]
def order(pid='001',source='tms',**fields):
 return dict(platform_order_id=pid,shop_id='1' if source=='tms' else 'm1',paid_at=(AT-timedelta(hours=2)).isoformat(),money=dict(value='10',currency='CNY',basis='ORDER_TOTAL'),payment_status='PAID',refund_status='NONE',skus=['A'],**fields) if not fields else dict(order(pid,source),**fields)
def pair(t=None,m=None):return {'tms':dict(coverage='COMPLETE',orders=t or []),'miaoshou':dict(coverage='COMPLETE',orders=m or [])}
def canonical(hour,amount='10',skus=None):return dict(paid_at=(AT-timedelta(hours=hour)).isoformat(),amount_rmb=amount,skus=skus or ['A'])

class DomainTests(unittest.TestCase):
 def test_two_half_open_windows(self):
  orders=[canonical(h) for h in [0,1,24,48,72,96,144,168,336,337]]
  m=calculate(orders,AT.isoformat(),['A'],{})
  self.assertEqual([(m[w]['current_count'],m[w]['previous_count']) for w in WINDOWS],[(2,1),(4,2)])
 def test_tms_fields_win_and_fill_only_null(self):
  a=order(money=None,paid_at=None);b=order(source='miaoshou',money=dict(value='12',currency='CNY',basis='ORDER_TOTAL'))
  rows,notes,errors=reconcile(pair([a],[b]),SHOP)
  self.assertFalse(errors);self.assertEqual(rows[0]['amount_rmb'],'12');self.assertEqual(rows[0]['provenance']['money'],'miaoshou');self.assertEqual(notes['matched'],1)
  a['money']=dict(value='0',currency='CNY',basis='ORDER_TOTAL')
  rows,notes,errors=reconcile(pair([a],[b]),SHOP);self.assertFalse(errors);self.assertEqual(rows[0]['amount_rmb'],'0');self.assertGreater(notes['differences'],0)
  a['money']['value']='invalid';self.assertTrue(reconcile(pair([a],[b]),SHOP)[2])
 def test_money_never_combines_currency_or_basis(self):
  a=order(money=dict(value='3',currency='BRL',basis='ORDER_TOTAL'))
  self.assertTrue(reconcile(pair([a],[order(source='miaoshou')]),SHOP)[2])
 def test_string_ids_shop_identity_and_duplicates(self):
  self.assertEqual(len(reconcile(pair([order(),order()],[]),SHOP)[0]),1)
  self.assertTrue(reconcile(pair([order(platform_order_id=123)],[]),SHOP)[2])
  self.assertTrue(reconcile(pair([order(shop_id='2')],[]),SHOP)[2])
  self.assertTrue(reconcile(pair([order(),order(money=dict(value='11',currency='CNY',basis='ORDER_TOTAL'))]),SHOP)[2])
 def test_failure_is_not_absence(self):
  p=pair([order()]);p['miaoshou']={'coverage':'INCOMPLETE','error':'OFFLINE'}
  self.assertEqual(reconcile(p,SHOP)[2],['OFFLINE'])
 def test_refund_exclusion_partial_original(self):
  rows=[order(str(i),payment_status=s) for i,s in enumerate(['UNPAID','CANCELLED','VOID','FULL_REFUND','PAID'])]
  rows[-1]['refund_status']='PARTIAL';out,_,err=reconcile(pair(rows),SHOP)
  self.assertFalse(err);self.assertEqual(len(out),1);self.assertEqual(out[0]['amount_rmb'],'10')
  self.assertFalse(reconcile(pair([order(refund_status='FULL')]),SHOP)[0])
 def test_mixed_products_whole_order_and_decimal(self):
  rows=[canonical(1,'0.1',['A','B']),canonical(2,'0.2',['A'])]
  a=calculate(rows,AT.isoformat(),['A','B'],{},dict(fx_rate='2.5'))['24h']
  b=calculate(rows,AT.isoformat(),['B'],{})['24h']
  self.assertEqual(a['current_count'],2);self.assertEqual(a['aov_rmb'],'0.15');self.assertEqual(a['aov_reference'],'0.38');self.assertEqual(b['aov_rmb'],'0.10')
 def test_sample_minimum_both_periods_and_zero_prior(self):
  rows=[canonical(1,'20')]*10+[canonical(25,'0')]*9
  self.assertEqual(calculate(rows,AT.isoformat(),['A'],RULE)['24h']['status'],'INSUFFICIENT')
  rows.append(canonical(25,'0'));m=calculate(rows,AT.isoformat(),['A'],RULE)['24h']
  self.assertIsNone(m['change_pct']);self.assertEqual(m['directions'],['HIGH'])
 def test_threshold_boundary_and_or(self):
  rows=[canonical(1,'15')]*10+[canonical(25,'10')]*10
  rules={'24h':dict(upper='15',rise_pct='50')};m=calculate(rows,AT.isoformat(),['A'],rules)['24h'];self.assertEqual(m['status'],'NORMAL')
  rules['24h']['rise_pct']='49';self.assertEqual(calculate(rows,AT.isoformat(),['A'],rules)['24h']['directions'],['HIGH'])
 def test_two_auto_manual_no_repeat_recovery_and_gap(self):
  metric={'status':'ABNORMAL','directions':['HIGH']};a=AT.isoformat();b=(AT+timedelta(hours=2)).isoformat()
  s,e=transition(None,metric,a,True,'v1');self.assertIsNone(e)
  manual,e=transition(s,metric,b,False,'v1');self.assertEqual(manual,s);self.assertIsNone(e)
  s,e=transition(s,metric,b,True,'v1');self.assertEqual(e,'ABNORMAL')
  self.assertIsNone(transition(s,metric,b,True,'v1')[1])
  s,e=transition(s,metric,(AT+timedelta(hours=4)).isoformat(),True,'v1');self.assertIsNone(e)
  s,e=transition(s,{'status':'NORMAL'},(AT+timedelta(hours=6)).isoformat(),True,'v1');self.assertEqual(e,'RECOVERY')
  self.assertIsNone(transition(s,{'status':'NORMAL'},(AT+timedelta(hours=8)).isoformat(),True,'v1')[1])
  s,_=transition(None,metric,a,True,'v1');self.assertIsNone(transition(s,metric,(AT+timedelta(hours=4)).isoformat(),True,'v1')[1])
  self.assertIsNone(transition(s,metric,b,True,'v2')[1])
 def test_incomplete_resets_pending_not_active(self):
  s,_=transition(None,{'status':'ABNORMAL','directions':['LOW']},AT.isoformat(),True,'v')
  s,_=transition(s,{'status':'INCOMPLETE'},(AT+timedelta(hours=2)).isoformat(),True,'v');self.assertEqual(s['streak'],0)
  self.assertEqual(transition(dict(revision='v',active='LOW'),{'status':'INCOMPLETE'},AT.isoformat(),True,'v')[0]['active'],'LOW')
 def test_even_shanghai_slot(self):self.assertEqual(slot_at(AT+timedelta(minutes=59)),AT)

class StoreTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'test.db')
 def tearDown(self):self.tmp.cleanup()
 def test_defaults_validation_and_optimistic_save(self):
  self.assertFalse(self.store.config()['enabled']);self.assertFalse(self.store.config()['shops'])
  s=deepcopy(SHOP);s['monitor']=False
  with self.assertRaises(ValueError):self.store.save_config(dict(revision=1,enabled=False,shops=[s]),CAT,STORES)
  good=self.store.save_config(dict(revision=1,enabled=False,shops=[SHOP]),CAT,STORES);self.assertEqual(good['revision'],2)
  with self.assertRaises(ValueError):self.store.save_config(dict(revision=1,enabled=False,shops=[SHOP]),CAT,STORES)
  pending=self.store.save_config(dict(revision=2,enabled=True,shops=[SHOP]),CAT,STORES)
  self.assertEqual(self.store.verification(pending),dict(ready=[],pending=['1:f']))
 def test_malformed_nested_settings_are_validation_errors(self):
  for mutate in (lambda s:s['products'][0].update(family_id=[]),lambda s:s.update(sku_aliases={'miaoshou':{'A':[]}})):
   shop=deepcopy(SHOP);mutate(shop)
   with self.assertRaises(ValueError):self.store.save_config(dict(revision=1,enabled=False,shops=[shop]),CAT,STORES)
 def test_enable_requires_real_same_scope_then_persist(self):
  c=self.store.save_config(dict(revision=1,enabled=False,shops=[SHOP]),CAT,STORES)
  rid=self.store.start(None,{});self.store.finish(rid,dict(status='SUCCEEDED',real_sources_verified=True,scope_hash=digest(c['shops'])))
  c=self.store.save_config(dict(c,enabled=True),CAT,STORES);self.assertTrue(Store(self.store.path).config()['enabled'])
 def test_slot_idempotence_restart_and_events(self):
  rid=self.store.start('slot',{});self.assertIsNone(self.store.start('slot',{}));self.store.recover();self.assertEqual(self.store.history()[0]['status'],'INTERRUPTED')
  self.store.finish(rid,dict(status='SUCCEEDED'),{'x':dict(active='HIGH',streak=1)},[dict(id='event')],{'1':[dict(order_key='hash',amount_rmb='2')]})
  self.store.event_status('event','sending');self.store.recover();self.assertEqual(self.store.events()[0]['status'],'uncertain');self.assertEqual(self.store.alert_states()['x']['active'],'HIGH');self.assertEqual(self.store.alert_states()['x']['streak'],0)
  self.assertEqual(self.store.snapshot()['status'],'SUCCEEDED')
  with self.store.db() as db:self.assertEqual(db.execute('select count(*) from aov_monitor_orders').fetchone()[0],1)
 def test_display_name_persists_without_changing_identity_or_scope(self):
  shop=deepcopy(SHOP);shop['products'][0].update(display_name='  瞬效眼霜  ',skus=['not-trusted'],name='not-trusted')
  config=self.store.save_config(dict(revision=1,enabled=False,shops=[shop]),CAT,STORES)
  p=Store(self.store.path).config()['shops'][0]['products'][0]
  self.assertEqual(p['display_name'],'瞬效眼霜');self.assertEqual(p['name'],'眼霜');self.assertEqual(p['skus'],['A'])
  original=deepcopy(config['shops']);original[0]['products'][0].pop('display_name')
  self.assertEqual(monitor_scope(config['shops']),original)
 def test_label_only_save_keeps_enabled_monitor_and_alert_state(self):
  c=self.store.save_config(dict(revision=1,enabled=False,shops=[SHOP]),CAT,STORES)
  rid=self.store.start(None,{});self.store.finish(rid,dict(status='SUCCEEDED',real_sources_verified=True,scope_hash=digest(c['shops'])))
  c=self.store.save_config(dict(c,enabled=True),CAT,STORES)
  failed=self.store.start(None,{});self.store.finish(failed,dict(status='FAILED'),{'test':dict(active='HIGH',streak=2)})
  c['shops'][0]['products'][0]['display_name']='手机用眼霜'
  c=self.store.save_config(c,CAT,STORES)
  self.assertTrue(c['enabled']);self.assertEqual(self.store.alert_states()['test']['streak'],2)
  c['shops'][0]['products'][0]['rules']['24h']['upper']='12'
  c=self.store.save_config(c,CAT,STORES)
  self.assertEqual(self.store.verification(c),dict(ready=[],pending=['1:f']))
  self.assertTrue(c['enabled'])
 def test_display_name_validation_and_clear_to_catalog_name(self):
  for label in ([], 'x'*101, '一行\n二行'):
   s=deepcopy(SHOP);s['products'][0]['display_name']=label
   with self.assertRaises(ValueError):self.store.save_config(dict(revision=1,enabled=False,shops=[s]),CAT,STORES)
  s=deepcopy(SHOP);s['products'][0]['display_name']='  '
  c=self.store.save_config(dict(revision=1,enabled=False,shops=[s]),CAT,STORES)
  self.assertNotIn('display_name',c['shops'][0]['products'][0])
 def test_unchecked_product_keeps_confirmed_catalog_alias(self):
  s=deepcopy(SHOP);s.update(monitor=False,push=False,products=[],sku_aliases={'miaoshou':{'old':'A'}})
  c=self.store.save_config(dict(revision=1,enabled=False,shops=[s]),CAT,STORES)
  self.assertEqual(c['shops'][0]['sku_aliases']['miaoshou'],{'old':'A'})
 def test_new_shop_saves_pending_without_blocking_existing_verified_shop(self):
  c=self.store.save_config(dict(revision=1,enabled=False,shops=[SHOP]),CAT,STORES)
  rid=self.store.start(None,{});self.store.finish(rid,dict(status='SUCCEEDED',real_sources_verified=True,scope_hash=digest(c['shops'])))
  c=self.store.save_config(dict(c,enabled=True),CAT,STORES)
  other=deepcopy(SHOP);other.update(id='2',miaoshou_shop_id='m2',miaoshou_shop_name='新店铺')
  c['shops'].append(other)
  c=self.store.save_config(c,CAT,STORES+[dict(shop_id=2,name='新店铺',platform_id=104)])
  self.assertEqual(self.store.verification(c),dict(ready=['1:f'],pending=['2:f']))
  self.assertEqual(Store(self.store.path).config()['shops'][1]['products'][0]['skus'],['A'])
 def test_only_manual_complete_real_scope_becomes_verified(self):
  c=self.store.save_config(dict(revision=1,enabled=True,shops=[SHOP]),CAT,STORES)
  group=dict(key='1:f',status='COMPLETE',verification_hash=group_scope_hash(c['shops'][0],c['shops'][0]['products'][0]))
  for trigger,real,status in [('automatic',True,'COMPLETE'),('manual',False,'COMPLETE'),('manual',True,'INCOMPLETE')]:
   rid=self.store.start(None,{})
   self.store.finish(rid,dict(status='PARTIAL',trigger=trigger,real_sources_verified=real,groups=[dict(group,status=status)]))
   self.assertEqual(self.store.verification(c)['ready'],[])
  rid=self.store.start(None,{})
  self.store.finish(rid,dict(status='PARTIAL',trigger='manual',real_sources_verified=True,groups=[group]))
  self.assertEqual(self.store.verification(c),dict(ready=['1:f'],pending=[]))

class FxTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'test.db')
 def tearDown(self):self.tmp.cleanup()
 def test_cache_expiry_and_independent_currency(self):
  def unavailable(c,at):raise RuntimeError('offline')
  rates=Rates(self.store,unavailable)
  self.store.fx('BRL',dict(fx_rate='0.7',fx_pair='CNY/BRL',fx_fetched_at=(AT-timedelta(hours=23)).isoformat()))
  self.assertEqual(rates.get('BRL',AT)['fx_status'],'CACHED')
  self.assertEqual(rates.get('BRL',AT+timedelta(hours=2))['fx_status'],'UNAVAILABLE')
  self.assertEqual(rates.get('MXN',AT)['fx_status'],'UNAVAILABLE')



class ServiceTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'t.db'
  class Repo:
   path=self.path
   def active_catalog(self):return {'families':CAT}
   def get(self,key):return {'stores':STORES} if key=='binding' else None
  class Source:
   real_verified=True
   def status(self):return {'verified':True}
   def collect(self,*args):return {'1':pair([order(str(i),paid_at=(AT-timedelta(hours=h)).isoformat(),money=dict(value='20' if h<24 else '10',currency='CNY',basis='ORDER_TOTAL')) for i,h in enumerate([1]*10+[25]*10)])}
  class FakeRates:
   def get(self,k,at):return {'fx_rate':'2','fx_pair':'CNY/BRL'}
  self.sent=[]
  outer=self
  class Client:
   def __init__(self,*a,**kw):pass
   async def send(self,text,event_id,**kwargs):outer.sent.append((text,event_id));return {'status':'success'}
  self.clock=AT
  self.monitor=Monitor(SimpleNamespace(repo=Repo(),lock=asyncio.Lock(),secrets=SimpleNamespace(configured=lambda:True,get=lambda:{'webhook':'test'})),Source(),FakeRates(),lambda:self.clock,Client)
  self.cfg=self.monitor.settings(dict(revision=1,enabled=False,shops=[SHOP]))
 async def asyncTearDown(self):self.tmp.cleanup()
 async def test_background_manual_then_two_auto_and_restart_dedupe(self):
  m=self.monitor;r=await m.start();await m.worker
  self.assertEqual(m.store.snapshot()['status'],'SUCCEEDED');self.assertEqual(self.sent,[])
  m.settings(dict(m.store.config(),enabled=True))
  await m.start(True);await m.worker;self.assertEqual(len(self.sent),0)
  self.clock+=timedelta(hours=2);await m.start(True);await m.worker;self.assertEqual(len(self.sent),1)
  self.assertIsNone(await m.start(True));m.store.recover();self.assertIsNone(await m.start(True));self.assertEqual(len(self.sent),1)
  self.assertEqual(len(m.store.history()),3)
 async def test_incomplete_retains_last_complete_no_send(self):
  m=self.monitor;await m.start();await m.worker
  m.source.collect=lambda *args:{'1':{'tms':{'coverage':'INCOMPLETE','error':'OFFLINE'}}}
  await m.start();await m.worker;g=m.store.snapshot()['groups'][0]
  self.assertEqual(g['status'],'INCOMPLETE');self.assertEqual(g['last_complete']['windows']['24h']['current_count'],10);self.assertFalse(self.sent)
 async def test_read_state_does_not_collect(self):
  m=self.monitor;m.source.collect=lambda *a: self.fail('state must not collect')
  m.state();m.store.history();self.assertFalse(m.store.history())
 async def test_generated_snapshot_uses_display_name_and_same_scope(self):
  c=self.monitor.store.config();c['shops'][0]['products'][0]['display_name']='我的眼霜'
  self.monitor.settings(c);await self.monitor.start();await self.monitor.worker
  snapshot=self.monitor.store.snapshot()
  self.assertEqual(snapshot['groups'][0]['product_name'],'我的眼霜')
  self.assertEqual(snapshot['scope_hash'],digest(monitor_scope(self.monitor.store.config()['shops'])))
  self.assertFalse(self.sent)
 async def test_pending_scope_does_not_start_automatic_collection(self):
  m=self.monitor;m.settings(dict(m.store.config(),enabled=True))
  m.source.collect=lambda *a:self.fail('pending scope must not collect automatically')
  self.assertIsNone(await m.start(True));self.assertFalse(m.store.history());self.assertFalse(self.sent)
  self.assertIsNone(m.state()['next_at'])
 async def test_automatic_collection_filters_new_pending_product(self):
  m=self.monitor;await m.start();await m.worker
  m.settings(dict(m.store.config(),enabled=True))
  catalog=deepcopy(CAT);catalog['new']=dict(product_name='新产品',member_skus=['B']);m.catalog=lambda:catalog
  c=m.store.config();c['shops'][0]['products'].append(dict(family_id='new',rules=RULE))
  m.settings(c);seen=[];collect=m.source.collect
  def inspect(config,*args):seen.append(deepcopy(config));return collect(config,*args)
  m.source.collect=inspect
  await m.start(True);await m.worker
  self.assertEqual([p['family_id'] for p in seen[0]['shops'][0]['products']],['f'])
  self.assertEqual(m.state()['automation'],dict(ready=['1:f'],pending=['1:new']))
  self.assertFalse(self.sent)

class SourceTests(unittest.TestCase):
 def test_tms_platform_id_no_internal_fallback(self):
  raw=dict(orderId='internal',shopId=1,orderTime=AT.timestamp()*1000,orderFee=0,itemList=[dict(stockSku='A')],platformOrderStatus='COMPLETED')
  with self.assertRaises(ValueError):normalize_tms(raw)
  raw['platformOrderId']='0001234567890123456789';o=normalize_tms(raw)
  self.assertEqual(o['platform_order_id'],'0001234567890123456789');self.assertEqual(o['money']['value'],'0');self.assertIsNone(o['refund_status'])
 def test_miaoshou_unknown_refund_is_not_zero(self):
  raw=dict(platform='tiktok',platform_order_id='12345678901234567890',shop_id='m1',order_amount='20',exchange_rate='2',cny_total='10',paid_at='2026-10-13 12:00:00',status='AWAITING_SHIPMENT',refund_status='0',refund_amount=None,skus=['A'],pay_amount='20')
  o=normalize_miaoshou(raw,[]);self.assertEqual(instant(o['paid_at']),AT);self.assertEqual(o['refund_status'],'NONE')
  raw.update(refund_status='unknown',refund_amount=None);self.assertEqual(normalize_miaoshou(raw)['refund_status'],'UNKNOWN')
  raw['refund_amount']='4';self.assertEqual(normalize_miaoshou(raw)['refund_status'],'UNKNOWN')
  raw['refund_amount']='20';self.assertEqual(normalize_miaoshou(raw)['refund_status'],'UNKNOWN')
  raw['split']=True
  with self.assertRaisesRegex(ValueError,'SPLIT'):normalize_miaoshou(raw)
 def test_nonempty_unverified_tms_refund_cannot_be_filled(self):
  r=dict(platformOrderId='001',shopId='1',orderTime=AT.timestamp()*1000,orderFee=10,itemList=[dict(stockSku='A')],platformOrderStatus='COMPLETED',refundStatus='unverified',refundAmount=0)
  a=normalize_tms(r);self.assertEqual(a['refund_status'],'UNKNOWN')
  self.assertTrue(reconcile(pair([a],[order(source='miaoshou')]),SHOP)[2])
 def test_foreign_total_mismatch_blocks(self):
  raw=dict(platform='tiktok',order_amount='20',exchange_rate='2',cny_total='9')
  with self.assertRaisesRegex(ValueError,'AMOUNT'):normalize_miaoshou(raw)
 def test_adapter_does_not_take_over_or_export_credentials(self):
  base=Path(__file__).resolve().parents[1]/'order_pulse/aov_monitor'
  text=(base/'miaoshou.js').read_text()+(base/'sources.py').read_text()
  for forbidden in ('takeOverTaskSpace(', 'claimTaskSpace(', 'document.cookie', 'consigneeInfo', 'localStorage.getItem', 'buyerName'):
   self.assertNotIn(forbidden,text)

 def test_confirmed_paid_on_hold_and_two_pack_alias(self):
  raw=dict(platform='tiktok',platform_order_id='00001',shop_id='m1',order_amount='20',exchange_rate='2',cny_total='10',paid_at='2026-10-13 12:00:00',status='ON_HOLD',currency='BRL',pay_amount='20',skus=['HBYDK2'])
  normalized=normalize_miaoshou(raw,[]);self.assertEqual(normalized['payment_status'],'PAID')
  shop=deepcopy(SHOP);shop['sku_aliases']['miaoshou']={'HBYDK2':'HBYDK1*2'}
  rows,_,errors=reconcile(pair([],[normalized]),shop,{'HBYDK1*2'})
  self.assertFalse(errors);self.assertEqual(rows[0]['skus'],['HBYDK1*2'])
  normalized['paid_at']=None;self.assertTrue(reconcile(pair([],[normalized]),shop)[2])
 def test_completed_refunds_override_false_order_history_zero(self):
  raw=dict(platform_order_id='001',shop_id='m1',currency='MXN',pay_amount='139.00',refund_status='0',refund_amount=None)
  item=dict(id='return1',platform_order_id='001',shop_id='m1',status='success',platform_status='RETURN_OR_REFUND_REQUEST_COMPLETE',currency='MXN',pay_amount='139.00',refund_amount='55.60')
  self.assertEqual(refund_status(raw,None),'UNKNOWN')
  self.assertEqual(refund_status(raw,[]),'NONE')
  self.assertEqual(refund_status(raw,[item]),'PARTIAL')
  item['refund_amount']='139.00';self.assertEqual(refund_status(raw,[item]),'FULL')
  item['currency']='BRL'
  with self.assertRaisesRegex(ValueError,'AMOUNT'):refund_status(raw,[item])
 def test_pending_rejected_refund_and_completed_cancellation(self):
  raw=dict(platform_order_id='001',shop_id='m1',currency='BRL',pay_amount='20')
  item=dict(platform_order_id='001',shop_id='m1',kind='refund',status='processing',platform_status='REFUND_OR_RETURN_REQUEST_REJECT')
  self.assertEqual(refund_status(raw,[item]),'NONE')
  item.update(kind='cancellation',status='cancelled',platform_status='CANCELLATION_REQUEST_COMPLETE')
  self.assertEqual(refund_status(raw,[item]),'FULL')
  item['platform_status']='NEW_UNVERIFIED_STATUS'
  with self.assertRaisesRegex(ValueError,'STATUS'):refund_status(raw,[item])
 def test_normalized_time_and_amount_are_not_false_differences(self):
  a=order();b=order(source='miaoshou',paid_at=instant(a['paid_at']).astimezone(timezone(timedelta(hours=8))).isoformat(),money=dict(value='10.00',currency='CNY',basis='ORDER_TOTAL'))
  _,notes,errors=reconcile(pair([a],[b]),SHOP);self.assertFalse(errors);self.assertEqual(notes['differences'],0)

class ApiTests(unittest.TestCase):
 def test_routes_roundtrip_and_read_only_history(self):
  from fastapi import FastAPI
  from fastapi.responses import JSONResponse
  from fastapi.testclient import TestClient
  from order_pulse.aov_monitor.router import create_router
  with tempfile.TemporaryDirectory() as directory:
   store=Store(Path(directory)/'t.db');calls=[]
   class TestMonitor:
    def __init__(self):self.store=store;self.source=SimpleNamespace(status=lambda:{'verified':False})
    def state(self):return {'config':store.config()}
    def settings(self,v):return store.save_config(v,CAT,STORES)
    async def start(self):calls.append(1);return {'run_id':'test','status':'RUNNING'}
   app=FastAPI();app.include_router(create_router(TestMonitor()))
   @app.exception_handler(ValueError)
   async def fail(req,exc):return JSONResponse({'error':str(exc)},400)
   client=TestClient(app)
   self.assertFalse(client.get('/api/aov-monitor/state').json()['config']['enabled'])
   client.get('/api/aov-monitor/history');client.get('/api/aov-monitor/mappings');self.assertFalse(calls)
   self.assertEqual(client.get('/api/aov-monitor/history/missing').status_code,404)
   r=client.post('/api/aov-monitor/config',json=dict(revision=1,enabled=False,shops=[SHOP]));self.assertEqual(r.status_code,200)
   self.assertEqual(client.post('/api/aov-monitor/detect',json={}).status_code,202);self.assertEqual(calls,[1])

class NullableSourceTests(unittest.TestCase):
 def test_missing_tms_fields_can_be_filled_without_relaxing_existing_ranking(self):
  from order_pulse.tms_orders import _order
  from order_pulse.exports.model import ExportError
  scope=dict(start=(AT-timedelta(days=1)).isoformat(),end=AT.isoformat(),shop_ids=['1'],account=dict(customer_id='2',supplier_id='3'))
  raw=dict(id='r',platformOrderId='001',shopId='1',platformId=104,companyId='2',supplierId='3',shopName='店铺',orderTime=None,orderFee=None,itemList=[])
  with self.assertRaises(ExportError):_order(raw,'current',scope)
  self.assertIsNone(_order(raw,'current',dict(scope,aov=True))['order_amount_rmb'])
  normalized=normalize_tms(raw);out,_,errors=reconcile(pair([normalized],[order(source='miaoshou')]),SHOP)
  self.assertFalse(errors);self.assertEqual(out[0]['amount_rmb'],'10');self.assertEqual(out[0]['skus'],['A'])
  raw.update(orderTime='',orderFee='',platformOrderStatus='')
  self.assertIsNone(_order(raw,'current',dict(scope,aov=True))['order_amount_rmb'])
  self.assertIsNone(normalize_tms(raw)['paid_at'])

if __name__=='__main__':unittest.main()
