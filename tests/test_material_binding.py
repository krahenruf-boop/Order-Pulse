import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from order_pulse.storage import Repository
from order_pulse.integrations.browser_binding import OperationsSpaceBinding, KEY
from order_pulse.integrations.material_binding import rebind

OLD=dict(space_id=3,expected_name='Order Pulse · 素材与客单价',profile_id='Default',profile_name='昊',
         purpose='material_aov',auto_takeover=True,tabs=dict(tms_tab='p3',miaoshou_tab='p2',kalodata_tab='p6'))
NEW=dict(OLD,expected_name='Order Pulse · Kalodata素材 · 客单价监控',tabs=dict(tms_tab='p1',miaoshou_tab='p2',kalodata_tab='p5'))
SELECTION={k:NEW[k] for k in ('space_id','expected_name','profile_id','profile_name')}

class BindingStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.repo=Repository(self.temp.name);self.owner=OperationsSpaceBinding(self.repo);self.owner.save(OLD)

    def test_verified_rename_and_tab_labels_survive_restart_preserving_other_settings(self):
        self.repo.set(KEY,{**self.repo.get(KEY),'balance_kalodata':{'space_id':1}})
        self.repo.set('retained_data',{'history':['keep'],'settings':True})
        self.owner.replace_verified(OLD,NEW)
        self.assertEqual(OperationsSpaceBinding(Repository(self.temp.name)).get(),NEW)
        self.assertEqual(self.repo.get(KEY)['balance_kalodata'],{'space_id':1})
        self.assertEqual(self.repo.get('retained_data'),{'history':['keep'],'settings':True})

    def test_rename_is_explicit_and_does_not_follow_numeric_id(self):
        with self.assertRaises(ValueError):
            self.owner.record_space(OLD,dict(space_id=3,space_name=NEW['expected_name'],profile_id='Default',profile_name='昊'))
        self.assertEqual(self.owner.get(),OLD)
        for name in ('','  ',None,'x'*151):
            with self.assertRaises(ValueError):self.owner.replace_verified(OLD,dict(NEW,expected_name=name))

    def test_rebind_cannot_overwrite_concurrent_settings_or_use_finance_space(self):
        self.owner.save(dict(OLD,auto_takeover=False))
        with self.assertRaises(ValueError):self.owner.replace_verified(OLD,NEW)
        self.repo.set(KEY,{'material_aov':OLD,'balance_kalodata':{'space_id':3}})
        with self.assertRaises(ValueError):self.owner.replace_verified(OLD,NEW)

class BindingScriptTests(unittest.TestCase):
    def run_script(self,*,duplicate=False,profile='Default',workbench='http://127.0.0.1:8797/#kalodata',missing=False,ownership='agentDelegatedToUser'):
        entry=dict(id=3,name=NEW['expected_name'],profileId=profile,profileName='昊',ownership=ownership)
        tabs=[dict(label='p3',url=workbench),dict(label='p1',url='https://tms.mabangerp.com/#/myorderlist'),
              dict(label='p2',url='https://erp.91miaoshou.com/order/package/all?appPackageTab=all'),
              dict(label='p5',url='https://www.kalodata.com/product/detail?id=123'),
              dict(label='p7',url='https://www.kalodata.com/product/detail?id=456')]
        if missing:tabs.pop(1)
        entries=[entry,dict(entry,id=4)] if duplicate else [entry]
        harness='const INPUT='+json.dumps(dict(selection=SELECTION,previous=OLD))+';const calls=[];const listTaskSpaces=async()=>'+json.dumps(entries)+';const task={spaceId:3,tabs:async()=>'+json.dumps(tabs)+',page:label=>({label})};const taskSpace=async()=>task;const takeOverTaskSpace=async id=>{calls.push(id);return task};const claimTaskSpace=takeOverTaskSpace;'
        script=Path('order_pulse/integrations/ego_material_binding.js').read_text()
        result=subprocess.run(['node','--input-type=module'],input=harness+'try{'+script+'}catch{console.log("REJECTED")}console.log(JSON.stringify(calls));',text=True,capture_output=True,check=True)
        lines=result.stdout.splitlines();return lines[0],json.loads(lines[-1])

    def test_renamed_space_reclaims_and_recovers_actual_tabs_without_focusing_sources(self):
        result,calls=self.run_script()
        self.assertEqual(json.loads(result.split(':',1)[1])['binding'],NEW)
        self.assertEqual(calls,[3])

    def test_wrong_profile_duplicate_name_finance_workbench_or_missing_sources_rejected(self):
        for values in (dict(profile='other'),dict(duplicate=True),dict(workbench='http://127.0.0.1:8786/#balance'),dict(missing=True)):
            result,calls=self.run_script(**values)
            self.assertEqual(result,'REJECTED')
            if 'profile' in values or 'duplicate' in values:self.assertEqual(calls,[])

class BindingEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.owner=OperationsSpaceBinding(Repository(self.temp.name));self.owner.save(OLD)
        self.host=SimpleNamespace(browser_space=self.owner)

    async def test_busy_or_changed_identity_cannot_save(self):
        self.owner.lock.acquire()
        try:
            with patch('order_pulse.integrations.material_binding.execute') as execute:
                with self.assertRaisesRegex(ValueError,'正在执行任务'):await rebind(self.host,SELECTION)
                execute.assert_not_called()
        finally:self.owner.lock.release()
        with patch('order_pulse.integrations.material_binding.execute',return_value={'binding':dict(NEW,space_id=99)}):
            with self.assertRaises(ValueError):await rebind(self.host,SELECTION)
        self.assertEqual(self.owner.get(),OLD)
        self.assertFalse(self.owner.lock.locked())

    async def test_valid_rebind_saves_verified_identity(self):
        with patch('order_pulse.integrations.material_binding.execute',return_value={'binding':NEW}):
            self.assertEqual((await rebind(self.host,SELECTION))['binding'],NEW)
        self.assertFalse(self.owner.lock.locked())

class BindingRouteProtectionTests(unittest.TestCase):
    def test_rebind_is_materials_only_and_keeps_origin_and_csrf_protection(self):
        from order_pulse.server import create_app
        with tempfile.TemporaryDirectory() as root:
            client=TestClient(create_app(root,role='materials'),base_url='http://127.0.0.1:8797')
            self.addCleanup(client.close)
            path='/api/materials/browser-binding/rebind'
            token=client.get('/api/state').json()['csrf']
            self.assertEqual(client.post(path,json=SELECTION).status_code,403)
            self.assertEqual(client.post(path,json=SELECTION,headers={'Origin':'https://example.invalid','X-CSRF-Token':token}).status_code,403)
            verified=AsyncMock(return_value={'bound':True,'binding':NEW})
            with patch('order_pulse.integrations.material_binding.rebind',verified):
                response=client.post(path,json=SELECTION,headers={'Origin':'http://127.0.0.1:8797','X-CSRF-Token':token})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['binding'],NEW)
            verified.assert_awaited_once()
        with tempfile.TemporaryDirectory() as root:
            finance=TestClient(create_app(root,role='finance'),base_url='http://127.0.0.1:8786')
            self.addCleanup(finance.close)
            self.assertEqual(finance.get('/api/materials/browser-binding').status_code,404)
