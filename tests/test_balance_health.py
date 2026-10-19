import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from order_pulse.balance.engine import Engine
from order_pulse.balance.health import CollectionHealth
from order_pulse.balance.store import Store


class BalanceHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.now=datetime(2026,10,19,3,tzinfo=timezone.utc)
        settings=self.store.settings();settings.update(paused=False,setup_complete=True)
        self.store.set('settings',settings);self.store.set('last_success',self.now.isoformat())
        self.bridge=SimpleNamespace(collect=AsyncMock(side_effect=AssertionError('watchdog cannot collect')))
        self.secrets=SimpleNamespace(get=lambda:{'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/test'})
        self.engine=Engine(self.store,self.bridge,self.secrets)
        self.engine.health.clock=lambda:self.now
        self.patch=patch('order_pulse.balance.engine.FeishuClient')
        self.client=self.patch.start();self.send=AsyncMock(return_value={'status':'success','reason':'code=0','attempts':1})
        self.client.return_value.send=self.send

    def tearDown(self):
        self.patch.stop();self.store.close();self.tmp.cleanup()

    async def fault(self):
        self.engine.health.failure('Linkme 登录已过期','AUTH_REQUIRED')
        await self.engine.dispatch(health_only=True)

    async def test_immediate_six_hour_boundaries_and_persistent_deduplication(self):
        await self.fault();self.assertEqual(self.send.await_count,1)
        first=self.store.events()[0]['id']
        self.now+=timedelta(hours=5,minutes=59,seconds=59)
        await self.engine.health_tick();self.assertEqual(self.send.await_count,1)
        self.now+=timedelta(seconds=1)
        await self.engine.health_tick();self.assertEqual(self.send.await_count,2)
        self.assertIn('6 小时',self.send.call_args.args[0])
        self.engine=Engine(self.store,self.bridge,self.secrets);self.engine.health.clock=lambda:self.now
        for _ in range(3):await self.engine.health_tick()
        self.assertEqual(self.send.await_count,2)
        self.now+=timedelta(hours=6)
        await self.engine.health_tick();self.assertEqual(self.send.await_count,3)
        self.assertEqual(len({e['id'] for e in self.store.events()}),3)
        self.assertIn(first,{e['id'] for e in self.store.events()})

    async def test_watchdog_detects_no_collection_without_waiting_for_browser(self):
        self.now+=timedelta(hours=6)
        await self.engine.health_tick()
        self.assertEqual(self.send.await_count,1)
        self.assertEqual(self.store.events()[0]['kind'],'monitor_reminder')
        self.assertIn('6 小时',self.send.call_args.args[0])
        self.bridge.collect.assert_not_called()
        self.assertTrue(self.engine.status()['monitor_health']['active'])
        self.assertEqual(self.engine.status()['data_status'],'failed')

    async def test_no_initial_success_is_also_watched(self):
        self.store.set('last_success',None)
        await self.engine.health_tick();self.assertEqual(self.send.await_count,0)
        self.now+=timedelta(hours=6)
        await self.engine.health_tick();self.assertEqual(self.send.await_count,1)

    async def test_manual_recovery_once_and_never_resend_old_fault_after_recovery(self):
        await self.fault();self.now+=timedelta(minutes=5)
        self.store.set('last_success',self.now.isoformat())
        self.engine.health.success();await self.engine.dispatch(health_only=True)
        self.assertEqual(self.send.await_count,2)
        self.assertIn('已恢复',self.send.call_args.args[0])
        for _ in range(3):
            self.engine.health.success();await self.engine.health_tick()
        self.assertEqual(self.send.await_count,2)
        # A later outage starts a new episode immediately.
        await self.fault();self.assertEqual(self.send.await_count,3)

    async def test_recovery_for_an_unsent_incident_is_silent(self):
        self.engine.health.failure('无法连接','SOURCE_UNAVAILABLE')
        self.store.set('last_success',self.now.isoformat());self.engine.health.success()
        await self.engine.dispatch(health_only=True)
        self.send.assert_not_awaited()
        self.assertTrue(all(e['status']=='cancelled' for e in self.store.events()))

    async def test_definite_failure_retries_after_fifteen_minutes_with_same_id(self):
        self.send.return_value={'status':'failed','reason':'network unreachable','attempts':3}
        await self.fault();first=self.store.events()[0]
        self.assertEqual(first['attempts'],3)
        self.now+=timedelta(minutes=14,seconds=59)
        await self.engine.health_tick();self.assertEqual(self.send.await_count,1)
        self.now+=timedelta(seconds=1)
        self.send.return_value={'status':'success','reason':'code=0','attempts':1}
        await self.engine.health_tick();self.assertEqual(self.send.await_count,2)
        self.assertEqual(len(self.store.events()),1)
        self.assertEqual(self.store.events()[0]['id'],first['id'])
        self.assertEqual(self.store.events()[0]['status'],'success')

    async def test_uncertain_send_is_not_retried_and_pending_balance_is_not_flushed(self):
        self.store.event('summary',{'sentinel':'must not send'})
        self.send.return_value={'status':'uncertain','reason':'timeout','attempts':1}
        await self.fault();self.now+=timedelta(minutes=30)
        await self.engine.health_tick();self.assertEqual(self.send.await_count,1)
        summary=next(e for e in self.store.events() if e['kind']=='summary')
        self.assertEqual(summary['status'],'pending')

    async def test_pause_silences_faults_and_excludes_pause_from_duration(self):
        await self.fault()
        settings=self.store.settings();settings['paused']=True;self.store.set('settings',settings)
        await self.engine.health_tick()
        self.now+=timedelta(days=1)
        settings['paused']=False;self.store.set('settings',settings)
        await self.engine.health_tick();self.assertEqual(self.send.await_count,1)
        await self.fault();self.assertEqual(self.send.await_count,2)
        self.assertEqual(self.store.events()[0]['kind'],'monitor_fault')

    async def test_settings_edits_do_not_silence_health_incident(self):
        self.engine.health.failure('无法连接')
        self.store.set('settings_revision',99)
        await self.engine.dispatch(health_only=True)
        self.assertEqual(self.send.await_count,1)

    async def test_shutdown_finishes_even_if_transport_consumes_cancellation(self):
        from order_pulse.balance.module import BalanceModule
        module=BalanceModule(self.tmp.name,None)
        started=asyncio.Event()
        async def delivery():
            started.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:return # Feishu can return an uncertain outcome
        module.engine.health_tick=delivery
        await module.startup();await asyncio.wait_for(started.wait(),1)
        await asyncio.wait_for(module.shutdown(),1)
        self.assertIsNone(module.health_worker)

    async def test_pending_retries_survive_restart_but_uncertain_events_do_not_repeat(self):
        await self.fault()
        self.store.event('monitor_reminder',{'episode':'old'},event_id='in-flight')
        self.store.update_event('in-flight','sending','sending')
        self.store.recover_sending()
        self.assertEqual(next(e for e in self.store.events() if e['id']=='in-flight')['status'],'uncertain')
        self.engine.health=CollectionHealth(self.store,lambda:self.now)
        await self.engine.health_tick();self.assertEqual(self.send.await_count,1)


if __name__=='__main__':unittest.main()
