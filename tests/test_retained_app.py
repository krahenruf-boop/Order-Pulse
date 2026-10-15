import tempfile,unittest
import json,subprocess
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from fastapi.testclient import TestClient
from order_pulse.server import create_app
from order_pulse.service import Service
from order_pulse.integrations.browser_binding import OperationsSpaceBinding
from order_pulse.aov_monitor.sources import DualSource

class RetainedAppTests(unittest.TestCase):
 def test_authorized_space_resume_checks_identity_before_taking_control(self):
  binding=dict(space_id=11,expected_name='Order Pulse · 素材与客单价',profile_id='Default',profile_name='昊',auto_takeover=True)
  captured=[]
  def capture(*args,**kw):
   captured.append(kw['input'])
   return SimpleNamespace(returncode=0,stdout='AOV_BINDING:{"space_id":11,"page_label":"p3"}',stderr='')
  real_run=subprocess.run
  for ownership,active,allowed,profile,expected,calls in [
   ('user',False,True,'Default',{'error':'WAITING_USER'},[]),
   ('agentDelegatedToUser',False,True,'Default',{'error':'WAITING_USER'},[]),
   ('agent',True,True,'Default',{'space_id':11,'page_label':'p3'},['resume']),
   ('user',True,True,'Default',{'space_id':11,'page_label':'p3'},['claim']),
   ('agentDelegatedToUser',True,True,'Default',{'space_id':11,'page_label':'p3'},['takeover']),
   ('user',True,False,'Default',{'error':'WAITING_USER'},[]),
   ('user',True,True,'different',{'error':'BROWSER_BINDING_REQUIRED'},[]),
  ]:
   with self.subTest(ownership=ownership,allowed=allowed,profile=profile):
    source=DualSource(None)
    with patch('order_pulse.aov_monitor.sources.subprocess.run',side_effect=capture):
     if active:source.run_browser_task(source.resolve,{**binding,'auto_takeover':allowed})
     else:source.resolve({**binding,'auto_takeover':allowed})
    entry=dict(id=11,name=binding['expected_name'],profileId=profile,profileName='昊',ownership=ownership)
    harness='const calls=[];const listTaskSpaces=async()=>['+json.dumps(entry)+'''];
const task={spaceId:11,tabs:async()=>[{label:'p3',url:'https://tms.mabangerp.com/#/myorderlist'}],page:label=>({label})};
const taskSpace=async()=>{calls.push('resume');return task};
const claimTaskSpace=async()=>{calls.push('claim');return task};
const takeOverTaskSpace=async()=>{calls.push('takeover');return task};
'''
    p=real_run(['node','--input-type=module'],input=harness+captured[-1]+"\nconsole.log(JSON.stringify(calls));",text=True,capture_output=True,check=True)
    lines=[line for line in p.stdout.splitlines() if not line.startswith('AOV_CONTROL:')]
    self.assertEqual(json.loads(lines[0].removeprefix('AOV_BINDING:')),expected)
    self.assertEqual(json.loads(lines[1]),calls)
 def test_removed_routes_and_tables_stay_absent(self):
  with tempfile.TemporaryDirectory() as d:
   s=Service(d)
   with TestClient(create_app(d,service=s),base_url='http://127.0.0.1:8786') as c:
    csrf=c.get('/api/state').json()['csrf'];headers={'Origin':'http://127.0.0.1:8786','X-CSRF-Token':csrf}
    for path in ['/api/query','/api/settings','/api/product-ranking/refresh','/api/distribution/generate']:
     self.assertEqual(c.post(path,json={},headers=headers).status_code,404,path)
    for path in ['/api/history','/api/unified-history/all','/api/product-ranking/state','/api/distribution/state']:
     self.assertEqual(c.get(path).status_code,404,path)
    for path in ['/api/balance/state','/api/kalodata/status','/api/aov-monitor/state']:
     self.assertEqual(c.get(path).status_code,200,path)
    page=c.get('/').text
    for route in ['#dashboard','#product-ranking','#distribution','#history']:self.assertNotIn(route,page)
    self.assertEqual(c.post('/api/credentials',json={}).status_code,403)
   with s.repo.db() as db:tables={r[0] for r in db.execute("select name from sqlite_master where type='table'")}
   self.assertFalse(tables & {'runs','orders','snapshots','daily_tasks','product_rank_runs','distribution_batches'})
   self.assertIn('aov_monitor_orders',tables);self.assertIn('balance_accounts',tables);self.assertIn('kalodata_runs',tables)
 def test_separate_space_persistence_and_mutex(self):
  with tempfile.TemporaryDirectory() as d:
   s=Service(d)
   balance={'space_id':0,'expected_name':'Order Pulse · Balance + Kalodata','profile_id':'Default','profile_name':'昊','purpose':'balance_kalodata','auto_takeover':True,'tabs':{'balance_tab':'p1','kalodata_tab':None}}
   operations={**balance,'space_id':11,'expected_name':'Order Pulse · 素材与客单价','purpose':'material_aov','tabs':{'tms_tab':'p3','miaoshou_tab':'p2','kalodata_tab':'p6'}}
   s.balance_browser_space.save(balance);s.browser_space.save(operations)
   restored=Service(d)
   self.assertEqual(restored.browser_space.get(),operations)
   self.assertEqual(restored.balance_browser_space.target('balance')['space_id'],0)
   restored.browser_space.lock.acquire()
   try:self.assertFalse(restored.balance_browser_space.lock.locked())
   finally:restored.browser_space.lock.release()
   with self.assertRaises(ValueError):restored.browser_space.save({**operations,'space_id':0})
