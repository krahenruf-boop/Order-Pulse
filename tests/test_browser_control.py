"""Task control and five-minute idle grace stay separate from source polling."""
import asyncio
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from order_pulse.aov_monitor.sources import DualSource
from order_pulse.aov_monitor.service import Monitor
from tests import test_aov_cards as fixture

BINDING=dict(space_id=11,expected_name='Order Pulse · 素材与客单价',profile_id='Default',profile_name='昊',auto_takeover=True)

class BrowserControlTests(unittest.TestCase):
 def test_claim_is_retained_on_success_missing_page_and_timeout(self):
  for result in ({'space_id':11,'page_label':'p3'},{'error':'TMS_PAGE_REQUIRED'},None):
   source=DualSource(None)
   reply=SimpleNamespace(returncode=0,stdout='AOV_CONTROL:claimed\nAOV_BINDING:'+json.dumps(result),stderr='')
   with self.subTest(result=result),patch('order_pulse.integrations.ego_runtime.execute',return_value={'released':True}) as release:
    with patch('order_pulse.aov_monitor.sources.subprocess.run',**({'side_effect':subprocess.TimeoutExpired('ego',15,output=b'AOV_CONTROL:claimed\n')} if result is None else {'return_value':reply})):
     if result and not result.get('error'):source.run_browser_task(source.resolve,BINDING)
     else:
      with self.assertRaises(ValueError):source.run_browser_task(source.resolve,BINDING)
    release.assert_not_called()
    self.assertFalse(source._control.active)
    self.assertIsNone(source._control.claimed)

 def test_existing_codex_control_is_not_released(self):
  source=DualSource(None)
  with patch('order_pulse.aov_monitor.sources.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='AOV_BINDING:{"space_id":11,"page_label":"p3"}',stderr='')),patch('order_pulse.integrations.ego_runtime.execute') as release:
   source.run_browser_task(source.resolve,BINDING)
   release.assert_not_called()

 def test_handoff_only_addresses_the_same_still_owned_space(self):
  script=Path('order_pulse/integrations/ego_handoff.js').read_text()
  for ownership,profile,expected in [('agent','Default',1),('user','Default',0),('agent','different',0)]:
   entry=dict(id=11,name=BINDING['expected_name'],profileId=profile,profileName='昊',ownership=ownership)
   harness='const INPUT='+json.dumps(BINDING)+';const calls=[];const listTaskSpaces=async()=>['+json.dumps(entry)+'];const taskSpace=async id=>({handOff:async()=>{calls.push(id)}});'
   output=subprocess.run(['node','--input-type=module'],input=harness+script+'\nconsole.log(JSON.stringify(calls))',text=True,capture_output=True,check=True)
   self.assertEqual(len(json.loads(output.stdout.splitlines()[-1])),expected)

class PausedMonitorTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=fixture.CardServiceTests.asyncSetUp
 asyncTearDown=fixture.CardServiceTests.asyncTearDown
 source=fixture.CardServiceTests.source

 async def test_restart_waits_without_work_and_explicit_resume_reuses_run(self):
  source=self.source();original=source.directory;calls=[]
  def directory():calls.append('directory');return original()
  source.directory=directory
  source.readiness=lambda:{'ready':False,'error':'WAITING_USER'}
  m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{}),clock=lambda:fixture.AT)
  context=dict(card_run=True,cutoff=fixture.AT.isoformat(),start=(fixture.AT-fixture.timedelta(days=6)).isoformat(),phase='TMS_PAGE_REQUIRED')
  rid=m.store.start(None,context)
  await m.startup()
  await asyncio.sleep(.01)
  self.assertEqual(calls,[])
  self.assertIsNone(m.worker)
  result=await m.resume();self.assertEqual(result['run_id'],rid)
  with self.assertRaises(ValueError):await m.resume()
  await m.worker
  self.assertEqual(calls,['directory'])
  self.assertEqual(m.store.snapshot()['run_id'],rid)
  self.assertIsNone(m.store.pending())
  await m.shutdown()

 async def test_idle_waits_two_hours_but_pending_tms_retries_after_twenty_seconds(self):
  m=Monitor(self.host,source=self.source(),clock=lambda:fixture.AT)
  self.assertEqual(m.scheduler_delay(),7200)
  rid=m.store.start(None,dict(card_run=True,phase='TMS_PAGE_REQUIRED'))
  self.assertEqual(m.scheduler_delay(),20)
  m.store.context(rid,dict(card_run=True,phase='WAITING_CONNECTION'))
  self.assertEqual(m.scheduler_delay(),20)
  m.store.context(rid,dict(card_run=True,phase='BROWSER_BINDING_REQUIRED'))
  self.assertEqual(m.scheduler_delay(),20)
  for phase in ('WAITING_USER','AUTH_REQUIRED'):
   m.store.context(rid,dict(card_run=True,phase=phase))
   self.assertEqual(m.scheduler_delay(),7200)
  m.worker=asyncio.create_task(asyncio.sleep(0))
  self.assertEqual(m.scheduler_delay(),20)
  await m.worker
  self.assertEqual(m.scheduler_delay(),7200)

 async def test_pending_restart_readiness_is_an_authorized_task_and_keeps_run_id(self):
  source=self.source();active=[]
  def runner(fn,*args):
   active.append(True)
   try:return fn(*args)
   finally:active.pop()
  def readiness():
   self.assertEqual(active,[True]);return {'ready':True}
  source.run_browser_task=runner;source.readiness=readiness
  m=Monitor(self.host,source=source,rates=SimpleNamespace(get=lambda *a:{}),clock=lambda:fixture.AT)
  rid=m.store.start(None,dict(card_run=True,cutoff=fixture.AT.isoformat(),start=(fixture.AT-fixture.timedelta(days=6)).isoformat(),phase='BROWSER_BINDING_REQUIRED'))
  async def stop_after_wait(seconds):m.stopping=True
  with patch('order_pulse.aov_monitor.service.asyncio.sleep',side_effect=stop_after_wait):await m.scheduler()
  self.assertIsNotNone(m.worker)
  m.stopping=False
  await m.worker
  self.assertEqual(m.store.snapshot()['run_id'],rid)

 async def test_idle_scheduler_sleeps_without_browser_readiness(self):
  from unittest.mock import Mock
  source=self.source();source.readiness=Mock(side_effect=AssertionError('idle browser check'))
  m=Monitor(self.host,source=source,clock=lambda:fixture.AT)
  waits=[]
  async def stop_after_wait(seconds):
   waits.append(seconds);m.stopping=True
  with patch('order_pulse.aov_monitor.service.asyncio.sleep',side_effect=stop_after_wait):
   await m.scheduler()
  self.assertEqual(waits,[7200]);source.readiness.assert_not_called()

 async def test_idle_delay_aligns_to_next_two_hour_slot(self):
  m=Monitor(self.host,source=self.source(),clock=lambda:fixture.AT+fixture.timedelta(minutes=30))
  with patch.object(m.store,'config',return_value=dict(enabled=True,collection_mode='ROLLING_INCREMENTAL',cycle_hours=2)):
   self.assertEqual(m.scheduler_delay(),5400)
