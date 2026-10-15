import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from order_pulse.aov_monitor.cards import *
from order_pulse.aov_monitor.card_store import CardStore,collection_scope
from order_pulse.aov_monitor.card_service import run_cards,split_job
from order_pulse.aov_monitor.domain import reconcile,calculate,transition,instant
from order_pulse.aov_monitor.service import Monitor

AT=datetime(2026,10,13,8,tzinfo=timezone.utc)
CAT={'f':dict(product_name='产品',member_skus=['A','B']),'g':dict(product_name='另一产品',member_skus=['C'])}
TMS=[dict(id='1',name='TK-彪-巴西本土11店-侨巴2059'),dict(id='2',name='TK-彪-巴西52店-2084-乔巴-舒')]
MS=[dict(id='14989003',name='TK-彪-巴西本土11店',platform_shop_id='7494604067083617492',status='normal',auth_status='valid'),dict(id='17315641',name=TMS[1]['name'],status='normal',auth_status='valid')]
STORES=[dict(shop_id=s['id'],name=s['name'],platform_id=104) for s in TMS]
def row(pid,skus=['A'],hour=1):return dict(order_key=pid,platform_order_id=pid,paid_at=(AT-timedelta(hours=hour)).isoformat(),amount_rmb='10',refund_status='NONE',skus=skus)

class CardDomainTests(unittest.TestCase):
 def test_actual_internal_id_and_verified_suffix(self):
  m=match_shops(TMS,MS);self.assertEqual(m['1']['miaoshou_shop_id'],'14989003');self.assertEqual(m['1']['method'],'NORMALIZED_NAME');self.assertEqual(m['2']['method'],'EXACT_NAME')
 def test_ambiguity_and_identity_conflict(self):
  self.assertEqual(match_shops(TMS,MS+[dict(MS[0],id='duplicate')])['1']['status'],'CONFLICT')
  for name in ['TK-彪-巴西11店','TK-彪-墨西哥本土11店']:
   self.assertNotEqual(match_shops(TMS,[dict(MS[0],name=name)],{'1':'14989003'})['1']['status'],'MATCHED')
  self.assertNotEqual(match_shops([dict(TMS[0],platform_shop_id='other')],MS)['1']['status'],'MATCHED')
 def test_normalization_preserves_unknown_suffix(self):
  self.assertEqual(normalized_name(TMS[0]['name']),normalized_name(MS[0]['name']))
  self.assertNotEqual(normalized_name('TK-彪-巴西本土11店-未知人员'),normalized_name(MS[0]['name']))
 def test_mixed_orders_unique_shop_total_actual_skus_unknown(self):
  s=blank_shop(TMS[0]);rows=[row('1',['A','C']),row('2',['B']),row('3',['UNKNOWN'])]
  p=discover_products(rows,CAT,s);self.assertEqual(sorted(next(x for x in p if x['family_id']=='f')['observed_skus']),['A','B'])
  self.assertEqual(sum(x['unknown'] for x in p),1)
  self.assertEqual(shop_windows(rows,AT.isoformat(),s,None)['24h']['current_count'],3)
  self.assertEqual(sum(calculate(rows,AT.isoformat(),x['skus'],x['rules'])['24h']['current_count'] for x in p),4)
 def test_new_product_inherits_default_override_preserved(self):
  s=blank_shop(TMS[0]);s['default_rules']['24h']['upper']='9';s['products']=[dict(family_id='g',override=True,rules={w:rule({'upper':'20'}) for w in WINDOWS},display_name='别名')]
  p={x['family_id']:x for x in discover_products([row('1',['A','C'])],CAT,s)}
  self.assertEqual(p['f']['rules']['24h']['upper'],'9');self.assertEqual(p['g']['rules']['24h']['upper'],'20');self.assertEqual(p['g']['display_name'],'别名')
  self.assertEqual(alert_fingerprint(s,p['f']),alert_fingerprint(dict(s,hidden=True,display_name='new'),dict(p['f'],display_name='new')))
 def test_bounded_slice_exact_window_coverage(self):
  j=dict(id='job',start=(AT-timedelta(days=14)).isoformat(),end=AT.isoformat())
  a,b=split_job(j);self.assertEqual(a['end'],b['start']);self.assertEqual(a['start'],j['start']);self.assertEqual(b['end'],j['end'])
  self.assertFalse(split_job(dict(j,depth=8)))


 def test_observed_sku_ageing_does_not_change_group_rule_identity(self):
  s=blank_shop(TMS[0]);a=discover_products([row('1',['A','B'])],CAT,s)[0];b=discover_products([row('1',['A'])],CAT,s)[0]
  self.assertEqual(a['skus'],b['skus']);self.assertNotEqual(a['observed_skus'],b['observed_skus']);self.assertEqual(alert_fingerprint(s,a),alert_fingerprint(s,b))

 def test_missing_payment_and_refund_are_visible_not_assumed_paid(self):
  shop=dict(blank_shop(TMS[0]),miaoshou_shop_id='14989003')
  value=dict(platform_order_id='001_1',shop_id='1',paid_at=AT.isoformat(),money={'value':'0','currency':'CNY','basis':'ORDER_TOTAL'},skus=['A'],payment_status=None,refund_status=None)
  rows,notes,errors=reconcile({'tms':{'coverage':'COMPLETE','orders':[value]},'miaoshou':{'coverage':'COMPLETE','orders':[]}},shop)
  self.assertEqual(rows,[]);self.assertEqual(errors,['ORDER_FIELDS_UNVERIFIED']);self.assertEqual(notes['invalid_orders'][0]['issues'],['付款状态缺失','退款状态缺失'])

class CardPersistenceTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db';self.store=CardStore(self.path)
 def tearDown(self):self.tmp.cleanup()
 def prepare(self):self.store.prepare(STORES);return self.store.apply_directory(dict(tms=TMS,miaoshou=MS,target={}))
 def test_legacy_config_overrides_history_and_correct_id(self):
  old=dict(revision=1,enabled=True,shops=[dict(blank_shop(TMS[0]),miaoshou_shop_id='7494604067083617492',products=[dict(family_id='f',name='产品',skus=['A','B'],rules={w:rule({'upper':'99'}) for w in WINDOWS})])])
  with self.store.db() as db:db.execute('UPDATE aov_monitor_settings SET payload=? WHERE id=1',(__import__('json').dumps(old),))
  rid=self.store.start(None,{});proof=dict(status='SUCCEEDED',groups=[{'historic':'kept'}]);self.store.finish(rid,proof)
  c=self.prepare();self.assertEqual(c['shops'][0]['miaoshou_shop_id'],'14989003');self.assertTrue(c['shops'][0]['products'][0]['override']);self.assertFalse(c['shops'][1]['push']);self.assertEqual(self.store.snapshot(rid),proof)
 def test_hidden_persistence_and_no_counter_reset(self):
  c=self.prepare();rid=self.store.start(None,{});self.store.finish(rid,dict(status='SUCCEEDED'),{'1:f:24h':dict(active='HIGH',streak=2)})
  before=digest(collection_scope(c));c['shops'][0].update(hidden=True,display_name='测试')
  self.store.save_cards(c,CAT,STORES);self.assertTrue(CardStore(self.path).config()['shops'][0]['hidden']);self.assertEqual(self.store.alert_states()['1:f:24h']['streak'],2);self.assertEqual(before,digest(collection_scope(self.store.config())))
 def test_job_resume_preserves_complete_and_frozen_cutoff(self):
  rid=self.store.start('slot',dict(card_run=True,cutoff=AT.isoformat()));self.store.add_jobs(rid,[dict(id='a'),dict(id='b')]);self.store.job_done(rid,'a',{'orders':[row('1')]});self.store.recover()
  again=CardStore(self.path);self.assertEqual(again.pending()[0],rid);self.assertEqual([j['status'] for j in again.jobs(rid)],['COMPLETE','QUEUED']);self.assertEqual(again.pending()[1]['cutoff'],AT.isoformat());self.assertIsNone(again.start('slot',{}))
 def test_invalid_manual_id_and_conflicting_choice(self):
  c=self.prepare();c['shops'][0]['mapping_choice']='not-real'
  with self.assertRaises(ValueError):self.store.save_cards(c,CAT,STORES)


 def test_cached_binding_does_not_remove_new_real_directory_shops(self):
  self.prepare();new=dict(id='3',name='TK-巴西90店')
  self.store.apply_directory(dict(tms=TMS+[new],miaoshou=MS,target={}))
  self.assertEqual(len(self.store.prepare(STORES)['shops']),3)
  self.assertEqual(self.store.prepare(STORES)['shops'][-1]['id'],'3')


 def test_rule_change_and_back_resets_only_affected_shop(self):
  c=self.prepare();rid=self.store.start(None,{})
  self.store.finish(rid,dict(status='SUCCEEDED'),{'1:f:24h':dict(streak=1),'2:f:24h':dict(streak=1)})
  c['shops'][0]['default_rules']['24h']['upper']='20';c=self.store.save_cards(c,CAT,STORES)
  self.assertNotIn('1:f:24h',self.store.alert_states());self.assertEqual(self.store.alert_states()['2:f:24h']['streak'],1)
  c['shops'][0]['default_rules']['24h']['upper']=None;self.store.save_cards(c,CAT,STORES)
  self.assertNotIn('1:f:24h',self.store.alert_states())

class CardServiceTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db'
  self.host=SimpleNamespace(repo=SimpleNamespace(path=self.path,active_catalog=lambda:{'families':CAT},get=lambda k:{'stores':STORES} if k=='binding' else None),secrets=SimpleNamespace(configured=lambda:False,get=lambda:{}),lock=asyncio.Lock())
  legacy=CardStore(self.path)
  with legacy.db() as db:db.execute("UPDATE aov_monitor_settings SET payload=? WHERE id=1",(__import__('json').dumps(dict(revision=1,enabled=False,shops=[],collection_mode='ROLLING')),))
 async def asyncTearDown(self):self.tmp.cleanup()
 def source(self,fail=False,count=3):
  class Source:
   real_verified=True
   def status(self):return {'verified':True}
   def directory(self):return dict(tms=TMS,miaoshou=MS,target={})
   def batch(self,job,shops,target):
    if fail and job['kind']=='miaoshou':raise ValueError('MIAOSHOU_SOURCE_FAILED')
    if job['kind']=='refunds':return dict(refunds=[])
    if job['kind']=='tms':return dict(orders=[dict(platform_order_id=str(i),shop_id=s['id'],paid_at=(AT-timedelta(hours=1)).isoformat(),money=dict(value='10',currency='CNY',basis='ORDER_TOTAL'),payment_status='PAID',refund_status=None,skus=['A','unknown']) for s in shops for i in range(count)])
    return dict(rows=[dict(platform_order_id=str(i),shop_id=s['miaoshou_shop_id'],platform='tiktok',paid_at=(AT-timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'),status='ON_HOLD',skus=['A','unknown'],order_amount='20',exchange_rate='2',cny_total='10',pay_amount='20',currency='BRL') for s in shops for i in range(count)])
  return Source()
 async def test_full_collection_includes_hidden_actual_groups(self):
  m=Monitor(self.host,source=self.source(),rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:AT)
  c=m.store.prepare(STORES);c['shops'][0]['hidden']=True;m.store.save_cards(c,CAT,STORES)
  await m.start();await m.worker;s=m.store.snapshot();self.assertEqual(s['status'],'SUCCEEDED');self.assertEqual(len(s['shop_cards']),2);self.assertEqual(len(s['groups']),4);self.assertEqual(s['shop_cards'][0]['windows']['24h']['current_count'],3);self.assertFalse(m.store.events())
 async def test_incomplete_retains_result_and_no_recovery(self):
  m=Monitor(self.host,source=self.source(),rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:AT)
  await m.start();await m.worker;before=m.store.snapshot()['shop_cards'][0]['windows'];m.source=self.source(True)
  await m.start();await m.worker;s=m.store.snapshot();self.assertEqual(s['status'],'INCOMPLETE');self.assertEqual(s['shop_cards'][0]['windows'],before);self.assertFalse(m.store.events());self.assertEqual(s['shop_cards'][0]['errors'],['MIAOSHOU_SOURCE_FAILED'])

 async def test_disabled_push_never_sends_false_recovery(self):
  source=self.source(count=10);original=source.batch
  def batch(job,shops,target):
   result=original(job,shops,target)
   field='orders' if job['kind']=='tms' else 'rows' if job['kind']=='miaoshou' else None
   if not field:return result
   out=[]
   for o in result[field]:
    a=deepcopy(o);b=deepcopy(o);b['platform_order_id']+='-prior'
    if field=='orders':
     a['money']['value']=batch.amount;b['paid_at']=(AT-timedelta(hours=25)).isoformat()
    else:
     a.update(cny_total=batch.amount,order_amount=str(int(batch.amount)*2),paid_at=(AT+timedelta(hours=7)).strftime('%Y-%m-%d %H:%M:%S'));b['paid_at']=(AT-timedelta(hours=17)).strftime('%Y-%m-%d %H:%M:%S')
    out.extend([a,b])
   result[field]=out;return result
  batch.amount='20';source.batch=batch
  clock=[AT];m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:clock[0])
  c=m.store.prepare(STORES);c['enabled']=True;c['shops'][1]['push']=True
  for shop in c['shops']:shop['default_rules']['24h']=rule({'upper':'15','min_orders':10})
  m.store.save_cards(c,CAT,STORES);sent=[]
  async def send(event):sent.append(event)
  m.send=send
  await m.start(True);await m.worker;self.assertFalse(sent)
  clock[0]+=timedelta(hours=2);await m.start(True);await m.worker
  self.assertTrue(sent);self.assertTrue(all(e['group'].startswith('2:') for e in sent))
  self.assertFalse(any(k.startswith('1:') for k in m.store.alert_states()))
  batch.amount='10';clock[0]+=timedelta(hours=2);await m.start(True);await m.worker
  recoveries=[e for e in sent if any(c['kind']=='RECOVERY' for c in e['changes'])];self.assertTrue(recoveries);self.assertTrue(all(e['group'].startswith('2:') for e in recoveries))

 async def test_legacy_state_contract_remains_read_only(self):
  source=SimpleNamespace(status=lambda:{'verified':False},collect=lambda *a:{})
  monitor=Monitor(self.host,source=source)
  state=monitor.state();self.assertIn('catalog',state);self.assertIn('automation',state);self.assertNotIn('shop_cards',state);self.assertEqual(state['config']['shops'],[])


class ShopSourcePolicyTests(unittest.TestCase):
 def test_absence_requires_complete_directory_and_conflicts_do_not_fallback(self):
  absent=dict(id='3',name='TK-巴西90店')
  d=dict(tms=TMS+[absent],miaoshou=MS,status='COMPLETE')
  self.assertEqual(collection_mapping(d)['3']['status'],'TMS_ONLY')
  self.assertEqual(collection_mapping(dict(d,status='FAILED'))['3']['status'],'UNMATCHED')
  self.assertEqual(collection_mapping(d,{'3':'missing'})['3']['status'],'CONFLICT')
  self.assertEqual(collection_mapping(dict(d,miaoshou=MS+[dict(MS[0],id='dup')]))['1']['status'],'CONFLICT')
  revoked=dict(MS[0],auth_status='expired')
  self.assertEqual(collection_mapping(dict(d,miaoshou=[revoked]))['1']['status'],'UNAVAILABLE')
 def test_single_source_retains_unknown_refund_and_zero_without_faking_fields(self):
  s=blank_shop(TMS[0]);r=dict(platform_order_id='001',shop_id='1',paid_at=AT.isoformat(),money=dict(value='0',currency='CNY',basis='ORDER_TOTAL'),payment_status='PAID',refund_status=None,skus=['A'])
  source={'tms':dict(coverage='COMPLETE',orders=[r,r]),'miaoshou':dict(coverage='FAILED',error='OFFLINE')}
  rows,notes,errors=reconcile(source,s,tms_only=True)
  self.assertEqual(errors,[]);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['amount_rmb'],'0');self.assertEqual(rows[0]['refund_status'],'UNKNOWN');self.assertEqual(notes['refund_unverified'],1);self.assertEqual(notes['coverage']['miaoshou'],'NOT_APPLICABLE')
  source['tms']['orders']=[dict(r,payment_status=None)]
  self.assertEqual(reconcile(source,s,tms_only=True)[2],['ORDER_FIELDS_UNVERIFIED'])
  source['tms']['orders']=[dict(r,refund_status='FULL')]
  self.assertEqual(reconcile(source,s,tms_only=True)[0],[])
  source['tms']['coverage']='INCOMPLETE'
  self.assertTrue(reconcile(source,s,tms_only=True)[2])
 def test_ten_order_floor_current_and_previous_and_recovery_suppressed(self):
  for current,prior in [(9,10),(10,9),(10,10)]:
   rows=[row('c'+str(i),hour=1) for i in range(current)]+[row('p'+str(i),hour=25) for i in range(prior)]
   metric=calculate(rows,AT.isoformat(),['A'],{'24h':{'upper':'9','min_orders':1}})['24h']
   self.assertEqual(metric['rule']['min_orders'],10)
   self.assertEqual(metric['status'],'ABNORMAL' if current==prior==10 else 'INSUFFICIENT')
   if current<10 or prior<10:
    before=dict(revision='r',active='HIGH',last_slot=(AT-timedelta(hours=2)).isoformat(),streak=2)
    after,event=transition(before,metric,AT.isoformat(),True,'r');self.assertIsNone(event);self.assertEqual(after['active'],'HIGH')

class SkuVisibilityPersistenceTests(unittest.TestCase):
 setUp=CardPersistenceTests.setUp
 tearDown=CardPersistenceTests.tearDown
 prepare=CardPersistenceTests.prepare
 def test_hidden_skus_persist_shop_locally_without_changing_stats_history_or_counters(self):
  c=self.prepare();c['collection_mode']='ROLLING';c=self.store.save_cards(c,CAT,STORES);rid=self.store.start(None,{})
  products=discover_products([row('1',['A','B'])],CAT,c['shops'][0]);card=dict(id='1',status='COMPLETE',products=products,windows={'24h':{'current_count':1}})
  snapshot=dict(status='SUCCEEDED',shop_cards=[card]);self.store.finish(rid,snapshot,{'1:f:24h':dict(streak=1,active='HIGH')})
  scope=collection_scope(c);fingerprint=alert_fingerprint(c['shops'][0],products[0]);c['shops'][0]['hidden_skus']=['A','B']
  saved=self.store.save_cards(c,CAT,STORES)
  self.assertEqual(saved['shops'][0]['hidden_skus'],['A','B']);self.assertEqual(saved['shops'][1]['hidden_skus'],[])
  self.assertEqual(CardStore(self.path).config(),saved);self.assertEqual(collection_scope(saved),scope);self.assertEqual(alert_fingerprint(saved['shops'][0],products[0]),fingerprint)
  self.assertEqual(self.store.alert_states()['1:f:24h']['streak'],1);self.assertEqual(self.store.snapshot(rid),snapshot)
  visible=visible_cards(saved,snapshot,self.store.directory()['mapping'])[0]
  self.assertEqual(visible['windows'],card['windows']);self.assertEqual(visible['products'],products)
  # Once an SKU ages out, it stays restorable without a new collection.
  rid2=self.store.start(None,{});self.store.finish(rid2,dict(status='SUCCEEDED',shop_cards=[]));self.store.save_cards(saved,CAT,STORES)
 def test_unobserved_other_shop_sku_rejected(self):
  c=self.prepare();c['shops'][0]['hidden_skus']=['A']
  with self.assertRaisesRegex(ValueError,'实采订单'):self.store.save_cards(c,CAT,STORES)

class SingleSourceServiceTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=CardServiceTests.asyncSetUp
 asyncTearDown=CardServiceTests.asyncTearDown
 source=CardServiceTests.source
 async def test_miaoshou_failure_does_not_block_absent_shop_or_enqueue_its_refunds(self):
  source=self.source(True);original=source.directory;absent=dict(id='3',name='TK-巴西90店')
  source.directory=lambda:dict(original(),tms=TMS+[absent])
  m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:AT)
  await m.start();await m.worker;s=m.store.snapshot();cards={c['id']:c for c in s['shop_cards']}
  self.assertEqual(s['status'],'PARTIAL');self.assertEqual(cards['1']['status'],'INCOMPLETE');self.assertEqual(cards['3']['status'],'COMPLETE');self.assertEqual(cards['3']['mapping']['status'],'TMS_ONLY');self.assertEqual(cards['3']['windows']['24h']['current_count'],3);self.assertFalse(cards['3']['alert_eligible']);self.assertFalse(any(j.get('shop_id')=='3' for j in m.store.jobs(s['run_id'])))
 async def test_single_shop_only_enqueues_tms_and_unknown_refund_suppresses_notifications(self):
  source=self.source(count=10);source.directory=lambda:dict(tms=TMS,miaoshou=[],target={})
  m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:AT)
  m.store.apply_directory(source.directory());c=m.store.config();c['enabled']=True;c['shops'][0]['push']=True;c['shops'][0]['default_rules']['24h']=rule({'upper':'9'})
  m.store.save_cards(c,CAT,STORES)
  await m.start(True);await m.worker
  s=m.store.snapshot();self.assertEqual(s['status'],'SUCCEEDED');self.assertEqual([j['kind'] for j in m.store.jobs(s['run_id'])],['tms']*3);self.assertEqual(s['shop_cards'][0]['windows']['24h']['current_count'],10);self.assertFalse(m.store.events());self.assertFalse(m.store.alert_states())

