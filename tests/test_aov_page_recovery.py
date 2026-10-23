"""A signed-in Mabang session does not require manually opening the order list."""
import json
from pathlib import Path
import subprocess
import unittest

SCRIPT=Path('order_pulse/aov_monitor/resolve_browser.js').read_text()
BINDING=dict(space_id=2,expected_name='Materials',profile_id='Default',profile_name='User',auto_takeover=True,task_active=True)

class PageRecoveryTests(unittest.TestCase):
 def run_script(self,tabs,*,active=True,ownership='agent',redirect=None,timeout=False,ready=True,entries=None):
  entry=dict(id=2,name='Materials',profileId='Default',profileName='User',taskId='Materials',ownership=ownership)
  spec=dict(binding=dict(BINDING,task_active=active),tabs=tabs,entries=entries if entries is not None else [entry],redirect=redirect,timeout=timeout,ready=ready)
  harness='const x='+json.dumps(spec)+''';const s=x.binding,calls=[];
const listTaskSpaces=async()=>x.entries;
const handle=tab=>({label:tab.label,url:async()=>tab.url,
 goto:async(url,opts)=>{calls.push(['goto',tab.label,url,opts.waitUntil]);tab.url=x.redirect||url;if(x.timeout)throw Error('load timeout');},
 waitForFunction:async fn=>{calls.push(['wait',tab.label]);const u=new URL(tab.url);globalThis.location=u;globalThis.document={querySelector:()=>x.ready?{}:null};if(!fn())throw Error('not ready');},
 close:async()=>calls.push(['close',tab.label])});
const task={spaceId:2,tabs:async()=>x.tabs,page:label=>handle(x.tabs.find(t=>t.label===label)),
 adopt:async tab=>{calls.push(['adopt']);tab.label='p8';return handle(tab);},
 newPage:async()=>{calls.push(['new']);const tab={label:'p9',url:'about:blank'};x.tabs.push(tab);return handle(tab);}};
const taskSpace=async()=>task;
const claimTaskSpace=async()=>{calls.push(['claim']);return task;};const takeOverTaskSpace=claimTaskSpace;
'''
  completed=subprocess.run(['node','--input-type=module'],input=harness+SCRIPT+'\nconsole.log(JSON.stringify(calls));',text=True,capture_output=True,check=True)
  lines=completed.stdout.splitlines()
  result=json.loads(next(line.removeprefix('AOV_BINDING:') for line in lines if line.startswith('AOV_BINDING:')))
  return result,json.loads(lines[-1])

 def test_product_or_home_page_is_prepared_and_reused_without_new_tab(self):
  for path in ('#/myproductlist','#/index',''):
   result,calls=self.run_script([dict(label='p2',url='https://tms.mabangerp.com/'+path)])
   self.assertEqual(result['page_label'],'p2')
   self.assertEqual(calls,[['goto','p2','https://tms.mabangerp.com/#/myorderlist','domcontentloaded'],['wait','p2']])

 def test_existing_orders_are_reused_without_reload(self):
  result,calls=self.run_script([dict(label='p2',url='https://tms.mabangerp.com/#/myorderlist')])
  self.assertEqual(result['page_label'],'p2');self.assertEqual(calls,[])

 def test_closed_source_opens_one_tab_in_same_space_and_redirected_login_waits(self):
  for redirect in (None,'https://tms.mabangerp.com/#/login'):
   result,calls=self.run_script([dict(label='p1',url='http://127.0.0.1:8797/')],redirect=redirect)
   self.assertEqual(calls[0],['new']);self.assertEqual(sum(c[0]=='new' for c in calls),1)
   self.assertEqual(result.get('error'),'AUTH_REQUIRED' if redirect else None)
   if not redirect:self.assertEqual(result['page_label'],'p9')

 def test_login_and_passive_checks_never_navigate_or_create_pages(self):
  for url,active,error in [('https://tms.mabangerp.com/#/login',True,'AUTH_REQUIRED'),('https://tms.mabangerp.com/#/home',False,'TMS_PAGE_REQUIRED'),('http://127.0.0.1:8797/',False,'TMS_PAGE_REQUIRED')]:
   result,calls=self.run_script([dict(label='p2',url=url)],active=active)
   self.assertEqual(result['error'],error);self.assertEqual(calls,[])
  result,calls=self.run_script([],active=False,ownership='user')
  self.assertEqual(result['error'],'WAITING_USER');self.assertEqual(calls,[])

 def test_load_timeout_accepts_committed_ready_spa_but_not_unready_page(self):
  tabs=[dict(label='p2',url='https://tms.mabangerp.com/#/home')]
  self.assertEqual(self.run_script(tabs,timeout=True)[0]['page_label'],'p2')
  result,calls=self.run_script(tabs,ready=False)
  self.assertEqual(result['error'],'WAITING_CONNECTION');self.assertNotIn(['close','p2'],calls)
  result,calls=self.run_script([],redirect='chrome-error://chromewebdata/',ready=False)
  self.assertEqual(result['error'],'WAITING_CONNECTION');self.assertIn(['close','p9'],calls)

 def test_ambiguous_or_wrong_identity_never_navigates(self):
  for tabs in ([dict(label='p2',url='https://tms.mabangerp.com/#/home'),dict(label='p3',url='https://tms.mabangerp.com/#/home')],
               [dict(label='p2',url='https://tms.mabangerp.com/#/myorderlist'),dict(label='p3',url='https://tms.mabangerp.com/#/myorderlist')]):
   result,calls=self.run_script(tabs)
   self.assertEqual(result['error'],'BROWSER_BINDING_REQUIRED');self.assertEqual(calls,[])
  result,calls=self.run_script([],entries=[dict(id=2,name='Other',profileId='Default',profileName='User',ownership='agent')])
  self.assertEqual(result['error'],'BROWSER_BINDING_REQUIRED');self.assertEqual(calls,[])

 def test_similar_hostname_is_not_reused_and_task_can_reclaim(self):
  result,calls=self.run_script([dict(label='p2',url='https://tms.mabangerp.com.evil.invalid/#/myorderlist')],ownership='agentDelegatedToUser')
  self.assertEqual(result['page_label'],'p9');self.assertEqual(calls[:2],[['claim'],['new']])

