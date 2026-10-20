"""Rolling windows reuse certified raw receipts; source failures stay visible."""
import asyncio,json,tempfile,unittest
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from order_pulse.aov_monitor.card_store import CardStore
from order_pulse.aov_monitor.cards import migrate_config,blank_shop
from order_pulse.aov_monitor.domain import instant,calculate
from order_pulse.aov_monitor.service import Monitor
from order_pulse.aov_monitor import incremental
from tests.test_aov_cards import AT,CAT,TMS,MS,STORES

class RollingIncrementalTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db';self.clock=[AT];self.calls=[];self.fail_kind=None;self.refund=False
  self.orders=[dict(pid=str(h+1000),paid=AT+timedelta(hours=h,minutes=30),amount='30' if h>=0 else '20' if h>=-24 else '10') for h in range(-145,8)]
  host=SimpleNamespace(repo=SimpleNamespace(path=self.path,active_catalog=lambda:{'families':CAT},get=lambda k:{'stores':STORES} if k=='binding' else None),secrets=SimpleNamespace(configured=lambda:False,get=lambda:{}),lock=asyncio.Lock())
  parent=self
  class Source:
   real_verified=True
   def status(self):return {'verified':True}
   def directory(self):return dict(tms=TMS,miaoshou=MS,target={})
   def batch(self,j,shops,target):
    parent.calls.append(deepcopy(j))
    if parent.fail_kind==j['kind']:raise ValueError('SOURCE_FAILED')
    if j['kind']=='refunds':return {'refunds':[dict(platform_order_id='999',shop_id=MS[0]['id'],kind='refund',status='success',platform_status='RETURN_OR_REFUND_REQUEST_COMPLETE',pay_amount='40',refund_amount='40',currency='BRL')] if parent.refund else []}
    rows=[r for r in parent.orders if instant(j['start'])<=r['paid']<instant(j['end'])]
    if j['kind']=='tms':return {'orders':[dict(platform_order_id=r['pid'],shop_id=s['id'],paid_at=r['paid'].isoformat(),money=dict(value=r['amount'],currency='CNY',basis='ORDER_TOTAL'),payment_status='PAID',refund_status=None,skus=['A','C']) for s in shops for r in rows]}
    return {'rows':[dict(platform_order_id=r['pid'],shop_id=s['miaoshou_shop_id'],platform='tiktok',paid_at=(r['paid']+timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S'),status='ON_HOLD',skus=['A','C'],order_amount=str(Decimal(r['amount'])*2),exchange_rate='2',cny_total=r['amount'],pay_amount=str(Decimal(r['amount'])*2),currency='BRL') for s in shops for r in rows]}
  self.m=Monitor(host,source=Source(),rates=SimpleNamespace(get=lambda *a:{'fx_rate':None}),clock=lambda:self.clock[0]);self.m.store.prepare(STORES)
 async def asyncTearDown(self):self.tmp.cleanup()
 async def run_once(self,**kwargs):
  result=await self.m.start(**kwargs);await self.m.worker;return self.m.store.snapshot(result['run_id'])
 async def test_rolling_24_72_two_hour_increment_math_restart_and_immutable_history(self):
  first=await self.run_once();self.assertEqual(first['status'],'SUCCEEDED')
  before=json.dumps(first,sort_keys=True);m=first['shop_cards'][0]['windows']['24h'];self.assertEqual((m['current_count'],m['previous_count'],m['aov_rmb'],m['previous_aov_rmb']),(24,24,'20.00','10.00'))
  self.clock[0]+=timedelta(hours=2);self.calls.clear();second=await self.run_once()
  self.assertEqual(second['active_windows'],['24h','72h']);self.assertEqual(second['collection_mode'],incremental.MODE)
  for job in self.calls:
   if job['kind']!='refunds':self.assertEqual(job['start'],first['cutoff']);self.assertEqual(job['end'],second['cutoff'])
  self.assertEqual(sum(j['kind']=='refunds' for j in self.calls),0)
  self.assertTrue(second['refund_estimation']['reused']);self.assertEqual(second['refund_estimation']['observed_at'],first['refund_estimation']['observed_at'])
  self.assertTrue(second['shop_cards'][0]['diagnostics']['refund_estimation']['estimated'])
  m=second['shop_cards'][0]['windows']['24h'];self.assertEqual((m['current_count'],m['previous_count'],m['aov_rmb'],m['previous_aov_rmb']),(24,24,'20.83','10.83'))
  self.assertEqual(second['shop_cards'][0]['windows']['72h']['current_count'],72)
  self.assertEqual(sum(p['windows']['24h']['current_count'] for p in second['shop_cards'][0]['products']),48)
  again=CardStore(self.path);self.assertEqual(json.dumps(again.snapshot(first['run_id']),sort_keys=True),before)
  jobs,metrics=incremental.plan(again,again.config()['shops'],second['start'],second['cutoff'])
  self.assertEqual([j['kind'] for j in jobs],['refunds']);self.assertEqual(metrics['missing_source_hours'],0)
  self.assertFalse(again.events())
 async def test_failed_gap_not_zero_retry_keeps_successful_receipts(self):
  await self.run_once();self.clock[0]+=timedelta(hours=2);self.fail_kind='miaoshou';failed=await self.run_once()
  self.assertEqual(failed['status'],'INCOMPLETE');self.assertTrue(all('MIAOSHOU_CACHE_GAP' in c['errors'] for c in failed['shop_cards']))
  self.assertEqual(failed['shop_cards'][0]['windows']['24h']['current_count'],24)
  self.assertEqual(failed['shop_cards'][0]['diagnostics']['coverage'],{'tms':'COMPLETE','miaoshou':'INCOMPLETE'})
  self.fail_kind=None;self.calls.clear();retry=await self.run_once(retry_id=failed['run_id'])
  self.assertEqual(retry['status'],'SUCCEEDED');self.assertEqual([j['kind'] for j in self.calls],['miaoshou']);self.assertFalse(self.m.store.events())
  original={j['id']:j for j in self.m.store.jobs(failed['run_id'])}
  for j in self.m.store.jobs(retry['run_id']):
   if j.get('reused'):self.assertEqual(j['observed_at'],original[j['id']].get('observed_at') or original[j['id']]['finished_at'])
 async def test_later_refund_removes_cached_paid_order_without_rereading_history(self):
  await self.run_once();self.clock[0]+=timedelta(hours=24);self.refund=True;self.calls.clear();s=await self.run_once()
  self.assertEqual(s['status'],'SUCCEEDED');self.assertEqual(s['shop_cards'][0]['windows']['24h']['previous_count'],23)
  self.assertEqual(s['shop_cards'][1]['windows']['24h']['previous_count'],24,'same platform ID in a different shop must not inherit refund')
  self.assertTrue(all(instant(j['start'])==AT for j in self.calls if j['kind']!='refunds'))
 async def test_four_hour_schedule_two_auto_alerts_manual_and_source_failure(self):
  c=self.m.store.config();c['enabled']=True;c['cycle_hours']=4;c['shops'][0]['push']=True
  c['shops'][0]['default_rules']['24h']['upper']='1';self.m.store.save_cards(c,CAT,STORES)
  sent=[]
  async def send(e):sent.append(e)
  self.m.send=send
  await self.run_once(automatic=True);self.assertFalse(sent)
  await self.run_once();self.assertFalse(sent)
  self.clock[0]+=timedelta(hours=4);await self.run_once(automatic=True)
  self.assertEqual(len(sent),2);self.assertTrue(all(x['changes'][0]['window']=='24h' for x in sent))
  self.fail_kind='refunds';self.clock[0]+=timedelta(hours=24);await self.run_once(automatic=True)
  self.assertEqual(len(sent),2,'incomplete source must not recover an active alarm')
  self.assertIsNone(await self.m.start(True),'same slot must not send again')
 async def test_empty_new_range_advances_coverage_and_mapping_change_does_not_reuse(self):
  await self.run_once();self.orders=[];self.clock[0]+=timedelta(hours=2);s=await self.run_once();self.assertEqual(s['status'],'SUCCEEDED')
  self.assertEqual(s['shop_cards'][0]['windows']['24h']['current_count'],22)
  shop=deepcopy(self.m.store.config()['shops'][0]);shop['miaoshou_shop_id']='changed'
  _,gaps=incremental.read(self.m.store,shop,'miaoshou',s['start'],s['cutoff']);self.assertEqual(gaps,[[s['start'],s['cutoff']]])
 async def test_old_full_receipts_seed_cache_no_snapshot_rewrite(self):
  c=self.m.store.config();c['collection_mode']='ROLLING';self.m.store.save_cards(c,CAT,STORES)
  old=await self.run_once();before=json.dumps(old,sort_keys=True)
  c=self.m.store.config();c['collection_mode']=incremental.MODE;self.m.store.save_cards(c,CAT,STORES)
  self.clock[0]+=timedelta(hours=2);self.calls.clear();new=await self.run_once()
  self.assertEqual(new['status'],'SUCCEEDED');self.assertTrue(all(instant(j['start'])==AT for j in self.calls if j['kind']!='refunds'))
  self.assertEqual(json.dumps(self.m.store.snapshot(old['run_id']),sort_keys=True),before)
 async def test_completed_manual_refresh_does_not_rewind_after_restart(self):
  await self.run_once();self.assertTrue(self.m.current_slot_covered())
  self.clock[0]+=timedelta(hours=2);self.assertFalse(self.m.current_slot_covered())
  await self.run_once(shop_ids=[TMS[0]['id']]);self.assertFalse(self.m.current_slot_covered(),'one shop must not suppress all-shop synchronization')

class MigrationTests(unittest.TestCase):
 def test_migrate_cycle_keeps_rolling_rules_aliases_visibility_and_interval(self):
  old=dict(revision=3,enabled=True,collection_mode='CYCLE',cycle_hours=4,shops=[dict(blank_shop(TMS[0]),hidden=True,hidden_skus=['A'],default_rules={'24h':{'upper':'55'},'72h':{'drop_pct':'10'},'cycle':{'upper':'9'}})])
  c=migrate_config(old,TMS);self.assertEqual(c['collection_mode'],incremental.MODE);self.assertEqual(c['cycle_hours'],4)
  self.assertEqual(c['shops'][0]['default_rules']['24h']['upper'],'55');self.assertEqual(c['shops'][0]['default_rules']['cycle']['upper'],'9');self.assertTrue(c['shops'][0]['hidden'])

class CleanupTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=RollingIncrementalTests.asyncSetUp
 asyncTearDown=RollingIncrementalTests.asyncTearDown
 run_once=RollingIncrementalTests.run_once
 async def test_cleanup_removes_duplicate_details_preserves_summary_and_receipts(self):
  from order_pulse.aov_monitor.cleanup import cleanup
  first=await self.run_once();original=json.dumps(first,sort_keys=True)
  self.clock[0]+=timedelta(hours=2);second=await self.run_once()
  result=cleanup(self.m.store,self.clock[0])
  self.assertEqual(json.dumps(self.m.store.snapshot(first['run_id']),sort_keys=True),original)
  self.assertTrue(self.m.store.orders(first['run_id'],'1')['details_cleared'])
  self.assertFalse(self.m.store.orders(second['run_id'],'1')['details_cleared'])
  jobs,metrics=incremental.plan(self.m.store,self.m.store.config()['shops'],second['start'],second['cutoff'])
  self.assertEqual(metrics['missing_source_hours'],0)
  self.assertTrue(all(j.get('reused') for j in jobs if j['kind']=='refunds'))

 async def test_cleanup_failure_does_not_overwrite_successful_statistics(self):
  from unittest.mock import patch
  with patch('order_pulse.aov_monitor.cleanup.cleanup',side_effect=OSError('disk unavailable')):
   with self.assertLogs('order_pulse.aov_monitor.card_service',level='WARNING'):
    result=await self.run_once()
  self.assertEqual(result['status'],'SUCCEEDED')
