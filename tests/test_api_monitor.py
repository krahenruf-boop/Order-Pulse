import asyncio,sys,tempfile,unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from pathlib import Path
from fastapi.testclient import TestClient
from order_pulse.storage import Repository
from order_pulse.api_monitor.service import Monitor,friendly_error
from order_pulse.api_monitor.domain import reading,ratio
from order_pulse.server import create_app

class QuotaFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=datetime(2026,10,20,1,tzinfo=timezone.utc);self.sent=[]
        async def send(text,event):self.sent.append((text,event));return {'status':'success'}
        self.repo=Repository(self.tmp.name,role='finance');self.m=Monitor(SimpleNamespace(repo=self.repo),clock=lambda:self.now,sender=send)
    async def asyncTearDown(self):
        await self.m.shutdown();self.tmp.cleanup()
    def ds(self,remaining='100',identity='account'):return dict(identity=identity,remaining=remaining,unit='CNY')
    def el(self,remaining='15',reset='10月21日'):return dict(identity='email · workspace',remaining=remaining,total='100',used=str(100-int(remaining)),unit='Credits',reset_label=reset)
class QuotaTests(QuotaFixture):
    async def test_baseline_zero_boundary_recovery_restart(self):
        self.m.accept('deepseek',self.ds('0'));self.assertIsNone(self.m.get('deepseek')['baseline']);self.assertEqual(self.sent,[])
        self.m.accept('deepseek',self.ds());self.assertEqual(self.m.get('deepseek')['baseline'],'100')
        self.m.accept('deepseek',self.ds('15'));await self.m.dispatch();self.assertEqual(len(self.sent),1);self.assertFalse(self.sent[0][0].startswith('\n'))
        self.m.accept('deepseek',self.ds('0'));await self.m.dispatch();self.assertEqual(len(self.sent),1)
        m=Monitor(SimpleNamespace(repo=self.repo),clock=lambda:self.now,sender=self.m.sender);m.accept('deepseek',self.ds('16'));await m.dispatch();self.assertEqual(len(self.sent),2)
        m.accept('deepseek',self.ds('200'));self.assertEqual(m.get('deepseek')['baseline'],'100')
    async def test_identity_currency_failure_preserves_old(self):
        self.m.accept('deepseek',self.ds())
        for value in [self.ds('10','other'),dict(self.ds(),unit='Credits'),self.ds('NaN')]:
            with self.assertRaises(ValueError):self.m.accept('deepseek',value)
        self.assertEqual(self.m.get('deepseek')['last']['remaining'],'100')
    async def test_cycle_reset_native_units(self):
        self.m.accept('elevenlabs',self.el());await self.m.dispatch();self.assertEqual(len(self.sent),1)
        self.m.accept('elevenlabs',self.el('100','11月21日'));await self.m.dispatch();self.assertEqual(len(self.sent),2)
        self.assertEqual(self.m.get('elevenlabs')['baseline'],None)
        with self.assertRaises(ValueError):reading('elevenlabs',dict(self.el(),used='99'))
    async def test_fault_immediate_6h_and_26h_stale(self):
        self.m.accept('deepseek',self.ds());c=self.m.get('deepseek');c['last_day']=self.now.date().isoformat()
        with self.repo.db() as d:self.m.put(d,'deepseek',c)
        # Disable the unused provider to keep this test free of browser actions.
        await self.m.settings('elevenlabs',{'enabled':False})
        self.now+=timedelta(hours=25);c=self.m.get('deepseek');c['last_day']=self.now.astimezone(__import__('order_pulse.api_monitor.domain',fromlist=['TZ']).TZ).date().isoformat()
        with self.repo.db() as d:self.m.put(d,'deepseek',c)
        await self.m.tick();self.assertEqual(len(self.sent),0)
        self.now+=timedelta(hours=1);await self.m.tick();self.assertEqual(len(self.sent),1)
        self.now+=timedelta(hours=6);await self.m.tick();self.assertEqual(len(self.sent),2)
        self.m.accept('deepseek',self.ds());await self.m.dispatch();self.assertEqual(len(self.sent),3)
    async def test_no_duplicate_same_day_and_failed_read_keeps_last(self):
        await self.m.settings('elevenlabs',{'enabled':False});calls=[]
        def collect(pid):calls.append(pid);return self.ds()
        self.m.collector=collect
        await self.m.tick();await asyncio.gather(*list(self.m.workers.values()));await asyncio.sleep(0)
        await self.m.tick();self.assertEqual(calls,['deepseek'])
        def fail(pid):raise ValueError('登录失效')
        self.m.collector=fail;await self.m.check('deepseek');self.assertEqual(self.m.get('deepseek')['last']['remaining'],'100')
        await self.m.dispatch();self.assertEqual(len(self.sent),1)
    async def test_crashed_outbox_not_replayed(self):
        self.m.accept('deepseek',self.ds());self.m.accept('deepseek',self.ds('15'))
        with self.repo.db() as d:d.execute("UPDATE api_monitor_outbox SET status='sending'")
        # Startup recovery only; stop its timer before it can make source requests.
        await self.m.startup();self.m.timer.cancel();await asyncio.gather(self.m.timer,return_exceptions=True)
        await self.m.dispatch();self.assertEqual(self.sent,[])

