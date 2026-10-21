import asyncio
import json
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

from order_pulse.api_monitor.service import Monitor
from tests.test_api_monitor import QuotaFixture


class LowQuotaScheduleTests(QuotaFixture):
    async def settle(self,monitor=None):
        monitor=monitor or self.m
        await asyncio.gather(*list(monitor.workers.values()))
        await asyncio.sleep(0)

    def provider(self,pid='elevenlabs',monitor=None):
        return next(c for c in (monitor or self.m).state()['providers'] if c['id']==pid)

    async def test_hour_boundary_single_worker_recovery_returns_to_daily(self):
        await self.m.settings('deepseek',{'enabled':False})
        start=self.now;calls=[];values=iter(['14','16'])
        self.m.collector=lambda pid:(calls.append(pid) or self.el(next(values)))
        self.m.accept('elevenlabs',self.el('15'));await self.m.dispatch()
        self.assertEqual(self.provider()['schedule_mode'],'low_recheck')
        self.assertEqual(datetime.fromisoformat(self.provider()['next_at']),start+timedelta(hours=1))
        self.now=start+timedelta(minutes=59,seconds=59);await self.m.tick();self.assertEqual(calls,[])
        self.now+=timedelta(seconds=1);await self.m.tick();await self.m.tick();await self.settle()
        self.assertEqual(calls,['elevenlabs']);self.assertEqual(len(self.sent),1,'persistent low does not repeat alerts')
        self.now+=timedelta(hours=1);await self.m.tick();await self.settle()
        self.assertEqual(len(self.sent),2,'one actual recovery notification')
        self.assertEqual(self.provider()['schedule_mode'],'daily')
        self.assertEqual(datetime.fromisoformat(self.provider()['next_at']),start+timedelta(days=1))
        self.now+=timedelta(hours=1);await self.m.tick();self.assertEqual(len(calls),2)

    async def test_failure_waits_full_interval_and_preserves_old_quota(self):
        await self.m.settings('deepseek',{'enabled':False})
        self.m.accept('elevenlabs',self.el('14'));await self.m.dispatch();calls=[]
        def fail(pid):calls.append(pid);raise ValueError('网络暂不可用')
        self.m.collector=fail
        self.now+=timedelta(hours=1);await self.m.tick();await self.settle()
        self.assertEqual(self.m.get('elevenlabs')['last']['remaining'],'14')
        self.assertEqual(datetime.fromisoformat(self.provider()['next_at']),self.now+timedelta(hours=1))
        self.now+=timedelta(minutes=59);await self.m.tick();self.assertEqual(len(calls),1)
        self.now+=timedelta(minutes=1);await self.m.tick();await self.settle()
        self.assertEqual(len(calls),2);self.assertEqual(len(self.sent),2,'low and fault each notify once')

    async def test_platform_intervals_are_independent_and_validated(self):
        self.m.accept('deepseek',self.ds());self.m.accept('deepseek',self.ds('15'))
        self.m.accept('elevenlabs',self.el('15'));calls=[]
        self.m.collector=lambda pid:(calls.append(pid) or (self.ds('14') if pid=='deepseek' else self.el('14')))
        await self.m.settings('deepseek',{'low_interval_hours':2})
        await self.m.settings('elevenlabs',{'low_interval_hours':3})
        for value in (0,25,True,False,1.5,'1',None):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'1–24'):
                await self.m.settings('elevenlabs',{'low_interval_hours':value})
        self.now+=timedelta(hours=1);await self.m.tick();self.assertEqual(calls,[])
        self.now+=timedelta(hours=1);await self.m.tick();await self.settle();self.assertEqual(calls,['deepseek'])
        self.now+=timedelta(hours=1);await self.m.tick();await self.settle();self.assertEqual(calls,['deepseek','elevenlabs'])

    async def test_manual_read_restarts_interval_and_push_switch_does_not_stop_checks(self):
        await self.m.settings('deepseek',{'enabled':False})
        await self.m.settings('elevenlabs',{'push':False})
        self.m.accept('elevenlabs',self.el('15'));calls=[]
        self.m.collector=lambda pid:(calls.append(pid) or self.el('14'))
        self.now+=timedelta(minutes=30);await self.m.start('elevenlabs');await self.settle()
        self.now+=timedelta(minutes=30);await self.m.tick();self.assertEqual(len(calls),1)
        self.now+=timedelta(minutes=30);await self.m.tick();await self.settle();self.assertEqual(len(calls),2)
        self.assertEqual(self.sent,[])
        await self.m.settings('elevenlabs',{'enabled':False})
        self.assertIsNone(self.provider()['next_at'])
        self.now+=timedelta(hours=3);await self.m.tick();self.assertEqual(len(calls),2)

    async def test_restart_retains_due_time_and_only_catches_up_once(self):
        await self.m.settings('deepseek',{'enabled':False})
        self.m.accept('elevenlabs',self.el('14'));await self.m.dispatch();initial=self.now;calls=[]
        def restart():
            return Monitor(SimpleNamespace(repo=self.repo),clock=lambda:self.now,
                           collector=lambda pid:(calls.append(pid) or self.el('13')),sender=self.m.sender)
        self.now+=timedelta(minutes=40);m=restart()
        await m.tick();self.assertEqual(calls,[])
        self.assertEqual(datetime.fromisoformat(self.provider(monitor=m)['next_at']),initial+timedelta(hours=1))
        self.now+=timedelta(hours=10);await m.tick();await m.tick();await self.settle(m)
        self.assertEqual(calls,['elevenlabs']);self.assertEqual(len(self.sent),1)
        m=restart();await m.tick();self.assertEqual(len(calls),1)
        self.assertEqual(datetime.fromisoformat(self.provider(monitor=m)['next_at']),self.now+timedelta(hours=1))

    async def test_migration_adds_defaults_using_latest_attempt_without_changing_history(self):
        self.m.accept('elevenlabs',self.el('14'));c=self.m.get('elevenlabs')
        c.pop('low_interval_hours');c.pop('last_check_at')
        failed_at=(self.now+timedelta(minutes=20)).isoformat();payload=json.dumps(dict(ok=False,error='网络暂不可用'))
        with self.repo.db() as d:
            self.m.put(d,'elevenlabs',c)
            d.execute('INSERT INTO api_monitor_history VALUES(?,?,?,?)',('latest-failure','elevenlabs',failed_at,payload))
            before=d.execute('SELECT * FROM api_monitor_history ORDER BY id').fetchall()
        m=Monitor(SimpleNamespace(repo=self.repo),clock=lambda:self.now,sender=self.m.sender)
        saved=m.get('elevenlabs');self.assertEqual(saved['low_interval_hours'],1);self.assertEqual(saved['last_check_at'],failed_at)
        self.assertEqual(saved['last'],c['last']);self.assertEqual(saved['low_episode'],c['low_episode'])
        with self.repo.db() as d:self.assertEqual(d.execute('SELECT * FROM api_monitor_history ORDER BY id').fetchall(),before)

    async def test_interval_and_threshold_edits_do_not_invent_recovery(self):
        await self.m.settings('deepseek',{'enabled':False})
        self.m.accept('elevenlabs',self.el('15'));await self.m.dispatch();episode=self.m.get('elevenlabs')['low_episode']
        await self.m.settings('elevenlabs',{'low_interval_hours':2})
        self.now+=timedelta(hours=1)
        await self.m.settings('elevenlabs',{'low_interval_hours':1,'threshold':'10'})
        self.assertEqual(self.m.get('elevenlabs')['low_episode'],episode)
        self.m.collector=lambda _:self.el('15')
        await self.m.tick();await self.settle()
        self.assertEqual(len(self.sent),1);self.assertEqual(self.provider()['schedule_mode'],'low_recheck')
        self.now+=timedelta(hours=1);self.m.collector=lambda _:self.el('16')
        await self.m.tick();await self.settle()
        self.assertEqual(len(self.sent),2);self.assertEqual(self.provider()['schedule_mode'],'daily')

    async def test_unknown_baseline_stays_daily_and_verified_zero_rechecks(self):
        self.m.accept('deepseek',self.ds('0'));self.m.accept('elevenlabs',self.el('0'))
        self.assertEqual(self.provider('deepseek')['schedule_mode'],'daily')
        self.assertEqual(self.provider()['schedule_mode'],'low_recheck')

    async def test_earlier_daily_check_counts_as_the_next_recheck(self):
        await self.m.settings('deepseek',{'enabled':False})
        self.now=self.now.replace(hour=15,minute=30)  # 23:30 Beijing
        await self.m.settings('elevenlabs',{'time':'00:00','low_interval_hours':4})
        self.m.accept('elevenlabs',self.el('14'));calls=[]
        self.m.collector=lambda pid:(calls.append(pid) or self.el('13'))
        self.assertEqual(datetime.fromisoformat(self.provider()['next_at']),self.now+timedelta(minutes=30))
        self.now+=timedelta(minutes=30);await self.m.tick();await self.settle()
        self.assertEqual(calls,['elevenlabs'])
        self.assertEqual(datetime.fromisoformat(self.provider()['next_at']),self.now+timedelta(hours=4))
