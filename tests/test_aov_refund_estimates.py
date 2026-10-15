import unittest
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from order_pulse.aov_monitor import refund_estimates as refunds
from order_pulse.aov_monitor.service import render_message
from tests import test_aov_incremental as rolling
from tests.test_aov_cards import AT,MS,CAT,STORES,TMS


class DailyRefundTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=rolling.RollingIncrementalTests.asyncSetUp
    asyncTearDown=rolling.RollingIncrementalTests.asyncTearDown
    run_once=rolling.RollingIncrementalTests.run_once

    async def test_estimate_formula_uniqueness_and_partial_original_amount(self):
        # 2 shops * 20 paid orders. One 30-yuan full refund among total 440:
        # count -2.5%, amount -6.818%, AOV -4.43%. Partial does not affect AOV.
        self.orders=[dict(pid=str(2000+i),paid=AT-timedelta(hours=1),amount='30' if i==0 else '10') for i in range(20)]
        original=self.m.source.batch
        def capture(j,shops,target):
            if j['kind']=='refunds':
                return {'refunds':[dict(platform_order_id=str(pid),shop_id=MS[0]['id'],kind='refund',status='success',platform_status='RETURN_OR_REFUND_REQUEST_COMPLETE',pay_amount=pay,refund_amount=amount,currency='BRL') for pid,pay,amount in [(2000,'60','60'),(2001,'20','10')]]}
            data=original(j,shops,target)
            if j['kind']=='miaoshou':data['rows']+=deepcopy(data['rows'])
            return data
        self.m.source.batch=capture
        s=await self.run_once();estimate=s['refund_estimation']
        self.assertEqual(s['status'],'SUCCEEDED')
        self.assertEqual((estimate['sample_orders'],estimate['full_refunds'],estimate['partial_refunds']),(40,1,1))
        self.assertEqual((estimate['order_pct'],estimate['amount_pct'],estimate['aov_impact_pct']),('2.50','6.82','-4.43'))
        self.assertEqual(s['shop_cards'][0]['windows']['24h']['current_count'],19)
        self.assertEqual(s['shop_cards'][0]['windows']['24h']['current_total'],'190.00')
        self.clock[0]+=timedelta(hours=2);self.calls.clear();s=await self.run_once()
        self.assertTrue(s['refund_estimation']['reused']);self.assertEqual(s['refund_estimation']['observed_at'],estimate['observed_at'])
        self.assertEqual(s['shop_cards'][0]['windows']['24h']['current_count'],19)
        self.assertFalse(any(j['kind']=='refunds' for j in self.calls))
        self.assertTrue(s['shop_cards'][0]['diagnostics']['refund_estimation']['estimated'])

    async def test_expired_failed_sample_does_not_become_zero_or_recover_alert(self):
        self.refund=True
        first=await self.run_once();self.clock[0]+=timedelta(hours=24);self.fail_kind='refunds';self.calls.clear()
        s=await self.run_once()
        self.assertEqual(s['refund_estimation']['status'],'UNAVAILABLE')
        self.assertTrue(all(c['status']=='COMPLETE' and not c['alert_eligible'] for c in s['shop_cards']))
        self.assertTrue(any(j['kind']=='refunds' for j in self.calls));self.assertFalse(self.m.store.events())
        self.assertEqual(s['shop_cards'][0]['windows']['24h']['previous_count'],23,'expired sample must not resurrect a known full refund')
        self.assertEqual(self.m.store.snapshot(first['run_id'])['refund_estimation']['status'],'AVAILABLE')

    async def test_identity_change_and_small_sample_cannot_certify(self):
        await self.run_once();shops=deepcopy(self.m.store.config()['shops'])
        self.assertIsNotNone(refunds.latest(self.m.store,shops,AT.isoformat()))
        shops[0]['miaoshou_shop_id']='changed'
        self.assertIsNone(refunds.latest(self.m.store,shops,AT.isoformat()))
        self.assertIsNone(refunds.latest(self.m.store,self.m.store.config()['shops'],(AT+timedelta(hours=24)).isoformat()))

    async def test_small_sample_unavailable_and_unknown_is_not_no_refund(self):
        self.orders=self.orders[-2:];s=await self.run_once()
        self.assertEqual(s['refund_estimation']['status'],'INSUFFICIENT')
        self.assertIsNone(s['refund_estimation']['order_pct'],'zero observed orders must not imply zero refund rate')
        self.clock[0]+=timedelta(hours=8);s=await self.run_once()
        self.assertTrue(all(not c['alert_eligible'] for c in s['shop_cards']))

    async def test_estimated_auto_notifications_labeled_and_low_counts_still_ignored(self):
        await self.run_once();c=self.m.store.config();c['enabled']=True;c['shops'][0]['push']=True
        c['shops'][0]['default_rules']['24h']['upper']='1';self.m.store.save_cards(c,CAT,STORES)
        sent=[]
        async def send(e):sent.append(e)
        self.m.send=send
        self.clock[0]+=timedelta(hours=2);await self.run_once(automatic=True);self.assertFalse(sent)
        self.clock[0]+=timedelta(hours=2);s=await self.run_once(automatic=True)
        self.assertEqual(len(sent),2)
        for e in sent:
            self.assertTrue(e['refund_estimation']['estimated']);self.assertIn('非逐笔退款净额',render_message(e))
        self.clock[0]+=timedelta(hours=2);await self.run_once(automatic=True);self.assertEqual(len(sent),2)


class ImpactTests(unittest.TestCase):
    def test_uniform_refunds_do_not_imply_same_aov_decline(self):
        sample=dict(status='AVAILABLE',order_rate='.02',amount_rate='.02',order_pct='2.00',amount_pct='2.00',aov_impact_pct='0.00',sample_orders=100,sample_shops=2,observed_at=AT.isoformat())
        out=refunds.impact(dict(current_count=100,current_total='5000'),sample)
        self.assertEqual(out['expected_refund_orders'],'2.00');self.assertEqual(out['expected_refund_amount_rmb'],'100.00');self.assertEqual(out['aov_impact_pct'],'0.00')
