"""Restart recovery persists only verified logical Space identities."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from order_pulse.storage import Repository
from order_pulse.integrations.browser_binding import OperationsSpaceBinding, KEY
from order_pulse.integrations.browser_control import BrowserControl
from order_pulse.aov_monitor.sources import DualSource

BINDING=dict(space_id=11,expected_name='Order Pulse · 素材与客单价',profile_id='Default',profile_name='昊',purpose='material_aov',auto_takeover=True,tabs=dict(tms_tab='p3',miaoshou_tab='p2',kalodata_tab='p6'))
IDENTITY=dict(space_id=4,space_name=BINDING['expected_name'],profile_id='Default',profile_name='昊')

class ReconnectTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.repo=Repository(self.tmp.name);self.binding=OperationsSpaceBinding(self.repo);self.binding.save(BINDING)

 def test_verified_relocation_survives_restart_and_preserves_other_bindings(self):
  self.repo.set(KEY,{**self.repo.get(KEY),'balance_kalodata':{'sentinel':'keep'}})
  self.binding.record_space(BINDING,IDENTITY)
  self.binding.record_space(BINDING,IDENTITY) # concurrent identical lookup
  restored=OperationsSpaceBinding(Repository(self.tmp.name))
  self.assertEqual(restored.get(),dict(BINDING,space_id=4))
  self.assertEqual(self.repo.get(KEY)['balance_kalodata'],{'sentinel':'keep'})

 def test_bad_identity_and_changed_settings_cannot_overwrite_binding(self):
  for field,value in [('space_name','other'),('profile_id','other'),('profile_name','other'),('space_id',True),('space_id',-1)]:
   with self.assertRaises(ValueError):self.binding.record_space(BINDING,dict(IDENTITY,**{field:value}))
   self.assertEqual(self.binding.get(),BINDING)
  self.binding.save(dict(BINDING,auto_takeover=False))
  with self.assertRaises(ValueError):self.binding.record_space(BINDING,IDENTITY)
  self.assertFalse(self.binding.get()['auto_takeover'])

 def test_control_saves_verified_relocation_without_reading_page(self):
  manager=BrowserControl(self.binding,None)
  with patch('order_pulse.integrations.browser_control.execute',return_value={'state':'CONTROLLED','browser':IDENTITY}):
   self.assertEqual(manager.ensure(BINDING,'task'),'CONTROLLED')
  self.assertEqual(self.binding.get()['space_id'],4)

 def test_source_records_identity_even_when_order_page_is_still_restoring(self):
  source=DualSource(SimpleNamespace(browser_space=self.binding))
  reply=SimpleNamespace(returncode=0,stdout='AOV_BINDING:'+json.dumps({'error':'TMS_PAGE_REQUIRED','browser':IDENTITY}),stderr='')
  with patch('order_pulse.aov_monitor.sources.subprocess.run',return_value=reply):
   with self.assertRaisesRegex(ValueError,'TMS_PAGE_REQUIRED'):source.resolve(BINDING)
  self.assertEqual(self.binding.get()['space_id'],4)

 def test_real_resolver_rejects_ambiguity_and_adopts_current_page_label(self):
  captured=[];source=DualSource(None)
  def capture(*args,**kwargs):
   captured.append(kwargs['input']);return SimpleNamespace(returncode=0,stdout='AOV_BINDING:{"space_id":11,"page_label":"p3"}',stderr='')
  with patch('order_pulse.aov_monitor.sources.subprocess.run',side_effect=capture):source.run_browser_task(source.resolve,BINDING)
  entry=dict(id=4,name=BINDING['expected_name'],profileId='Default',profileName='昊',taskId=BINDING['expected_name'],ownership='user')
  for entries,ok in [([entry],True),([],False),([entry,dict(entry,id=11)],False),([dict(entry,profileId='other')],False),([dict(entry,taskId='other')],False)]:
   harness='const calls=[];const listTaskSpaces=async()=>'+json.dumps(entries)+''';
const task={spaceId:4,tabs:async()=>[{url:'https://tms.mabangerp.com/#/myorderlist',page:{}}],adopt:async()=>({label:'p17'})};
const claimTaskSpace=async id=>{calls.push(id);return task};
'''
   output=subprocess.run(['node','--input-type=module'],input=harness+captured[0]+'\nconsole.log(JSON.stringify(calls));',text=True,capture_output=True,check=True).stdout.splitlines()
   result=json.loads(next(x.removeprefix('AOV_BINDING:') for x in output if x.startswith('AOV_BINDING:')))
   if ok:
    self.assertEqual(result['space_id'],4);self.assertEqual(result['page_label'],'p17');self.assertEqual(result['browser'],IDENTITY)
   else:self.assertEqual(result,{'error':'BROWSER_BINDING_REQUIRED'})
   self.assertEqual(json.loads(output[-1]),[4] if ok else [])
