"""Completion focus respects Space identity, human control, and source mutexes."""
import asyncio
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from order_pulse.aov_monitor.sources import DualSource
from order_pulse.balance.bridge import BrowserBridge
from order_pulse.integrations.browser_control import BrowserControl
from order_pulse.kalodata.models import KalodataError


BINDING = dict(space_id=4, expected_name='Order Pulse · 素材与客单价',
               profile_id='Default', profile_name='昊', purpose='material_aov')
ENTRY = dict(id=4, name=BINDING['expected_name'], profileId='Default',
             profileName='昊', ownership='agent', taskId=BINDING['expected_name'])
TABS = [dict(label='p1', url='https://tms.mabangerp.com/#/myorderlist', active=True),
        dict(label='p2', url='http://127.0.0.1:8786/#aov-monitor', active=False)]


class WorkbenchScriptTests(unittest.TestCase):
    def run_script(self, *, entries=None, tabs=None, module='aov-monitor', binding=None, after=None):
        spec = dict(entries=entries if entries is not None else [ENTRY], tabs=tabs if tabs is not None else TABS,
                    binding=binding or BINDING, module=module, after=after)
        script = Path('order_pulse/integrations/ego_workbench.js').read_text()
        harness = '''
const calls=[];let reads=0;
const listTaskSpaces=async()=> ++reads>1&&INPUT.after ? INPUT.after : INPUT.entries;
const taskSpace=async id=>({tabs:async()=>INPUT.tabs,
 page:label=>({cdp:async(method)=>{calls.push({id,label,method})}}),
 adopt:async()=>{throw Error('unexpected adoption')}});
'''
        output = subprocess.run(['node', '--input-type=module'], text=True, capture_output=True, check=True,
                                input='const INPUT='+json.dumps(spec)+';\n'+harness+script+'\nconsole.log(JSON.stringify(calls));\n')
        lines = output.stdout.splitlines()
        result = json.loads(next(line.split(':', 1)[1] for line in lines if line.startswith('ORDER_PULSE_WORKBENCH:')))
        return result['state'], json.loads(lines[-1])

    def test_returns_to_existing_workbench_without_navigating_source(self):
        state, calls = self.run_script()
        self.assertEqual(state, 'VISIBLE')
        self.assertEqual(calls, [dict(id=4, label='p2', method='Page.bringToFront')])

    def test_balance_and_operations_are_separate_and_renumbering_is_verified(self):
        b = dict(BINDING, expected_name='Order Pulse · Balance + Kalodata', purpose='balance_kalodata')
        e = dict(ENTRY, id=19, name=b['expected_name'], taskId=b['expected_name'])
        tabs = [TABS[0], dict(TABS[1], url='http://127.0.0.1:8786/#balance')]
        state, calls = self.run_script(entries=[e], binding=b, tabs=tabs, module='balance')
        self.assertEqual((state, calls[0]['id']), ('VISIBLE', 19))
        self.assertEqual(self.run_script(entries=[dict(e, taskId='other')], binding=b, tabs=tabs)[1], [])
        self.assertEqual(self.run_script(entries=[e], binding=b, tabs=TABS, module='balance')[0], 'WORKBENCH_MISSING')

    def test_never_claims_or_switches_ambiguous_wrong_or_human_owned_spaces(self):
        for entries in ([dict(ENTRY, ownership='user')], [dict(ENTRY, ownership='agentDelegatedToUser')],
                        [dict(ENTRY, profileId='other')], [ENTRY, dict(ENTRY, id=5)], []):
            with self.subTest(entries=entries):self.assertEqual(self.run_script(entries=entries)[1], [])
        self.assertEqual(self.run_script(after=[dict(ENTRY, ownership='user')])[1], [])

    def test_login_stays_visible_and_workbench_is_not_recreated(self):
        self.assertEqual(self.run_script(tabs=[dict(TABS[0], url='https://tms.mabangerp.com/#/login'), TABS[1]])[0], 'WAITING_USER')
        for url in ['http://127.0.0.1:8796/#aov-monitor', 'https://example.org/#aov-monitor',
                    'http://127.0.0.1:8786/?redirect=x#aov-monitor']:
            self.assertEqual(self.run_script(tabs=[TABS[0], dict(TABS[1], url=url)])[0], 'WORKBENCH_MISSING')
        self.assertEqual(self.run_script(tabs=[dict(TABS[0], active=False), dict(TABS[1], active=True)])[1], [])
        self.assertEqual(self.run_script(tabs=TABS+[dict(TABS[1], label='p3')])[0], 'AMBIGUOUS')


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = SimpleNamespace(lock=threading.Lock(), get=lambda:dict(BINDING))
        self.balance = SimpleNamespace(lock=threading.Lock(), get=lambda:dict(BINDING, purpose='balance_kalodata'))
        self.control = BrowserControl(self.owner, self.balance)

    async def test_material_success_failure_and_cancel_return_before_releasing_mutex(self):
        for error in (None, ValueError('source failed'), asyncio.CancelledError()):
            with self.subTest(error=error), patch.object(self.control, 'ensure', return_value='CONTROLLED'):
                def restore(*args, **kwargs):
                    self.assertTrue(self.owner.lock.locked())
                    return 'VISIBLE'
                with patch.object(self.control, 'show_workbench', side_effect=restore) as show:
                    try:
                        async with self.control.operation():
                            if error:raise error
                    except (ValueError, asyncio.CancelledError):pass
                    show.assert_called_once_with('kalodata', locked=True)
                    self.assertFalse(self.owner.lock.locked())

    async def test_no_focus_for_failed_control_or_login_challenge(self):
        with patch.object(self.control, 'show_workbench') as show:
            with patch.object(self.control, 'ensure', return_value='USER_ACTIVE'):
                with self.assertRaises(KalodataError):
                    async with self.control.operation():pass
            self.control.material_needs_user = lambda:True
            with patch.object(self.control, 'ensure', return_value='CONTROLLED'):
                async with self.control.operation():pass
            show.assert_not_called()
            self.assertFalse(self.owner.lock.locked())

    async def test_focus_is_best_effort_and_never_interrupts_other_module(self):
        with patch('order_pulse.integrations.browser_control.execute', side_effect=RuntimeError('browser closed')) as execute:
            self.owner.lock.acquire()
            self.assertEqual(self.control.show_workbench('aov-monitor'), 'BUSY')
            execute.assert_not_called()
            self.owner.lock.release()
            self.assertEqual(self.control.show_workbench('aov-monitor'), 'UNAVAILABLE')
            self.assertFalse(self.owner.lock.locked())

    async def test_balance_and_aov_both_return_after_success_and_failure(self):
        for error in (None, ValueError('source failed')):
            callback = Mock()
            source = DualSource(SimpleNamespace(browser_control=SimpleNamespace(show_workbench=callback)))
            fn = Mock(side_effect=error, return_value={'orders':[]})
            try:source.run_browser_task(fn)
            except ValueError:pass
            callback.assert_called_once_with('aov-monitor')
            bridge = BrowserBridge.__new__(BrowserBridge)
            bridge.return_to_workbench = Mock()
            bridge._read_source = fn
            try:bridge._execute('collect')
            except ValueError:pass
            bridge.return_to_workbench.assert_called_once_with()