class IsolationTests(unittest.TestCase):
    def test_routes_schema_static_isolated(self):
        for role,owned,other,port in [('finance','balance','aov-monitor',8786),('materials','aov-monitor','balance',8797)]:
            with tempfile.TemporaryDirectory() as root:
                app=create_app(root,role=role,port=port);client=TestClient(app,base_url=f'http://127.0.0.1:{port}')
                self.assertEqual(client.get(f'/api/{owned}/state').status_code,200)
                self.assertEqual(client.get(f'/api/{other}/state').status_code,404)
                forbidden='aov-monitor.js' if role=='finance' else 'balance.js'
                self.assertEqual(client.get('/static/'+forbidden).status_code,404)
                with app.state.service.repo.db() as d:
                    tables=[r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                self.assertFalse(any(t.startswith('aov_monitor_' if role=='finance' else 'balance_') for t in tables))
                state=client.get('/api/state').json();csrf=state['csrf']
                path='/api/api-monitor/check/deepseek' if role=='finance' else '/api/aov-monitor/detect'
                self.assertEqual(client.post(path,json={}).status_code,403)
                self.assertEqual(client.get('/api/state',headers={'Origin':'https://evil.test'}).status_code,403)

class RuleChangeTests(QuotaFixture):
    async def test_lowering_threshold_does_not_fake_recovery(self):
        self.m.accept('deepseek',self.ds());self.m.accept('deepseek',self.ds('15'));await self.m.dispatch()
        await self.m.settings('deepseek',{'threshold':'10'})
        self.m.accept('deepseek',self.ds('15'));await self.m.dispatch();self.assertEqual(len(self.sent),1)
        self.m.accept('deepseek',self.ds('16'));await self.m.dispatch();self.assertEqual(len(self.sent),2)
    async def test_enabling_push_notifies_current_low_once(self):
        await self.m.settings('elevenlabs',{'push':False});self.m.accept('elevenlabs',self.el());await self.m.dispatch();self.assertEqual(self.sent,[])
        await self.m.settings('elevenlabs',{'push':True});self.m.accept('elevenlabs',self.el());await self.m.dispatch();self.assertEqual(len(self.sent),1)
        self.m.accept('elevenlabs',self.el());await self.m.dispatch();self.assertEqual(len(self.sent),1)

class HumanActionTests(QuotaFixture):
    async def test_waiting_login_blocks_other_automatic_browser_jobs_until_manual_refresh(self):
        import threading
        from unittest.mock import Mock,patch
        self.m.host.balance_browser_space=SimpleNamespace(get=lambda:{'verified':True},lock=threading.Lock())
        self.m.host.browser_control=SimpleNamespace(show_workbench=Mock())
        self.repo.set('finance_user_action',{'provider':'deepseek','at':self.now.isoformat()})
        with patch('order_pulse.api_monitor.service.execute') as run:
            with self.assertRaisesRegex(ValueError,'等待本人'):self.m.collect('elevenlabs')
            run.assert_not_called()
        self.assertFalse(self.m.host.balance_browser_space.lock.locked())
        self.m.collector=lambda _:self.ds()
        await self.m.start('deepseek');await asyncio.gather(*list(self.m.workers.values()))
        self.assertIsNone(self.repo.get('finance_user_action'))
        self.assertEqual(self.m.get('deepseek')['last']['remaining'],'100')

class BrowserFaultTests(QuotaFixture):
    async def test_browser_timeout_preserves_reading_and_recovers_once(self):
        self.m.accept('elevenlabs',self.el('80'))
        raw='page.goto timed out after 15000ms waiting for load; navigation committed at "https://elevenlabs.io/app/settings" with document.readyState="interactive".'
        def fail(_):raise ValueError(raw)
        self.m.collector=fail
        await self.m.check('elevenlabs');await self.m.check('elevenlabs')
        self.assertEqual(self.m.get('elevenlabs')['last']['remaining'],'80')
        self.assertEqual(len(self.sent),1)
        self.assertNotIn('page.goto',self.sent[0][0])
        self.assertIn('读取超时',self.m.state()['providers'][1]['fault']['error'])
        failed=[r for r in self.m.history('elevenlabs')['readings'] if not r['ok']]
        self.assertTrue(failed)
        self.assertTrue(all(r['error']==friendly_error(raw) for r in failed))
        self.m.collector=lambda _:self.el('79')
        await self.m.check('elevenlabs');await self.m.check('elevenlabs')
        self.assertIsNone(self.m.get('elevenlabs')['fault'])
        self.assertEqual(len(self.sent),2,'only one fault recovery notification')
        self.assertEqual(self.m.get('elevenlabs')['last']['remaining'],'79')

    async def test_old_diagnostics_are_presented_without_rewriting_history(self):
        import json
        raw='page.goto timed out after 15000ms waiting for load; document.readyState="interactive"'
        self.m.accept('elevenlabs',self.el('80'))
        c=self.m.get('elevenlabs');c['fault']=dict(error=raw,episode='old',since=self.now.isoformat())
        with self.repo.db() as d:
            self.m.put(d,'elevenlabs',c)
            d.execute('INSERT INTO api_monitor_history VALUES(?,?,?,?)',('old-fault','elevenlabs',self.now.isoformat(),json.dumps(dict(ok=False,error=raw))))
        self.assertEqual(self.m.state()['providers'][1]['fault']['error'],friendly_error(raw))
        self.assertTrue(all('page.goto' not in r.get('error','') for r in self.m.history('elevenlabs')['readings']))
        with self.repo.db() as d:
            saved=json.loads(d.execute('SELECT payload FROM api_monitor_history WHERE id=?',('old-fault',)).fetchone()[0])
        self.assertEqual(saved['error'],raw)
        for raw in ('net::ERR_CONNECTION_RESET','page.evaluate failed: 页面执行失败','WAITING_CONNECTION'):
            self.assertIn('供应商页面',friendly_error(raw))
        self.assertEqual(friendly_error('账户或工作区已变化，请核对后重新绑定'),'账户或工作区已变化，请核对后重新绑定')