class BrowserFailureQueueTests(unittest.TestCase):
 def test_binding_error_is_not_a_density_split(self):
  from order_pulse.aov_monitor.card_service import batch_error,SPLIT_ERRORS
  code,detail=batch_error({'error':'FAST_TRANSPORT_FAILED','diagnostic':{'stage':'binding'}})
  self.assertEqual(code,'TMS_BROWSER_NOT_READY');self.assertNotIn(code,SPLIT_ERRORS);self.assertNotIn('FAST_HTTP_FAILED',SPLIT_ERRORS)
  self.assertIn(batch_error({'error':'FAST_TIMEOUT','diagnostic':{'stage':'current'}})[0],SPLIT_ERRORS)
 def test_retry_coalesces_unread_ranges_preserves_completed_holes_and_receipts(self):
  from order_pulse.aov_monitor.card_service import retry_tms_jobs
  a=AT-timedelta(hours=4)
  jobs=[dict(id='parent',kind='tms',start=a.isoformat(),end=AT.isoformat(),status='SPLIT')]
  for i,status in enumerate(['FAILED','QUEUED','COMPLETE','FAILED']):
   jobs.append(dict(id=str(i),kind='tms',start=(a+timedelta(hours=i)).isoformat(),end=(a+timedelta(hours=i+1)).isoformat(),status=status,result={'orders':[]} if status=='COMPLETE' else None))
  result=retry_tms_jobs(jobs);done=[j for j in result if j['status']=='COMPLETE'];pending=[j for j in result if j['status']=='QUEUED']
  self.assertEqual(done,[jobs[3]]);self.assertEqual([(j['start'],j['end']) for j in pending],[(a.isoformat(),(a+timedelta(hours=2)).isoformat()),((a+timedelta(hours=3)).isoformat(),AT.isoformat())]);self.assertFalse(any(j['status']=='SPLIT' for j in result))

class BrowserFailureServiceTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=CardServiceTests.asyncSetUp
 asyncTearDown=CardServiceTests.asyncTearDown
 source=CardServiceTests.source
 async def test_binding_failure_ends_once_keeps_previous_results_and_does_not_split(self):
  source=self.source();m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:AT)
  await m.start();await m.worker;before=m.store.snapshot();original=source.batch;calls=[]
  def batch(job,shops,target):
   calls.append(job['kind'])
   if job['kind']=='tms':
    exc=ValueError('FAST_TRANSPORT_FAILED');exc.details={'stage':'binding'};raise exc
   return original(job,shops,target)
  source.batch=batch;await m.start();await m.worker;after=m.store.snapshot()
  self.assertEqual(calls,['tms']);self.assertEqual(after['error_code'],'TMS_BROWSER_NOT_READY');self.assertIsNone(m.store.pending());self.assertFalse(m.store.events())
  self.assertEqual(after['shop_cards'][0]['windows'],before['shop_cards'][0]['windows']);self.assertFalse(any(j['status']=='SPLIT' for j in m.store.jobs(after['run_id'])))
  source.batch=original;await m.start(retry_id=after['run_id']);await m.worker
  self.assertEqual(m.store.snapshot()['status'],'SUCCEEDED');self.assertNotIn('error_code',m.store.snapshot())

class InitialTmsRangeTests(unittest.TestCase):
 def test_two_day_shared_slices_cover_fourteen_days_exactly(self):
  from order_pulse.aov_monitor.card_service import tms_ranges
  start=(AT-timedelta(days=14)).isoformat();end=AT.isoformat();parts=list(tms_ranges(start,end));self.assertEqual(len(parts),7);self.assertEqual(parts[0][0],start);self.assertEqual(parts[-1][1],end)
  for i,(a,b) in enumerate(parts):
   self.assertLessEqual(instant(b)-instant(a),timedelta(days=2))
   if i:self.assertEqual(parts[i-1][1],a)

