import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from order_pulse.integrations.browser_control import BrowserControl
from order_pulse.kalodata.models import KalodataError

BINDING=dict(space_id=11,expected_name='Order Pulse · 素材与客单价',profile_id='Default',profile_name='昊',purpose='material_aov',auto_takeover=True)

class ControlScriptTests(unittest.TestCase):
    def execute(self, *, idle=300, reason='idle', ownership='user', profile='Default', purpose='material_aov', returned_id=None, entry_id=11, extra=None, task_id=BINDING['expected_name']):
        with tempfile.TemporaryDirectory() as root:
            helper=Path(root)/'idle.py';helper.write_text('print('+repr(json.dumps(idle))+')')
            spec=dict(binding=dict(BINDING,purpose=purpose),reason=reason,python=sys.executable,idle_script=str(helper))
            entry=dict(id=entry_id,name=BINDING['expected_name'],profileId=profile,profileName='昊',ownership=ownership,taskId=task_id)
            returned_id=entry_id if returned_id is None else returned_id
            harness='const INPUT='+json.dumps(spec)+';const calls=[];const listTaskSpaces=async()=>'+json.dumps([entry]+(extra or []))+';const claimTaskSpace=async id=>{calls.push("claim");return {spaceId:'+str(returned_id)+'}};const takeOverTaskSpace=async id=>{calls.push("takeover");return {spaceId:'+str(returned_id)+'}};'
            script=Path('order_pulse/integrations/ego_control.js').read_text()
            out=subprocess.run(['node','--input-type=module'],input=harness+script+'\nconsole.log(JSON.stringify(calls));',text=True,capture_output=True,check=True)
            lines=out.stdout.splitlines()
            return json.loads(lines[0].split(':',1)[1]),json.loads(lines[-1])

    def test_idle_grace_and_unknown_idle_fail_closed(self):
        for idle in (None,0,1,299.99):
            result,calls=self.execute(idle=idle)
            self.assertEqual(result['state'],'USER_ACTIVE');self.assertEqual(calls,[])
        result,calls=self.execute(idle=300)
        self.assertEqual(result['state'],'CONTROLLED');self.assertEqual(calls,['claim'])

    def test_real_task_and_balance_can_reclaim_immediately(self):
        for reason,purpose in [('task','material_aov'),('balance','balance_kalodata')]:
            result,calls=self.execute(idle=0,reason=reason,purpose=purpose,ownership='agentDelegatedToUser')
            self.assertEqual(result['state'],'CONTROLLED');self.assertEqual(calls,['takeover'])
        self.assertEqual(self.execute(reason='balance')[1],[])

    def test_existing_control_wrong_identity_and_wrong_returned_space(self):
        self.assertEqual(self.execute(ownership='agent')[1],[])
        self.assertEqual(self.execute(profile='other')[1],[])
        self.assertEqual(self.execute(returned_id=99)[0]['state'],'BINDING_REQUIRED')

    def test_restart_resolves_unique_identity_not_reassigned_numeric_id(self):
        unrelated=dict(id=11,name='Other Space',profileId='Default',profileName='昊',ownership='user')
        result,calls=self.execute(entry_id=4,extra=[unrelated],reason='task')
        self.assertEqual(result['state'],'CONTROLLED');self.assertEqual(calls,['claim'])
        self.assertEqual(result['browser']['space_id'],4)
        self.assertEqual(self.execute(entry_id=0)[0]['browser']['space_id'],0)
        self.assertEqual(self.execute(entry_id=4,task_id='another task')[0]['state'],'BINDING_REQUIRED')
        # Re-identification does not bypass the five-minute user idle grace.
        result,calls=self.execute(entry_id=4,idle=1)
        self.assertEqual(result['state'],'USER_ACTIVE');self.assertEqual(calls,[])

    def test_duplicate_logical_identity_is_ambiguous_even_when_old_id_exists(self):
        duplicate=dict(id=4,name=BINDING['expected_name'],profileId='Default',profileName='昊',ownership='agent')
        result,calls=self.execute(extra=[duplicate],reason='task')
        self.assertEqual(result,{'state':'BINDING_REQUIRED'});self.assertEqual(calls,[])

class ControlManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.operations=SimpleNamespace(get=lambda:BINDING,lock=threading.Lock())
        self.balance=SimpleNamespace(get=lambda:dict(BINDING,purpose='balance_kalodata'))
        self.control=BrowserControl(self.operations,self.balance)

    async def test_idle_local_timer_resets_with_input_and_does_not_fetch_pages(self):
        for idle,delay,purposes in [(0,300,['balance']),(299,1,['balance']),(300,300,['balance','idle']),(None,300,['balance'])]:
            with patch('order_pulse.integrations.browser_control.input_idle_seconds',return_value=idle),patch.object(self.control,'ensure',return_value='CONTROLLED') as ensure:
                self.assertEqual(await self.control.maintain_once(),delay)
                self.assertEqual([c.args[1] for c in ensure.call_args_list],purposes)

    async def test_active_operation_reclaims_and_always_releases_mutex_not_control(self):
        with patch.object(self.control,'ensure',return_value='CONTROLLED') as ensure:
            with self.assertRaises(RuntimeError):
                async with self.control.operation():
                    self.assertTrue(self.operations.lock.locked())
                    raise RuntimeError('source failed')
            ensure.assert_called_once_with(BINDING,'task')
        self.assertFalse(self.operations.lock.locked())
        with patch.object(self.control,'ensure',return_value='BINDING_REQUIRED'):
            with self.assertRaises(KalodataError) as exc:
                async with self.control.operation():self.fail('must not run plugin')
            self.assertEqual(exc.exception.code,'BROWSER_CONTROL_UNAVAILABLE')
        self.assertFalse(self.operations.lock.locked())

    async def test_cancel_during_mutex_wait_does_not_release_another_job_lock(self):
        self.operations.lock.acquire()
        async def wait():
            async with self.control.operation():self.fail('still busy')
        worker=asyncio.create_task(wait());await asyncio.sleep(0);worker.cancel()
        with self.assertRaises(asyncio.CancelledError):await worker
        self.assertTrue(self.operations.lock.locked());self.operations.lock.release()
