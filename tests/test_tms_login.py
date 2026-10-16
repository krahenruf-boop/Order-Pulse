import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from order_pulse.storage import Repository
from order_pulse.integrations.browser_binding import OperationsSpaceBinding
from order_pulse.integrations.tms_login import TmsLogin,KEY
from order_pulse.integrations.login_keychain import KeychainError
from order_pulse.aov_monitor.sources import DualSource
from tests.test_browser_reconnect import BINDING,IDENTITY
from fastapi.testclient import TestClient
from order_pulse.service import Service
from order_pulse.server import create_app

class LoginTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.repo=Repository(self.tmp.name);self.binding=OperationsSpaceBinding(self.repo);self.binding.save(BINDING)
  self.repo.set('binding',{'account':{'employee_id':'1','supplier_id':'2','customer_id':'3'}})
  self.keychain=Mock();self.keychain.read.side_effect=lambda scope:dict(username='sample-account',password='test-only-secret')
  self.runner=Mock(return_value={'status':'SIGNED_IN'});self.now=1000
  self.host=SimpleNamespace(repo=self.repo,browser_space=self.binding)
  self.login=TmsLogin(self.host,self.keychain,self.runner,lambda:self.now);self.host.tms_login=self.login

 def configure(self):return self.login.configure(dict(enabled=True,username='sample-account',password='test-only-secret'))

 def test_credentials_only_enter_keychain_not_database_or_state(self):
  state=self.configure();self.assertTrue(state['enabled'])
  raw=self.repo.path.read_bytes()
  self.assertNotIn(b'test-only-secret',raw);self.assertNotIn(b'sample-account',raw)
  self.assertNotIn('test-only-secret',json.dumps(state));self.keychain.save.assert_called_once()
  self.assertTrue(TmsLogin(self.host,self.keychain).state()['enabled'])

 def test_disabled_missing_changed_identity_and_user_mutex_do_not_submit(self):
  self.assertFalse(self.login.attempt(BINDING));self.configure()
  self.binding.lock.acquire()
  try:self.assertFalse(self.login.attempt(BINDING))
  finally:self.binding.lock.release()
  self.repo.set('binding',{'account':{'employee_id':'different'}})
  self.assertFalse(self.login.attempt(BINDING));self.assertFalse(self.login.state()['configured'])
  self.runner.assert_not_called()

 def test_restart_cooldown_and_blocked_challenge_do_not_repeat_submit(self):
  for status in ('VERIFICATION_REQUIRED','CREDENTIALS_REJECTED','ACCOUNT_SCOPE_CHANGED'):
   self.configure();self.runner.return_value={'status':status};self.now+=1000
   self.assertFalse(self.login.attempt(self.binding.get()))
   self.now+=7200
   restarted=TmsLogin(self.host,self.keychain,self.runner,lambda:self.now)
   self.runner.reset_mock();self.assertFalse(restarted.attempt(self.binding.get()))
   if status=='VERIFICATION_REQUIRED':
    self.runner.assert_called_once();self.assertTrue(self.runner.call_args.args[1]['probe_only']);self.assertNotIn('credentials',self.runner.call_args.args[1])
   else:self.runner.assert_not_called()
  self.configure();self.runner.return_value={'status':'UNAVAILABLE'}
  self.assertFalse(self.login.attempt(BINDING));self.now+=30;self.runner.reset_mock()
  self.assertFalse(self.login.attempt(BINDING));self.runner.assert_not_called()

 def test_keychain_lock_stops_without_browser_and_settings_enable_requires_secret(self):
  self.login.configure({'enabled':False})
  self.assertFalse(self.login.state()['configured'])
  with self.assertRaises(ValueError):self.login.configure({'enabled':True})
  self.configure();self.keychain.read.side_effect=KeychainError('KEYCHAIN_LOCKED')
  self.assertFalse(self.login.attempt(BINDING));self.runner.assert_not_called()
  self.assertEqual(self.login.state()['status'],'KEYCHAIN_LOCKED')

 def test_renumbered_space_reuses_same_keychain_and_verified_session_clears_challenge(self):
  self.configure();scope=self.login.scope();self.binding.record_space(BINDING,IDENTITY)
  self.assertEqual(self.login.scope(),scope)
  self.runner.return_value={'status':'VERIFICATION_REQUIRED'};self.login.attempt(self.binding.get())
  self.login.observed_signed_in();self.assertEqual(self.login.state()['status'],'SIGNED_IN')

 def test_manual_verification_unlocks_one_normal_login_without_waiting_for_cooldown(self):
  self.configure();self.runner.return_value={'status':'VERIFICATION_REQUIRED'};self.login.attempt(BINDING)
  self.runner.side_effect=[{'status':'READY'},{'status':'SIGNED_IN'}];self.runner.reset_mock()
  self.assertTrue(self.login.attempt(BINDING));self.assertEqual(self.runner.call_count,2)
  self.assertTrue(self.runner.call_args_list[0].args[1]['probe_only'])
  self.assertEqual(self.login.state()['status'],'SIGNED_IN')

 def test_only_active_source_tasks_attempt_login_and_only_once(self):
  self.configure();self.binding.record_space(BINDING,IDENTITY);source=DualSource(self.host)
  reply=SimpleNamespace(returncode=0,stdout='AOV_BINDING:'+json.dumps({'error':'AUTH_REQUIRED','browser':IDENTITY}),stderr='')
  with patch('order_pulse.aov_monitor.sources.subprocess.run',return_value=reply),patch.object(self.login,'attempt',return_value=True) as attempt:
   with self.assertRaisesRegex(ValueError,'AUTH_REQUIRED'):source.resolve(self.binding.get())
   attempt.assert_not_called()
   with self.assertRaisesRegex(ValueError,'AUTH_REQUIRED'):source.run_browser_task(source.resolve,self.binding.get())
   attempt.assert_called_once()