class TimeoutResetTests(unittest.TestCase):
 def test_timeout_reloads_owned_page_before_propagating_for_split(self):
  from threading import Lock
  from unittest.mock import patch,Mock
  from order_pulse.aov_monitor.batch_sources import BatchSource
  from order_pulse.exports.model import ExportError
  host=SimpleNamespace(repo=SimpleNamespace(get=lambda k:{'account':{}}),browser_space=SimpleNamespace(get=lambda:{},lock=Lock()))
  source=BatchSource(host);source.resolve=lambda *_:dict(space_id=0,page_label='p3');source._directory={'tms':TMS};source._node=Mock(return_value={'reset':True})
  job=dict(kind='tms',start=(AT-timedelta(days=2)).isoformat(),end=AT.isoformat())
  with patch('order_pulse.aov_monitor.batch_sources.EgoOrderTransport') as transport:
   transport.return_value.collect.side_effect=ExportError('FAST_TIMEOUT')
   with self.assertRaisesRegex(ExportError,'FAST_TIMEOUT'):source.batch(job,[blank_shop(s) for s in TMS],{})
  self.assertEqual(source._node.call_count,1);self.assertIn('await p.reload()',source._node.call_args.args[0])
  source._node.side_effect=ValueError('SOURCE_TRANSPORT_FAILED')
  with patch('order_pulse.aov_monitor.batch_sources.EgoOrderTransport') as transport:
   transport.return_value.collect.side_effect=ExportError('FAST_TIMEOUT')
   with self.assertRaisesRegex(ValueError,'TMS_BROWSER_NOT_READY'):source.batch(job,[blank_shop(s) for s in TMS],{})

class ShortWindowTests(unittest.TestCase):
 def test_only_24_and_72_hours_and_complete_previous_72_hour_window(self):
  from order_pulse.aov_monitor.domain import LOOKBACK_HOURS,ruleset
  self.assertEqual(WINDOWS,{'24h':24,'72h':72});self.assertEqual(LOOKBACK_HOURS,144)
  rows=[row('a',hour=1),row('b',hour=72),row('c',hour=144),row('d',hour=145)]
  result=calculate(rows,AT.isoformat(),['A'],{})
  self.assertEqual(set(result),{'24h','72h'});self.assertEqual(result['72h']['current_count'],2);self.assertEqual(result['72h']['previous_count'],1)
  saved=ruleset({'24h':{'upper':'12'},'48h':{'upper':'1'},'7d':{'upper':'1'}})
  self.assertEqual(saved['48h']['upper'],'1');self.assertNotIn('48h',calculate(rows,AT.isoformat(),['A'],saved))

class NarrowQueueTests(unittest.TestCase):
 setUp=CardPersistenceTests.setUp
 tearDown=CardPersistenceTests.tearDown
 def test_running_fourteen_day_queue_narrows_without_losing_receipts_or_history(self):
  self.store.prepare(STORES)
  historic=self.store.start(None,{})
  snapshot={'status':'SUCCEEDED','groups':[{'windows':{'48h':{'aov_rmb':'20'},'7d':{'aov_rmb':'30'}}}]}
  self.store.finish(historic,snapshot)
  context=dict(card_run=True,start=(AT-timedelta(days=14)).isoformat(),cutoff=AT.isoformat(),phase='ORDERS')
  rid=self.store.start(None,context)
  jobs=[dict(id='old',kind='tms',start=(AT-timedelta(days=14)).isoformat(),end=(AT-timedelta(days=12)).isoformat()),dict(id='cross',kind='tms',start=(AT-timedelta(days=7)).isoformat(),end=(AT-timedelta(days=5)).isoformat()),dict(id='keep',kind='tms',start=(AT-timedelta(days=4)).isoformat(),end=(AT-timedelta(days=2)).isoformat()),dict(id='tail',kind='tms',start=(AT-timedelta(days=2)).isoformat(),end=AT.isoformat()),dict(id='ms',kind='miaoshou',start=context['start'],end=context['cutoff'])]
  self.store.add_jobs(rid,jobs)
  receipt={'orders':[row('kept')]}
  for jid in ('old','cross','keep'):self.store.job_done(rid,jid,receipt)
  c=self.store.narrow_run(rid,context);self.assertEqual(instant(c['cutoff'])-instant(c['start']),timedelta(days=6));self.assertEqual(c['active_windows'],['24h','72h'])
  data={j['id']:j for j in self.store.jobs(rid)}
  self.assertEqual(data['old']['status'],'ARCHIVED');self.assertEqual(data['old']['result'],receipt);self.assertEqual(data['cross']['status'],'ARCHIVED');self.assertEqual(data['cross-six-days']['status'],'QUEUED');self.assertEqual(data['cross-six-days']['start'],c['start'])
  self.assertEqual(data['keep']['status'],'COMPLETE');self.assertEqual(data['keep']['result'],receipt);self.assertEqual(data['ms-six-days']['start'],c['start'])
  self.assertEqual(self.store.snapshot(historic),snapshot);self.assertEqual(self.store.progress()['completed'],1);self.assertEqual(self.store.progress()['total'],4)
  self.store.narrow_run(rid,c);self.assertEqual(len(self.store.jobs(rid)),len(data),'restart migration is idempotent')

class ShortWindowServiceTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=CardServiceTests.asyncSetUp
 asyncTearDown=CardServiceTests.asyncTearDown
 source=CardServiceTests.source
 async def test_both_sources_use_six_days_and_results_have_only_two_windows(self):
  source=self.source();batch=source.batch;requests=[]
  def record(job,shops,target):
   if job['kind']!='refunds':requests.append((job['kind'],job['start'],job['end']))
   return batch(job,shops,target)
  source.batch=record
  m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:AT)
  await m.start();await m.worker;s=m.store.snapshot();self.assertEqual(s['status'],'SUCCEEDED');self.assertEqual(instant(s['cutoff'])-instant(s['start']),timedelta(days=6))
  self.assertEqual(s['active_windows'],['24h','72h']);self.assertEqual([r[0] for r in requests],['tms']*3+['miaoshou']*3)
  self.assertTrue(all(instant(b)-instant(a)<=timedelta(days=2) for k,a,b in requests if k=='tms'))
  tms=[r for r in requests if r[0]=='tms'];self.assertEqual(tms[0][1],s['start']);self.assertEqual(tms[-1][2],s['cutoff'])
  self.assertTrue(all(tms[i][2]==tms[i+1][1] for i in range(len(tms)-1)))
  self.assertTrue(all(set(g['windows'])=={'24h','72h'} for g in s['groups']));self.assertFalse(m.store.events())

class InactiveWindowAlertTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=CardServiceTests.asyncSetUp
 asyncTearDown=CardServiceTests.asyncTearDown
 source=CardServiceTests.source
 async def test_old_48_hour_threshold_never_creates_alert_or_recovery(self):
  source=self.source(count=10);base=source.batch
  def batch(job,shops,target):
   result=base(job,shops,target)
   field='orders' if job['kind']=='tms' else 'rows' if job['kind']=='miaoshou' else None
   if not field:return result
   out=[]
   for original in result[field]:
    current=deepcopy(original);prior=deepcopy(original);prior['platform_order_id']+='-previous'
    if field=='orders':current['money']['value']='20';prior['paid_at']=(AT-timedelta(hours=49)).isoformat()
    else:current.update(order_amount='40',cny_total='20',pay_amount='40');prior['paid_at']=(AT-timedelta(hours=41)).strftime('%Y-%m-%d %H:%M:%S')
    out.extend([current,prior])
   result[field]=out;return result
  source.batch=batch;clock=[AT];m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:clock[0])
  c=m.store.prepare(STORES);c['enabled']=True;c['shops'][0]['push']=True;c['shops'][0]['default_rules']['48h']=rule({'upper':'15'})
  m.store.save_cards(c,CAT,STORES)
  for _ in range(2):await m.start(True);await m.worker;clock[0]+=timedelta(hours=2)
  self.assertEqual(m.store.snapshot()['status'],'SUCCEEDED');self.assertFalse(m.store.events());self.assertTrue(all(k.rsplit(':',1)[-1] in ('24h','72h') for k in m.store.alert_states()))
  self.assertEqual(m.store.config()['shops'][0]['default_rules']['48h']['upper'],'15','inactive saved settings retained')

class ShortRetryTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=CardServiceTests.asyncSetUp
 asyncTearDown=CardServiceTests.asyncTearDown
 source=CardServiceTests.source
 async def test_only_unstable_slice_is_retried_successful_sources_are_reused(self):
  source=self.source();base=source.batch;calls=[];fail=[True]
  def batch(job,shops,target):
   calls.append(job['kind'])
   if fail[0] and job['kind']=='tms':raise ValueError('FAST_SOURCE_UNSTABLE')
   return base(job,shops,target)
  source.batch=batch;m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:AT)
  await m.start();await m.worker;old=m.store.snapshot();self.assertGreater(old['progress']['failed'],0)
  before={j['id']:j['result'] for j in m.store.jobs(old['run_id']) if j['status']=='COMPLETE'}
  calls.clear();fail[0]=False;await m.start(retry_id=old['run_id']);await m.worker;new=m.store.snapshot()
  self.assertTrue(calls and all(x=='tms' for x in calls));self.assertEqual(new['status'],'SUCCEEDED');self.assertEqual(new['lookback_hours'],144);self.assertEqual(new['cutoff'],old['cutoff'])
  after={j['id']:j['result'] for j in m.store.jobs(new['run_id']) if j['status']=='COMPLETE'}
  self.assertTrue(all(after[k]==v for k,v in before.items()));self.assertFalse(m.store.events())


