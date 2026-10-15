"""Focused regression checks for paused sync and bounded collection speedups."""
import json
import subprocess
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_aov_cards as fixture
from order_pulse.aov_monitor.service import Monitor,slot_at
from order_pulse.aov_monitor.sources import DualSource
from order_pulse.aov_monitor.card_store import CardStore

class SyncPauseTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):await fixture.CardServiceTests.asyncSetUp(self)
 async def asyncTearDown(self):await fixture.CardServiceTests.asyncTearDown(self)
 def source(self):return fixture.CardServiceTests.source(self)
 async def test_login_expiry_pauses_without_replacing_saved_orders_or_sending(self):
  source=self.source();clock=[fixture.AT]
  m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{}),clock=lambda:clock[0])
  await m.start();await m.worker;before=m.store.snapshot()
  rid=m.store.start(None,{})
  m.store.finish(rid,dict(status='SUCCEEDED'),{'1:f:24h':dict(candidate='UP',streak=1,active='UP')})
  def expired():raise ValueError('AUTH_REQUIRED')
  source.directory=expired;clock[0]+=timedelta(hours=2)
  c=m.store.config();c['enabled']=True;m.store.save_cards(c,fixture.CAT,fixture.STORES)
  await m.start(True);await m.worker
  pending=m.store.pending();self.assertEqual(pending[1]['phase'],'AUTH_REQUIRED')
  self.assertEqual(m.store.snapshot(before['run_id']),before)
  self.assertEqual(m.store.jobs(pending[0]),[]);self.assertFalse(m.store.events())
  # Start with a candidate after saving config, then pause resets only its pending streak.
  m.store.pause(*pending,'AUTH_REQUIRED')
  self.assertTrue(m.store.progress()['waiting_since'])
  self.assertEqual(m.store.alert_states()['1:f:24h']['streak'],0)
  self.assertEqual(m.store.alert_states()['1:f:24h']['active'],'UP')
 async def test_unstarted_old_slot_skipped_but_any_receipt_keeps_frozen_scope(self):
  m=Monitor(self.host,source=self.source(),clock=lambda:fixture.AT)
  old=fixture.AT-timedelta(hours=4);context=dict(card_run=True,slot=slot_at(old).isoformat(),cutoff=old.isoformat(),start=(old-timedelta(days=6)).isoformat())
  rid=m.store.start(context['slot'],context)
  self.assertTrue(m.store.skip_unstarted(rid,context,slot_at(fixture.AT)))
  self.assertIsNone(m.store.pending());self.assertIsNone(m.store.snapshot(rid))
  rid=m.store.start(None,context);m.store.add_jobs(rid,[dict(id='saved',kind='tms',start=context['start'],end=context['cutoff'])])
  m.store.job_done(rid,'saved',{'orders':[]})
  self.assertFalse(m.store.skip_unstarted(rid,context,slot_at(fixture.AT)))
  self.assertEqual(m.store.jobs(rid)[0]['result'],{'orders':[]})
 async def test_batch_timing_persists_and_waiting_is_not_read_time(self):
  store=CardStore(self.path);c=dict(card_run=True,phase='ORDERS',current='job',cutoff=fixture.AT.isoformat())
  rid=store.start(None,c);store.add_jobs(rid,[dict(id='job',kind='miaoshou')])
  with patch('order_pulse.aov_monitor.card_store.now',return_value=fixture.AT.isoformat()):store.job_started(rid,'job')
  with patch('order_pulse.aov_monitor.card_store.now',return_value=(fixture.AT+timedelta(seconds=12)).isoformat()):store.job_done(rid,'job',{'rows':[]})
  store.pause(rid,c,'WAITING_USER');reopened=CardStore(self.path)
  self.assertEqual(reopened.progress()['collection_seconds'],12)
  self.assertEqual(reopened.jobs(rid)[0]['elapsed_seconds'],12)
  reopened.finish(rid,{'status':'SUCCEEDED'})
  self.assertEqual(reopened.snapshot()['timing']['by_source']['miaoshou'],12)

 async def test_refunds_share_frozen_order_payment_scope_and_both_windows(self):
  source=self.source();original=source.batch;refund_jobs=[]
  def capture(job,shops,target):
   if job['kind']=='refunds':refund_jobs.append(dict(job))
   return original(job,shops,target)
  source.batch=capture;m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{}),clock=lambda:fixture.AT)
  await m.start();await m.worker
  self.assertEqual(len(refund_jobs),2)
  for job in refund_jobs:
   self.assertEqual(job['start'],(fixture.AT-timedelta(days=6)).isoformat());self.assertEqual(job['end'],fixture.AT.isoformat())
  self.assertEqual(set(m.store.snapshot()['shop_cards'][0]['windows']),{'24h','72h'})

class BrowserReadinessTests(unittest.TestCase):
 def test_binding_login_and_user_control_have_different_pause_causes(self):
  s=DualSource(None)
  for code in ('AUTH_REQUIRED','WAITING_USER','TMS_PAGE_REQUIRED','BROWSER_BINDING_REQUIRED'):
   result=SimpleNamespace(returncode=0,stdout='AOV_BINDING:'+json.dumps({'error':code}),stderr='')
   with patch('order_pulse.aov_monitor.sources.subprocess.run',return_value=result):
    with self.assertRaisesRegex(ValueError,code):s.resolve({})
  with patch('order_pulse.aov_monitor.sources.subprocess.run',side_effect=subprocess.TimeoutExpired('ego',15)):
   with self.assertRaisesRegex(ValueError,'WAITING_CONNECTION'):s.resolve({})