class LoginScriptTests(unittest.TestCase):
 def test_hidden_completed_challenge_does_not_block_login_form(self):
  script=Path('order_pulse/integrations/ego_tms_login.js').read_text().split('try {\n  const b=INPUT.binding;')[0]
  program='''
globalThis.location={origin:'https://tms.mabangerp.com',hash:'#/login'};
globalThis.innerHeight=900;globalThis.innerWidth=1440;
const element=(text,{visible=true,top=0}={})=>({children:[],textContent:text,
  getClientRects:()=>[{}],checkVisibility:()=>visible,
  getBoundingClientRect:()=>({top,bottom:top+20,left:0,right:200})});
const inputs=[element(''),element(''),element('登录')];
let notice=element('请完成拼图验证码',{visible:false,top:-1000000});
globalThis.document={querySelectorAll:s=>s==='body *'?[...inputs,notice]:
  s.includes('el-icon-s-custom')?[inputs[0]]:s.includes('el-icon-key')?[inputs[1]]:[inputs[2]]};
'''
  checks="console.log(loginView().kind);notice=element('请完成拼图验证码');console.log(loginView().kind);notice=element('密码错误');console.log(loginView().kind);"
  output=subprocess.run(['node','--input-type=module'],input=program+script+checks,text=True,capture_output=True,check=True).stdout
  self.assertEqual(output.splitlines(),['FORM','CHALLENGE','REJECTED'])

 def execute(self,views,*,profile='Default',probe_only=False):
  spec=dict(binding=BINDING,account=dict(employee_id='1',supplier_id='2',customer_id='3'),probe_only=probe_only,credentials=dict(username='sample-account',password='test-only-secret'))
  entry=dict(id=11,name=BINDING['expected_name'],profileId=profile,profileName='昊',ownership='agent')
  program='const INPUT='+json.dumps(spec)+';const views='+json.dumps(views)+';const calls=[];const listTaskSpaces=async()=>['+json.dumps(entry)+'''];
const p={evaluate:async()=>views.shift(),fill:async s=>calls.push(s),click:async()=>calls.push('submit'),waitForFunction:async()=>{},url:async()=>'https://tms.mabangerp.com/#/default',goto:async url=>calls.push(url)};
const task={spaceId:11,tabs:async()=>[{label:'p1',url:'https://tms.mabangerp.com/#/login'}],page:()=>p};const taskSpace=async()=>task;
'''
  output=subprocess.run(['node','--input-type=module'],input=program+Path('order_pulse/integrations/ego_tms_login.js').read_text()+'\nconsole.log(JSON.stringify(calls));',text=True,capture_output=True,check=True).stdout
  self.assertNotIn('test-only-secret',output)
  lines=output.splitlines();return json.loads(lines[0].split(':',1)[1]),json.loads(lines[1])

 def test_visible_challenge_never_submitted_or_solved(self):
  result,calls=self.execute([{'kind':'CHALLENGE'}]);self.assertEqual(result['status'],'VERIFICATION_REQUIRED');self.assertEqual(calls,[])
  result,calls=self.execute([{'kind':'FORM'}]*3+[{'kind':'CHALLENGE'}])
  self.assertEqual(result['status'],'VERIFICATION_REQUIRED');self.assertEqual(calls.count('submit'),1)
  result,calls=self.execute([{'kind':'FORM'}],probe_only=True);self.assertEqual(result['status'],'READY');self.assertEqual(calls,[])

 def test_correct_account_enters_order_list_but_wrong_identity_stops(self):
  session={'kind':'SESSION','account':dict(employee_id='1',supplier_id='2',customer_id='3')}
  result,calls=self.execute([{'kind':'FORM'}]*3+[session]);self.assertEqual(result['status'],'SIGNED_IN')
  self.assertIn('el-icon-s-custom',calls[0]);self.assertIn('el-icon-key',calls[1])
  self.assertEqual(calls[-1],'https://tms.mabangerp.com/#/myorderlist')
  result,calls=self.execute([dict(session,account={'employee_id':'other'})]);self.assertEqual(result['status'],'ACCOUNT_SCOPE_CHANGED');self.assertEqual(calls,[])
  result,calls=self.execute([{'kind':'FORM'}],profile='other');self.assertEqual(result['status'],'UNAVAILABLE');self.assertEqual(calls,[])

class LoginApiTests(unittest.TestCase):
 def test_settings_use_csrf_and_never_return_secrets(self):
  with tempfile.TemporaryDirectory() as d:
   host=Service(d);host.browser_space.save(BINDING);host.repo.set('binding',{'account':{'employee_id':'1'}})
   host.tms_login.keychain=Mock()
   with TestClient(create_app(d,service=host),base_url='http://127.0.0.1:8786') as client:
    spec={'enabled':True,'username':'sample-account','password':'test-only-secret'}
    self.assertEqual(client.post('/api/aov-monitor/login',json=spec).status_code,403)
    token=client.get('/api/state').json()['csrf']
    response=client.post('/api/aov-monitor/login',json=spec,headers={'Origin':'http://127.0.0.1:8786','X-CSRF-Token':token})
    self.assertEqual(response.status_code,200);self.assertTrue(response.json()['enabled'])
    response=client.get('/api/aov-monitor/login')
    self.assertNotIn('test-only-secret',response.text);self.assertNotIn('sample-account',response.text)
