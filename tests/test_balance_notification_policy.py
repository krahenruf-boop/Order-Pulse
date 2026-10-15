import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import APIRouter
from fastapi.testclient import TestClient
from order_pulse.balance.engine import Engine
from order_pulse.balance.store import Store, now_iso, validate_settings
from order_pulse.server import create_app


class BalanceNotificationPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        settings=self.store.settings()
        settings.update(paused=False,setup_complete=True,currency_confirmed=True,
                        source_timezone_confirmed=True,account_scope_confirmed=True)
        self.store.set('settings',settings)
        self.cash='100.00';self.collected=self.collection()
        self.store.ingest(self.collected[0],self.collected[1])
        self.store.update_accounts([{'asset_id':'900000000000000001','enabled':True,
            'include_summary':True,'alert_enabled':True,'threshold':'50.00','recovery_enabled':True}])
        self.store.set('collection',dict(self.collected[1],status='success'))
        self.store.set('last_success',self.collected[1]['collected_at'])
        self.bridge=SimpleNamespace(collect=AsyncMock(side_effect=lambda:self.collection()))
        secrets=SimpleNamespace(status=lambda:{'webhook_configured':True},
            get=lambda:{'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/test'})
        self.engine=Engine(self.store,self.bridge,secrets)
        self.client_patch=patch('order_pulse.balance.engine.FeishuClient')
        self.client=self.client_patch.start()
        self.client.return_value.send=AsyncMock(return_value={'status':'success','reason':'fake code=0','attempts':1})
        self.settings=copy.deepcopy(settings)

    def collection(self):
        stamp=now_iso()
        return ({'accounts':[{'asset_id':'900000000000000001','source_name':'测试账户',
            'cash_balance':self.cash,'grant_balance':'3.45','balance_updated_at':stamp}], 'warnings':[]},
            {'collected_at':stamp,'source_updated_at':stamp,'data_source':'json','refresh_mode':'snapshot'},
            {'page_count':1,'data_source':'json','request_count':1})

    def tearDown(self):
        self.client_patch.stop();self.store.close();self.tmp.cleanup()

    async def cycle(self,summary=False,alerts=False,trigger='periodic'):
        await self.engine._run_cycle({'summary':summary,'alerts':alerts,'slots':[],'triggers':{trigger}})

    async def test_successful_scheduled_summary_still_sends(self):
        await self.cycle(summary=True)
        self.client.return_value.send.assert_awaited_once()
        self.assertEqual(self.store.events()[0]['kind'],'summary')
        self.assertEqual(self.store.events()[0]['status'],'success')

    async def test_low_balance_alert_still_sends(self):
        self.cash='10.00';await self.cycle(alerts=True)
        self.client.return_value.send.assert_awaited_once()
        self.assertEqual(self.store.events()[0]['kind'],'alert')

    async def test_balance_recovery_still_sends_without_system_recovery(self):
        self.cash='10.00';await self.cycle(alerts=True)
        self.client.return_value.send.reset_mock()
        self.engine.incident('collection','SOURCE_TIMEOUT')
        self.cash='100.00';await self.cycle(alerts=True)
        self.client.return_value.send.assert_awaited_once()
        self.assertEqual(self.store.events()[0]['kind'],'recovery')
        self.assertFalse(any(e['kind'] in ('failure','system_recovery') for e in self.store.events()))

    async def test_manual_query_and_push_uses_successful_fresh_collection(self):
        self.cash='123.45'
        self.assertTrue(self.engine.request('push',summary=True));await self.engine.cycle
        self.client.return_value.send.assert_awaited_once()
        self.assertIn('123.45',self.client.return_value.send.call_args.args[0])
        self.assertTrue(self.store.events(include_payload=True)[0]['payload']['manual'])

    async def assert_failure_silent(self,message):
        before=self.store.accounts()[0];last=self.store.get('last_success')
        self.bridge.collect.side_effect=ValueError(message)
        with patch('order_pulse.balance.engine.render_template',side_effect=AssertionError('must not render')):
            await self.cycle(summary=True,alerts=True,trigger='push')
        self.client.assert_not_called();self.assertEqual(self.store.events(),[])
        self.assertEqual(self.store.get('last_error'),message)
        self.assertEqual(self.store.get('last_success'),last)
        self.assertEqual(self.engine.status()['data_status'],'failed')
        after=self.store.accounts()[0]
        self.assertEqual((after['cash_balance'],after['grant_balance']),(before['cash_balance'],before['grant_balance']))
        self.assertEqual(self.store.settings(),self.settings)

    async def test_source_timeout_does_not_send(self):await self.assert_failure_silent('SOURCE_TIMEOUT')
    async def test_http_520_does_not_send(self):await self.assert_failure_silent('HTTP 520')
    async def test_auth_failure_does_not_send(self):await self.assert_failure_silent('AUTH_REQUIRED')

    async def test_unavailable_invalid_data_and_missing_pages_do_not_send(self):
        for reason in ('SOURCE_UNAVAILABLE','HTTP_ERROR','DATA_INVALID','PAGINATION_FAILED','其他采集失败'):
            with self.subTest(reason=reason):await self.assert_failure_silent(reason)

    async def test_repeated_failed_cycles_are_silent_and_next_success_is_allowed(self):
        self.bridge.collect.side_effect=ValueError('SOURCE_TIMEOUT')
        await self.cycle(summary=True,alerts=True);await self.cycle(summary=True,alerts=True)
        self.client.assert_not_called();self.assertEqual(self.store.events(),[])
        failures=[x for x in self.store.logs() if x['level']=='error']
        self.assertEqual(len(failures),2);self.assertNotEqual(failures[0]['task_id'],failures[1]['task_id'])
        self.assertTrue(failures[0]['details']['retry'])
        self.assertFalse(failures[1]['details']['retry'])
        self.bridge.collect.side_effect=lambda:self.collection()
        await self.cycle(summary=True)
        self.client.return_value.send.assert_awaited_once()
        self.assertEqual(self.store.settings(),self.settings)

    async def test_failure_history_remains_visible_with_diagnostics(self):
        await self.assert_failure_silent('SOURCE_TIMEOUT')
        failure=next(x for x in self.store.logs() if x['level']=='error')
        for key in ('error_code','error_stage','safe_error_message','retry','elapsed_ms','started_at','ended_at'):
            self.assertIn(key,failure['details'])
        from order_pulse.service import Service
        service=Service(Path(self.tmp.name))
        client=TestClient(create_app(Path(self.tmp.name),service=service),base_url='http://127.0.0.1:8786')
        response=client.get('/api/balance/state')
        self.assertEqual(response.status_code,200)
        self.assertTrue(any(x['level']=='error' and x['details'].get('error_code')=='SOURCE_TIMEOUT' for x in response.json()['logs']))
        client.close()

    async def test_old_pending_fault_notifications_are_cancelled_without_rendering(self):
        for kind in ('failure','system_recovery'):self.store.event(kind,{'error':'old'})
        self.store.recover_sending()
        self.assertTrue(all(x['status']=='cancelled' for x in self.store.events()))
        for kind in ('failure','system_recovery'):self.store.event(kind,{'error':'new legacy event'})
        with patch('order_pulse.balance.engine.render_template',side_effect=AssertionError('legacy renderer')):
            for event in self.store.pending_events():self.assertEqual(self.engine.messages(event),[])
            await self.engine.dispatch()
        self.client.assert_not_called()
        self.assertTrue(all(x['status']=='cancelled' for x in self.store.events()))

    async def test_failed_collection_does_not_flush_previous_queued_summary(self):
        self.engine.queue_summary(manual=True)
        self.bridge.collect.side_effect=ValueError('SOURCE_TIMEOUT')
        await self.cycle(summary=True,trigger='push')
        self.client.assert_not_called()
        self.assertEqual(self.store.events()[0]['status'],'pending')

    def test_legacy_failure_template_is_ignored_and_active_templates_still_validated(self):
        settings=self.store.settings()
        for key in ('failure','failure_template','error_template'):settings['templates'][key]={'invalid':'{ignored'}
        settings['failure_template']='{old syntax';settings['error_template']=None
        validate_settings(settings)
        settings['templates']['alert']='{bad_field}'
        with self.assertRaises(ValueError):validate_settings(settings)

    def test_settings_ui_exposes_only_active_templates_and_omits_legacy_draft(self):
        source=(Path(__file__).parents[1]/'static/balance.js').read_text()
        self.assertNotIn('失败模板',source);self.assertNotIn('template_failure',source)
        for name in ('title','account','alert','recovery'):self.assertIn('name="template_'+name+'"',source)
        self.assertNotIn('templates:{...draft.templates,',source)
        self.assertIn('state.settings=balanceSettingsDraft(state.settings)',source)
        self.assertIn('templates:{title:f.template_title.value,account:f.template_account.value,alert:f.template_alert.value,recovery:f.template_recovery.value}',source)


if __name__=='__main__':unittest.main()