class ScopedBatchRecoveryTests(unittest.TestCase):
 def test_coverage_requires_full_time_span_for_this_shop_and_ignores_other_failures(self):
  from order_pulse.aov_monitor.card_service import tms_coverage
  a=(AT-timedelta(days=6)).isoformat();mid=(AT-timedelta(days=3)).isoformat();b=AT.isoformat()
  jobs=[dict(id='a',kind='tms',start=a,end=mid,shop_ids=['1'],status='COMPLETE'),dict(id='b',kind='tms',start=mid,end=b,shop_ids=['1'],status='COMPLETE'),dict(id='c',kind='tms',start=a,end=b,shop_ids=['2'],status='FAILED',result={'error':'FAST_SOURCE_UNSTABLE'})]
  self.assertEqual(tms_coverage(jobs,'1',a,b),[]);self.assertEqual(tms_coverage(jobs,'2',a,b),['FAST_SOURCE_UNSTABLE'])
  self.assertEqual(tms_coverage(jobs[:1],'1',a,b),['TMS_COVERAGE_UNVERIFIED'])
 def test_retry_keeps_shop_scope_archives_and_completed_receipts(self):
  from order_pulse.aov_monitor.card_service import retry_tms_jobs
  a=(AT-timedelta(days=2)).isoformat();b=AT.isoformat()
  jobs=[dict(id='done',kind='tms',start=a,end=b,shop_ids=['1'],status='COMPLETE',result={'orders':[]}),dict(id='unread',kind='tms',start=a,end=b,shop_ids=['2'],status='FAILED'),dict(id='archive',kind='tms',start=a,end=b,status='ARCHIVED',result={'proof':'kept'})]
  result=retry_tms_jobs(jobs,['1','2']);self.assertEqual(result[:2],[jobs[0],jobs[2]]);self.assertEqual(result[2]['shop_ids'],['2'])
  legacy=retry_tms_jobs([dict(jobs[1],shop_ids=[str(i) for i in range(80)])]);self.assertEqual(len(legacy),4);self.assertTrue(all(len(j['shop_ids'])==20 for j in legacy))
 def test_unstable_retry_is_bounded_and_keeps_dates(self):
  from order_pulse.aov_monitor.card_service import split_tms_job
  j=dict(id='x',kind='tms',shop_ids=['1','2','3','4'],start=(AT-timedelta(days=6)).isoformat(),end=AT.isoformat())
  children=split_tms_job(j,True);self.assertEqual(children[0]['start'],j['start']);self.assertEqual(children[1]['end'],j['end'])
  grandchildren=split_tms_job(children[0],True);self.assertEqual(len(grandchildren),2);self.assertFalse(split_tms_job(grandchildren[0],True))

class ScopedBatchServiceTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=CardServiceTests.asyncSetUp
 asyncTearDown=CardServiceTests.asyncTearDown
 source=CardServiceTests.source
 async def test_one_failed_shop_does_not_discard_another_shops_complete_orders(self):
  source=self.source();base=source.batch
  def batch(job,shops,target):
   if job['kind']=='tms' and any(s['id']=='2' for s in shops):raise ValueError('FAST_SOURCE_UNSTABLE')
   return base(job,shops,target)
  source.batch=batch;m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:AT)
  await m.start();await m.worker;s=m.store.snapshot();cards={c['id']:c for c in s['shop_cards']}
  self.assertEqual(s['status'],'PARTIAL');self.assertEqual(cards['1']['status'],'COMPLETE');self.assertEqual(cards['1']['windows']['24h']['current_count'],3)
  self.assertEqual(cards['2']['status'],'INCOMPLETE');self.assertFalse(cards['2']['windows']);self.assertFalse(m.store.events())


class TransientHttpTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=CardServiceTests.asyncSetUp
 asyncTearDown=CardServiceTests.asyncTearDown
 source=CardServiceTests.source
 async def test_two_transient_failures_recover_without_stopping_the_queue(self):
  from unittest.mock import patch,AsyncMock
  source=self.source();base=source.batch;attempts=[]
  def batch(job,shops,target):
   if job['kind']=='tms':
    attempts.append(job)
    if len(attempts)<=2:raise ValueError('FAST_HTTP_FAILED')
   return base(job,shops,target)
  source.batch=batch;m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:AT)
  with patch('order_pulse.aov_monitor.card_service.asyncio.sleep',new_callable=AsyncMock) as wait:
   await m.start();await m.worker
  self.assertEqual(len(attempts),5);self.assertEqual(m.store.snapshot()['status'],'SUCCEEDED')
  self.assertEqual([call.args[0] for call in wait.await_args_list if call.args[0]>=2],[2,4]);self.assertFalse(m.store.events())
 async def test_exhausted_retries_preserve_previous_results_and_continue_other_slices(self):
  from unittest.mock import patch,AsyncMock
  source=self.source();base=source.batch;m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:AT)
  await m.start();await m.worker;before=m.store.snapshot();first=before['start'];attempts=[]
  def batch(job,shops,target):
   if job['kind']=='tms':
    attempts.append(job)
    if job['start']==first:raise ValueError('FAST_HTTP_FAILED')
   return base(job,shops,target)
  source.batch=batch
  with patch('order_pulse.aov_monitor.card_service.asyncio.sleep',new_callable=AsyncMock):
   await m.start();await m.worker
  after=m.store.snapshot();self.assertEqual(after['status'],'INCOMPLETE');self.assertEqual(after['progress']['failed'],1)
  self.assertEqual(len(attempts),5);self.assertEqual(after['shop_cards'][0]['windows'],before['shop_cards'][0]['windows']);self.assertNotIn('error_code',after);self.assertFalse(m.store.events())
