"""Cycle-only orders, immutable records, local comparison and alarm boundaries."""
import asyncio,json,tempfile,unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy
from order_pulse.aov_monitor.card_store import CardStore
from order_pulse.aov_monitor.cards import blank_shop,migrate_config
from order_pulse.aov_monitor.domain import calculate,transition,instant
from order_pulse.aov_monitor.service import Monitor,slot_at
from order_pulse.aov_monitor.cycle import order_identity
from tests.test_aov_cards import AT,CAT,TMS,MS,STORES

# Live cycle-only collection is superseded by test_aov_incremental.py.
# Keep domain compatibility for immutable historical cycle snapshots.
class CycleDomainTests(unittest.TestCase):
 def test_half_open_boundaries_zero_and_sample_floor(self):
  rows=[dict(order_key=str(i),paid_at=(AT-timedelta(hours=h)).isoformat(),amount_rmb=v,skus=['A']) for i,(h,v) in enumerate([(0,'100'),(2,'0'),(4,'5'),(4.1,'100')])]
  m=calculate(rows,AT.isoformat(),['A'],{'cycle':{'upper':'1'}},windows={'cycle':2})['cycle']
  self.assertEqual((m['current_count'],m['previous_count']),(1,1));self.assertEqual(m['aov_rmb'],'0.00');self.assertEqual(m['previous_aov_rmb'],'5.00');self.assertEqual(m['status'],'INSUFFICIENT');self.assertFalse(m['directions'])
 def test_old_rules_preserved_new_cycle_rules_unset_and_cosmetic_aliases_kept(self):
  old=dict(revision=1,enabled=True,shops=[dict(blank_shop(TMS[0]),hidden=True,hidden_skus=['A'],default_rules={'24h':{'upper':'55'},'72h':{'drop_pct':'10'}},products=[])])
  c=migrate_config(old,TMS)
  self.assertEqual(c['collection_mode'],'ROLLING_INCREMENTAL');self.assertEqual(c['cycle_hours'],2);self.assertEqual(c['shops'][0]['default_rules']['24h']['upper'],'55');self.assertTrue(c['shops'][0]['hidden'])
 def test_consecutive_respects_configured_cycle(self):
  metric={'status':'ABNORMAL','directions':['HIGH'],'detection_interval_hours':4}
  s,e=transition(None,metric,AT.isoformat(),True,'four');self.assertIsNone(e)
  s,e=transition(s,metric,(AT+timedelta(hours=4)).isoformat(),True,'four');self.assertEqual(e,'ABNORMAL')
