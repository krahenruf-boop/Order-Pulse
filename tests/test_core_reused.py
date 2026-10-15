from copy import deepcopy
from datetime import datetime,timezone
import unittest
from order_pulse.tms_core.sales_domain import *
from order_pulse.tms_core.monitor_domain import parse_collection
NOW="2026-09-21T07:00:00+00:00"

def binding():
 return {'space_id':1,'page_label':'p1','account':{'employee_id':'11','supplier_id':'22','customer_id':'33'},
         'stores':[{'shop_id':'44','name':'Synthetic shop','platform_id':104,'company_id':'33'}]}

def rows(n=1):
 return [{'id':str(i+1),'companyId':33,'supplierId':22,'shopId':44,'platformId':104,'orderTime':1789966800000,
          'stockSku':'SKU','orderId':'1723456789012345678','platformOrderId':'1723456789012345678','originalOrderId':'1723456789012345678','isSplit':2,'isUnion':2} for i in range(n)]

def response(scope,records=None):
 records=deepcopy(rows() if records is None else records)
 batches=[]
 for partition in scope['partitions']:
  for sku in scope['skus']:
   q={'page':1,'pageSize':50,'stockSku':sku,'shopIdsList':[44],'keytype2':1 if partition=='current' else '1','orderStatus':0 if partition=='current' else 5}
   q['orderTimes' if partition=='current' else 'timeStr']=scope['source_ranges'][partition]
   if partition=='current':q.update(skutype='5',platformId=104)
   batches.append({'partition':partition,'sku':sku,'pages':[{'request':q,'http_status':200,'business_code':'200','total':len(records),'rows':records}],
                   'end_confirmed':True,'recheck_total':len(records),'recheck_rows':records})
 return {'account':scope['account'],'batches':batches}

def observation(key='row1',ref='586184211313296393',sku='SKU',qty=1,partition='current',shop='44'):
 return dict(record_key=key,source_row_id=key,paid_at='2026-09-21T05:00:00+00:00',shop_id=shop,
             partition=partition,matched_queries=[sku],source_sku_text=sku,
             order_references={'originalOrderId':ref,'platformOrderId':ref},
             source_items=[{'stockSku':sku,'platformQuantity':qty,'productNum':qty}])

class OperationalRules(unittest.TestCase):
 def setUp(self):self.scope=sales_scope(dict(name='Test',skus=['SKU','SKU*2'],window_mode='today',revision=1),dict(binding(),revision=1),NOW)
 def metrics(self,records,history=()):return observed_metrics(records,self.scope,history)
 def test_multi_sku_duplicate_sources_one_order_and_quantity(self):
  a=observation();b=observation('r2',sku='SKU*2',qty=2);c=deepcopy(a);c.update(record_key='r3',partition='completed')
  m,o=self.metrics([a,b,c]);self.assertEqual(m['product_order_count'],1);self.assertEqual(m['duplicate_record_count'],2)
  self.assertEqual(m['skus']['SKU']['sku_quantity'],1);self.assertEqual(m['skus']['SKU*2']['sku_quantity'],2)
  self.assertEqual(sum(x['sku_order_count'] for x in m['skus'].values()),2);self.assertFalse(o[0]['quantity_estimated'])
 def test_literal_alias_equality_without_parent_or_task_inference(self):
  a=observation();a['order_references']={'platformOrderId':'111'}
  b=observation('b');b['order_references']={'originalOrderId':'111'}
  c=observation('c',ref='222');c.update(is_split_raw=1,is_union_raw=1)
  self.assertEqual(self.metrics([a,b,c])[0]['product_order_count'],2)
 def test_missing_explicit_id_counts_source_row_with_flag(self):
  a=observation(ref=None);b=observation('b',ref=None)
  m,o=self.metrics([a,b]);self.assertEqual(m['product_order_count'],2);self.assertEqual(m['identity_estimated_order_count'],2)
 def test_quantity_priority_fallback_and_suffix_never_multiplied(self):
  for fields,expected in [({'platformQuantity':2,'productNum':4},(2,False,'platformQuantity')),
    ({'platformQuantity':None,'productNum':'3'},(3,False,'productNum')),
    ({'platformQuantity':'bad','productNum':2},(2,False,'productNum')),
    ({'platformQuantity':True,'productNum':None},(1,True,'FALLBACK_ONE')),
    ({'platformQuantity':'NaN','productNum':-2},(1,True,'FALLBACK_ONE')),
    ({'platformQuantity':1.5,'productNum':0},(1,True,'FALLBACK_ONE'))]:
   self.assertEqual(observed_quantity(dict(fields,stockSku='SKU*3')),expected)
  m,o=self.metrics([observation(sku='SKU*2',qty=None)])
  self.assertEqual(m['skus']['SKU*2']['sku_quantity'],1);self.assertTrue(o[0]['quantity_estimated'])
 def test_multiple_direct_lines_summed_once_per_order_copy(self):
  a=observation();a['source_items']+=[{'stockSku':'SKU','platformQuantity':2}]
  b=deepcopy(a);b.update(record_key='b',partition='completed')
  self.assertEqual(self.metrics([a,b])[0]['skus']['SKU']['sku_quantity'],3)
 def test_disagreeing_copies_disclosed_not_summed(self):
  a=observation(qty=2);b=observation('b',qty=3,partition='completed')
  m,o=self.metrics([a,b]);self.assertEqual(m['skus']['SKU']['sku_quantity'],3)
  self.assertEqual(m['skus']['SKU']['quantity_conflict_order_count'],1)
 def test_known_quantity_preferred_over_estimate(self):
  a=observation(qty=2);b=observation('b',qty=None,partition='completed')
  m,o=self.metrics([a,b]);self.assertEqual(m['skus']['SKU']['sku_quantity'],2);self.assertFalse(o[0]['quantity_estimated'])
 def test_empty_complete_is_zero(self):
  m,o=self.metrics([]);self.assertEqual(m['observed_paid_orders'],0);self.assertEqual(m['skus']['SKU']['sku_quantity'],0)
 def test_deterministic_and_shops_separate(self):
  self.scope['shop_ids']+=['45'];rows=[observation(),observation('b',shop='45')]
  self.assertEqual(self.metrics(rows),self.metrics(rows[::-1]));self.assertEqual(self.metrics(rows)[0]['product_order_count'],2)
 def test_invalid_time_exact_sku_and_shop_never_counted(self):
  for r in [dict(observation(),paid_at=self.scope['end']),dict(observation(),shop_id='other'),dict(observation(),matched_queries=['SKU-similar'])]:
   with self.assertRaises(ValueError):self.metrics([r])
 def test_paid_conflict_same_order_is_failure(self):
  with self.assertRaisesRegex(ValueError,'PAYMENT_IDENTITY_CONFLICT'):
   self.metrics([observation(),dict(observation('b'),paid_at='2026-09-21T06:00:00Z')])
 def test_late_order_paid_day_first_seen_and_cross_source_history(self):
  a=dict(observation(),first_seen_at='2026-09-22T07:00:00Z')
  old=dict(observation('older',partition='completed'),first_seen_at='2026-09-22T06:00:00Z',account_scope=digest(self.scope['account']))
  m,o=self.metrics([a],[old]);self.assertEqual(o[0]['first_seen_at'],old['first_seen_at']);self.assertEqual(o[0]['paid_at'],a['paid_at'])
  self.assertEqual(m['product_order_count'],1)
