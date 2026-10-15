from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xlrd
from order_pulse.exports.model import ExportError, ExportSettings
from order_pulse.exports.slicing import Budget, CurrentOrdersExportAdapter, CompletedOrdersExportAdapter
from order_pulse.exports.parser import aggregate, aggregate_all, read_xls, identifier, BASE
from order_pulse.exports.temporary import TemporaryExports, OWNER
from order_pulse.exports.connector import ExportConnector
from order_pulse.exports.transport import EgoExportTransport

SCOPE={'start':'2026-09-22T00:00:00-03:00','end':'2026-09-22T04:00:00-03:00',
       'shop_ids':['1'],'skus':['HBYDK1','HBYDK1*2'],'partitions':['current','completed']}
ALL_ORDERS_SCOPE=dict(SCOPE,reconciliation_contract='CURRENT_FIRST_V1')
def row(key='1234567890123456789',sku='HBYDK1',quantity=1,amount='56.07'):
    return dict(order_key=key,order_number=key,shop_name='巴西 中文店铺',paid_at='2026-09-22T01:00:00-03:00',
                order_amount_rmb=amount,item=dict(stock_sku=sku,platform_sku='PT-'+sku,quantity=quantity,product_name='商品'))
def file(rows,hash='a'):
    return dict(file_hash=hash,rows=rows,evidence=dict(partition='current',rows=len(rows)))


class AggregationTests(unittest.TestCase):
    def metrics(self,files):return aggregate(files,SCOPE['skus'],SCOPE)[0]
    def test_two_skus_one_order_once_amount(self):
        m=self.metrics([file([row(),row(sku='HBYDK1*2')])])
        self.assertEqual(m['product_order_count'],1);self.assertEqual(m['sku_quantity'],2)
        self.assertEqual(m['amount_rmb'],'56.07');self.assertEqual(m['skus']['HBYDK1*2']['sku_quantity'],1)
    def test_identical_file_idempotency(self):
        f=file([row(),row(sku='HBYDK1*2')]);self.assertEqual(self.metrics([f,f]),self.metrics([f]))
    def test_different_file_same_rows_idempotency(self):
        self.assertEqual(self.metrics([file([row()]),file([row()],hash='b')])['sku_quantity'],1)
    def test_overlap_current_completed_and_retry(self):
        a=file([row(),row(key='another')]);b=file([row(key='another'),row(key='third')],hash='b')
        b['evidence']['partition']='completed';m=self.metrics([a,b,a,b]);self.assertEqual(m['product_order_count'],3);self.assertEqual(m['sku_quantity'],3)
    def test_identical_legal_item_occurrences_retained(self):
        m=self.metrics([file([row(),row()]),file([row(),row()],hash='b')]);self.assertEqual(m['sku_quantity'],2)
    def test_different_legal_sku_rows_across_files_retained(self):
        m=self.metrics([file([row()]),file([row(sku='HBYDK1*2')],hash='b')]);self.assertEqual(m['sku_quantity'],2)
    def test_changed_quantity_rejected(self):
        with self.assertRaisesRegex(ExportError,'EXPORT_ITEM_CONFLICT'):self.metrics([file([row()]),file([row(quantity=2)],hash='b')])
    def test_changed_name_not_a_second_purchase(self):
        other=row();other['item']['product_name']='变更名称'
        with self.assertRaisesRegex(ExportError,'EXPORT_ITEM_CONFLICT'):self.metrics([file([row()]),file([other],hash='b')])
    def test_changed_identical_item_multiplicity_is_ambiguous(self):
        with self.assertRaisesRegex(ExportError,'EXPORT_ITEM_CONFLICT'):self.metrics([file([row()]),file([row(),row()],hash='b')])
    def test_amount_conflict_null_not_first_max_or_sum(self):
        m=self.metrics([file([row(),row(sku='HBYDK1*2',amount='57')])]);self.assertIsNone(m['amount_rmb']);self.assertTrue(m['amount_conflict']);self.assertEqual(m['amount_note'],'金额数据异常')
    def test_cross_file_amount_conflict(self):
        self.assertIsNone(self.metrics([file([row()]),file([row(amount='90')],hash='b')])['amount_rmb'])
    def test_target_local_filter_and_non_target_whole_order_amount(self):
        m=self.metrics([file([row(),row(sku='other'),row(key='not-related',sku='other')])]);self.assertEqual(m['product_order_count'],1);self.assertEqual(m['sku_quantity'],1);self.assertEqual(m['amount_rmb'],'56.07')
    def test_exact_sku_no_substring(self):
        self.assertEqual(self.metrics([file([row(sku='HBYDK1*3')])])['product_order_count'],0)
    def test_sort_stores_and_chinese(self):
        rows=[row(),row(key='b'),dict(row(key='c'),shop_name='另一店')];m=self.metrics([file(rows)])
        self.assertEqual(list(m['stores']),['巴西 中文店铺','另一店'])
    def test_order_shop_change_fails(self):
        with self.assertRaisesRegex(ExportError,'EXPORT_ORDER_CONFLICT'):self.metrics([file([row(),dict(row(),shop_name='other')])])
    def test_decimal_amounts(self):
        self.assertEqual(self.metrics([file([row(amount='0.1'),row(key='b',amount='0.2')])])['amount_rmb'],'0.3')
    def test_no_unverified_brl(self):
        self.assertIsNone(self.metrics([file([row()])])['amount_brl'])
    def test_empty_full_export_valid_zero(self):
        self.assertEqual(self.metrics([])['product_order_count'],0)
    def test_long_text_id_preserved(self):
        _,o,_=aggregate([file([row()])],SCOPE['skus'],SCOPE);self.assertEqual(o[0]['order_key'],'1234567890123456789')


class Cell:
    def __init__(self,value,ctype=xlrd.XL_CELL_TEXT):self.value,self.ctype=value,ctype
class Sheet:
    def __init__(self,headers,rows):self.headers=headers;self.rows=[headers]+rows;self.nrows=len(self.rows);self.ncols=len(headers)
    def cell_value(self,r,c):return self.rows[r][c].value if isinstance(self.rows[r][c],Cell) else self.rows[r][c]
    def cell(self,r,c):return self.rows[r][c] if isinstance(self.rows[r][c],Cell) else Cell(self.rows[r][c])
class Book:
    nsheets=1
    def __init__(self,s):self.s=s
    def sheet_by_index(self,i):return self.s
    def release_resources(self):pass


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.p=Path(self.t.name)/'fixture.xls';self.p.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1'))
        self.headers=list(BASE)+['币种','平台ID'];self.values=['56.07','2026-09-22 01:00','巴西 中文店铺','1234567890123456789','1234567890123456789','','HBYDK1*2','PT',1,'商品','RMB','tiktok']
        paid_ms=int(datetime(2026,9,22,4,0,tzinfo=timezone.utc).timestamp()*1000)
        self.batch=dict(SCOPE,total=1,partition='current',shop_names=['巴西 中文店铺'],expected_order_ids=['1234567890123456789'],
                        list_check=[dict(order_number='1234567890123456789',paid_at=paid_ms)])
    def tearDown(self):self.t.cleanup()
    def parse(self):
        with patch('xlrd.open_workbook',return_value=Book(Sheet(self.headers,[self.values]))):return read_xls(self.p,self.batch,ExportSettings())
    def test_text_and_numeric_quantities(self):self.assertEqual(self.parse()['rows'][0]['item']['quantity'],1)
    def test_numeric_id_rejected(self):
        self.values[3]=Cell(1234567890123456789,xlrd.XL_CELL_NUMBER)
        with self.assertRaisesRegex(ExportError,'NUMERIC_ID'):self.parse()
    def test_privacy_column_rejected_before_rows(self):
        self.headers.append('买家电话');self.values.append('must-not-read')
        with self.assertRaisesRegex(ExportError,'SCHEMA_CHANGED'):self.parse()
    def test_missing_required_column(self):
        self.headers[0]='unknown'
        with self.assertRaisesRegex(ExportError,'SCHEMA_CHANGED'):self.parse()
    def test_unmapped_non_target_stock_sku_remains_for_completeness(self):
        self.values[6]=''
        parsed=self.parse()
        self.assertEqual(parsed['rows'][0]['item']['stock_sku'],'')
        self.assertEqual(parsed['evidence']['orders'],1)
        self.assertEqual(parsed['evidence']['unmapped_sku_rows'],1)
        metrics,orders,_=aggregate([parsed],SCOPE['skus'],SCOPE)
        self.assertEqual(metrics['product_order_count'],0);self.assertEqual(orders,[])
    def test_missing_target_stock_sku_records_safe_row_evidence(self):
        self.batch['target_skus']=['PT']
        self.values[6]=''
        with self.assertRaisesRegex(ExportError,'SKU_MISSING') as caught:self.parse()
        self.assertEqual(caught.exception.details['stage'],'xls_row')
        self.assertEqual(caught.exception.details['partition'],'current')
        self.assertEqual(caught.exception.details['xls_row_index'],1)
        self.assertTrue(caught.exception.details['platform_sku_present'])
        self.assertEqual(len(caught.exception.details['order_hash']),12)
    def test_duplicate_column(self):
        self.headers.append('订单金额');self.values.append(56)
        with self.assertRaisesRegex(ExportError,'SCHEMA_CHANGED'):self.parse()
    def test_non_tiktok_rejected(self):
        self.values[-1]='shopee'
        with self.assertRaisesRegex(ExportError,'NON_TIKTOK'):self.parse()
    def test_unverified_currency_rejected(self):
        self.values[-2]='BRL'
        with self.assertRaisesRegex(ExportError,'CURRENCY_UNVERIFIED'):self.parse()
    def test_other_shop_rejected(self):
        self.values[2]='unknown'
        with self.assertRaisesRegex(ExportError,'SHOP_SCOPE'):self.parse()
    def test_minute_boundary_overlap_permitted(self):
        self.batch['partition']='completed';self.batch['expected_order_ids']=None
        self.batch['start']='2026-09-22T01:00:30-03:00';self.parse()
    def test_end_minute_excluded(self):
        self.batch['partition']='completed';self.batch['expected_order_ids']=None
        self.batch['end']='2026-09-22T01:00:00-03:00'
        with self.assertRaisesRegex(ExportError,'PAYMENT_SCOPE') as caught:self.parse()
        self.assertEqual(caught.exception.details,{
            'stage':'xls_payment_scope','partition':'completed',
            'paid_minute':'2026-09-22T01:00:00-03:00',
            'window_start':'2026-09-22T03:00:00+00:00',
            'window_end':'2026-09-22T04:00:00+00:00','xls_row_index':1,
        })
    def test_current_uses_guarded_list_payment_time_when_xls_clock_differs(self):
        self.values[1]='2026-09-22 22:30'
        parsed=self.parse()
        self.assertEqual(parsed['rows'][0]['paid_at'],'2026-09-22T01:00:00-03:00')
        self.assertEqual(parsed['evidence']['payment_time_source'],'CURRENT_LIST_JSON')
        self.assertEqual(parsed['evidence']['xls_payment_mismatch_orders'],1)
    def test_current_list_identity_still_must_match_selected_export_ids(self):
        self.batch['list_check'][0]['order_number']='different'
        with self.assertRaisesRegex(ExportError,'ID_SET_CHANGED'):self.parse()
    def test_current_list_payment_time_must_be_inside_frozen_scope(self):
        self.batch['list_check'][0]['paid_at']=int(datetime(2026,9,22,8,0,tzinfo=timezone.utc).timestamp()*1000)
        with self.assertRaisesRegex(ExportError,'PAYMENT_SCOPE') as caught:self.parse()
        self.assertEqual(caught.exception.details['stage'],'list_payment_scope')
    def test_missing_export_orders(self):
        self.batch['total']=2
        with self.assertRaisesRegex(ExportError,'INCOMPLETE'):self.parse()
    def test_completed_all_orders_drift_accepts_only_observed_before_or_after_count(self):
        self.batch.update(partition='completed',expected_order_ids=None,total=2,
                          acceptable_order_counts=[1,2],before_count=2,after_count=1,
                          reconciliation_result='RECONCILIATION_REQUIRED')
        parsed=self.parse()
        self.assertEqual(parsed['evidence']['exported_count'],1)
        self.assertEqual(parsed['evidence']['reconciliation_result'],'RECONCILIATION_REQUIRED')
        self.batch['acceptable_order_counts']=[2,3]
        with self.assertRaisesRegex(ExportError,'INCOMPLETE'):self.parse()
    def test_changed_export_ids(self):
        self.batch['expected_order_ids']=['different']
        with self.assertRaisesRegex(ExportError,'ID_SET_CHANGED'):self.parse()
    def test_negative_fractional_nonfinite_quantity(self):
        for value in (-1,'1.5','NaN'):
            with self.subTest(value=value):
                self.values[8]=value
                with self.assertRaises(ExportError):self.parse()
    def test_not_xlsx(self):
        self.p.write_bytes(b'PK\x03\x04')
        with self.assertRaisesRegex(ExportError,'FILE_INVALID'):self.parse()


class SliceTransport:
    def __init__(self,constant=None):self.queries=[];self.exports=[];self.constant=constant
    def query(self,partition,binding,scope,size):
        self.queries.append((partition,scope['start'],scope['end'],size))
        seconds=(datetime.fromisoformat(scope['end'])-datetime.fromisoformat(scope['start'])).total_seconds()
        total=self.constant if self.constant is not None else int(seconds/20)
        return dict(total=total,loaded=min(size,total),page=1,page_size=size,
                    order_ids=[f'order-{i}' for i in range(min(size,total))])
    def export(self,partition,binding,scope,page):
        value=dict(scope,partition=partition,total=page['total'],
                   expected_order_ids=(page['order_ids'] if partition=='current' else None),
                   suppressed_downloads=1,reconciliation_required=False)
        self.exports.append(value);return value


class DriftingExportTransport(SliceTransport):
    def __init__(self, constant):
        super().__init__(constant);self.export_attempts=0
    def export(self,partition,binding,scope,page):
        self.export_attempts+=1
        if self.export_attempts==1:
            error=ExportError('EXPORT_SOURCE_CHANGED')
            error.details={'stage':'export-precheck','reason':'list-drift'}
            raise error
        return super().export(partition,binding,scope,page)


class SlicingTests(unittest.TestCase):
    def test_lte_500_one_slice(self):
        t=SliceTransport(497);s=ExportSettings();b,r=CurrentOrdersExportAdapter(t,s).collect({},SCOPE,Budget(s));self.assertEqual(len(b),1);self.assertEqual([q[-1] for q in t.queries],[50,500])
    def test_gt_500_recursive_split_overlap_and_bounds(self):
        t=SliceTransport();s=ExportSettings();b,r=CurrentOrdersExportAdapter(t,s).collect({},SCOPE,Budget(s));self.assertEqual(len(b),2);self.assertTrue(all(x['total']<=500 for x in b));self.assertGreater(b[0]['end'],b[1]['start']);self.assertTrue(all(q[-1]<=500 for q in t.queries))
    def test_slice_limit_no_partial_result(self):
        t=SliceTransport(501);s=ExportSettings(max_export_slices=2)
        with self.assertRaisesRegex(ExportError,'SLICE_LIMIT'):CurrentOrdersExportAdapter(t,s).collect({},SCOPE,Budget(s))
    def test_dense_minute_fails_not_recursion_loop(self):
        s=ExportSettings();scope=dict(SCOPE,end='2026-09-22T00:01:00-03:00')
        with self.assertRaisesRegex(ExportError,'DENSE_MINUTE'):CurrentOrdersExportAdapter(SliceTransport(501),s).collect({},scope,Budget(s))
    def test_runtime_limit(self):
        s=ExportSettings();clock=iter([0,901]);b=Budget(s,clock=lambda:next(clock))
        with self.assertRaisesRegex(ExportError,'RUNTIME_LIMIT'):b.check()
    def test_cancel(self):
        with self.assertRaisesRegex(ExportError,'CANCELLED'):Budget(ExportSettings(),lambda:False).check()
    def test_source_count_change_fails(self):
        t=SliceTransport(400);old=t.query
        calls=0
        def query(*args):
            nonlocal calls
            value=old(*args)
            if args[-1]==500:
                calls+=1;value['total']=400+calls
            return value
        t.query=query;s=ExportSettings()
        with self.assertRaisesRegex(ExportError,'SOURCE_CHANGED'):CurrentOrdersExportAdapter(t,s).collect({},SCOPE,Budget(s))
    def test_source_count_change_recovers_one_slice_once(self):
        t=SliceTransport(400);old=t.query;calls=0
        def query(*args):
            nonlocal calls
            value=old(*args)
            if args[-1]==500:
                calls+=1
                if calls==1:value['total']=401
            return value
        t.query=query;s=ExportSettings();b,_=CurrentOrdersExportAdapter(t,s).collect({},SCOPE,Budget(s))
        self.assertEqual(len(b),1);self.assertEqual(calls,2)
    def test_completed_does_not_need_large_page_or_selected_ids(self):
        t=SliceTransport(1000);s=ExportSettings();b,_=CompletedOrdersExportAdapter(t,s).collect({},SCOPE,Budget(s));self.assertEqual(len(b),1);self.assertEqual(t.queries[0][-1],50)
    def test_completed_large_range_splits_below_sync_export_limit(self):
        scope=dict(SCOPE,start='2026-09-22T00:00:00-03:00',end='2026-09-23T00:00:00-03:00')
        t=SliceTransport();s=ExportSettings();b,r=CompletedOrdersExportAdapter(t,s).collect({},scope,Budget(s))
        self.assertGreater(len(b),1);self.assertTrue(all(x['total']<=2000 for x in b))
        self.assertTrue(any(x['action']=='SPLIT' for x in r));self.assertTrue(all(q[-1]==50 for q in t.queries))
    def test_all_orders_current_list_drift_is_reloaded_once(self):
        t=DriftingExportTransport(400);s=ExportSettings()
        batches,receipts=CurrentOrdersExportAdapter(t,s).collect({},ALL_ORDERS_SCOPE,Budget(s))
        self.assertEqual((len(batches),t.export_attempts),(1,2))
        self.assertEqual(sum(x['action']=='RETRY_SOURCE_CHANGED' for x in receipts),1)
    def test_all_orders_completed_list_drift_is_reloaded_once(self):
        t=DriftingExportTransport(1000);s=ExportSettings()
        batches,receipts=CompletedOrdersExportAdapter(t,s).collect({},ALL_ORDERS_SCOPE,Budget(s))
        self.assertEqual((len(batches),t.export_attempts),(1,2))
        self.assertEqual(sum(x['action']=='RETRY_SOURCE_CHANGED' for x in receipts),1)
    def test_zero_is_explicit_no_export(self):
        t=SliceTransport(0);s=ExportSettings();b,r=CurrentOrdersExportAdapter(t,s).collect({},SCOPE,Budget(s));self.assertEqual(b,[]);self.assertEqual(r[0]['action'],'EMPTY')
    def test_reject_large_page_configuration(self):
        with self.assertRaises(ValueError):ExportSettings(max_current_export_rows=3000)
        with self.assertRaises(ValueError):ExportSettings(max_completed_export_rows=2001)


class OrderReconciliationTests(unittest.TestCase):
    def test_all_orders_collects_current_before_completed(self):
        events=[]
        class Adapter:
            def __init__(self,name):self.name=name
            def collect(self,*args):events.append(self.name);return [],[]
        class Transport:
            deadline=None
            def cleanup(self,binding):events.append('cleanup')
        connector=ExportConnector(tempfile.mkdtemp(),transport=Transport())
        connector.current,connector.completed=Adapter('current'),Adapter('completed')
        connector.collect({},ALL_ORDERS_SCOPE)
        self.assertEqual(events,['current','completed','cleanup'])

    def test_filtered_order_read_sequence_is_unchanged(self):
        events=[]
        class Adapter:
            def __init__(self,name):self.name=name
            def collect(self,*args):events.append(self.name);return [],[]
        class Transport:
            deadline=None
            def cleanup(self,binding):events.append('cleanup')
        connector=ExportConnector(tempfile.mkdtemp(),transport=Transport())
        connector.current,connector.completed=Adapter('current'),Adapter('completed')
        connector.collect({},SCOPE)
        self.assertEqual(events,['completed','current','cleanup'])

    def test_cross_source_duplicate_is_reconciled_by_order_key(self):
        current=file([row()],hash='current');completed=file([row()],hash='completed')
        completed['evidence']['partition']='completed'
        batches=[dict(suppressed_downloads=1,reconciliation_candidate_ids=[]),
                 dict(suppressed_downloads=1,reconciliation_candidate_ids=[])]
        connector=ExportConnector(tempfile.mkdtemp())
        with patch.object(connector,'_parse_files',return_value=[current,completed]):
            result=connector.finalize_all(dict(batches=batches,receipts=[],budget=None),ALL_ORDERS_SCOPE,Path('.'))
        self.assertEqual(len(result['orders']),1)
        self.assertEqual(result['reconciliation']['cross_source_duplicates'],1)
        self.assertEqual(result['reconciliation']['reconciliation_result'],'PASS')

    def test_current_drift_is_recorded_with_one_post_export_read(self):
        class Moving(SliceTransport):
            def __init__(self):super().__init__();self.n=0
            def query(self,partition,binding,scope,size):
                self.n+=1;total={1:400,2:401,3:402}[self.n]
                self.queries.append((partition,scope['start'],scope['end'],size))
                return dict(total=total,loaded=total,page=1,page_size=size,
                            order_ids=[f'order-{i}' for i in range(total)])
        t=Moving();s=ExportSettings()
        batches,receipts=CurrentOrdersExportAdapter(t,s).collect({},ALL_ORDERS_SCOPE,Budget(s))
        self.assertEqual(len(t.queries),3)
        self.assertEqual(batches[0]['reconciliation_candidate_ids'],['order-401'])
        self.assertEqual(receipts[-1]['reconciliation_result'],'RECONCILIATION_REQUIRED')
        self.assertEqual((receipts[-1]['before_count'],receipts[-1]['after_count'],receipts[-1]['drift_delta']),
                         (400,402,2))
        windows={(x[1],x[2]) for x in t.queries}
        self.assertEqual(len(windows),1)
        only=next(iter(windows))
        self.assertEqual((datetime.fromisoformat(only[0]),datetime.fromisoformat(only[1])),
                         (datetime.fromisoformat(ALL_ORDERS_SCOPE['start']),datetime.fromisoformat(ALL_ORDERS_SCOPE['end'])))

    def test_completed_drift_accepts_only_before_or_after_xls_count(self):
        class Moving(SliceTransport):
            def __init__(self):super().__init__();self.n=0
            def query(self,partition,binding,scope,size):
                self.n+=1;total=1000 if self.n==1 else 1002
                return dict(total=total,loaded=50,page=1,page_size=size,
                            order_ids=[f'order-{i}' for i in range(50)])
            def export(self,partition,binding,scope,page):
                value=super().export(partition,binding,scope,page)
                value.update(total=1001,reconciliation_required=True)
                return value
        t=Moving();s=ExportSettings()
        batches,receipts=CompletedOrdersExportAdapter(t,s).collect({},ALL_ORDERS_SCOPE,Budget(s))
        self.assertEqual(batches[0]['acceptable_order_counts'],[1001,1002])
        self.assertEqual(receipts[-1]['reconciliation_result'],'RECONCILIATION_REQUIRED')

    def test_current_missing_candidate_explained_by_completed_passes(self):
        current=file([row(key='current')],hash='current')
        completed=file([row(key='moved')],hash='completed');completed['evidence']['partition']='completed'
        batches=[dict(suppressed_downloads=1,reconciliation_candidate_ids=['moved']),
                 dict(suppressed_downloads=1,reconciliation_candidate_ids=[])]
        connector=ExportConnector(tempfile.mkdtemp())
        with patch.object(connector,'_parse_files',return_value=[current,completed]):
            result=connector.finalize_all(dict(batches=batches,receipts=[],budget=None),ALL_ORDERS_SCOPE,Path('.'))
        self.assertEqual(result['reconciliation']['unexplained_missing_orders'],0)

    def test_unexplained_current_candidate_fails(self):
        current=file([row()],hash='current')
        batches=[dict(suppressed_downloads=1,reconciliation_candidate_ids=['missing-order'])]
        connector=ExportConnector(tempfile.mkdtemp())
        with patch.object(connector,'_parse_files',return_value=[current]):
            with self.assertRaisesRegex(ExportError,'UNEXPLAINED_GAP'):
                connector.finalize_all(dict(batches=batches,receipts=[],budget=None),ALL_ORDERS_SCOPE,Path('.'))


class TransportCooldownTests(unittest.TestCase):
    def test_verified_export_cooldown_waits_once_then_retries(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        import time
        class Cooldown:
            def __init__(self):self.recorded=[]
            def until(self):return None
            def record(self,value):
                self.recorded.append(value);return datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as root:
            transport=EgoExportTransport(root,ExportSettings());transport.cooldown=Cooldown()
            transport.deadline=time.monotonic()+900
            limited=SimpleNamespace(returncode=0,stderr='',stdout='ORDER_PULSE_EXPORT:{"error":"RATE_LIMITED","retry_after":"120"}\n')
            success=SimpleNamespace(returncode=0,stderr='',stdout='ORDER_PULSE_EXPORT:{"url":"https://tms-cos.mabangerp.com/a.xls"}\n')
            with patch('order_pulse.exports.transport.subprocess.run',side_effect=[limited,success]) as run, \
                 patch('order_pulse.exports.transport.time.sleep') as sleep:
                value=transport.execute({'action':'export'})
            self.assertEqual(value['url'],'https://tms-cos.mabangerp.com/a.xls')
            self.assertEqual(transport.cooldown.recorded,['120']);self.assertEqual(run.call_count,2)
            sleep.assert_called_once()

    def test_cleanup_is_not_blocked_by_source_cooldown(self):
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace
        class Cooldown:
            def until(self):return datetime.now(timezone.utc)+timedelta(seconds=120)
        with tempfile.TemporaryDirectory() as root:
            transport=EgoExportTransport(root,ExportSettings());transport.cooldown=Cooldown()
            success=SimpleNamespace(returncode=0,stderr='',stdout='ORDER_PULSE_EXPORT:{"cleaned":true}\n')
            with patch('order_pulse.exports.transport.subprocess.run',return_value=success), \
                 patch('order_pulse.exports.transport.time.sleep') as sleep:
                self.assertTrue(transport.execute({'action':'cleanup'})['cleaned'])
            sleep.assert_not_called()

    def test_query_timeout_reloads_then_retries_once(self):
        from types import SimpleNamespace
        import time
        class Cooldown:
            def until(self):return None
        with tempfile.TemporaryDirectory() as root:
            transport=EgoExportTransport(root,ExportSettings());transport.cooldown=Cooldown()
            transport.deadline=time.monotonic()+900
            timed=SimpleNamespace(returncode=0,stderr='',stdout='ORDER_PULSE_EXPORT:{"error":"EXPORT_TIMEOUT"}\n')
            success=SimpleNamespace(returncode=0,stderr='',stdout='ORDER_PULSE_EXPORT:{"total":1}\n')
            with patch('order_pulse.exports.transport.subprocess.run',side_effect=[timed,success]) as run, \
                 patch('order_pulse.exports.transport.time.sleep') as sleep:
                self.assertEqual(transport.execute({'action':'query'})['total'],1)
            self.assertEqual(run.call_count,2);sleep.assert_called_once_with(5)


class TemporaryTests(unittest.TestCase):
    def setUp(self):self.t=tempfile.TemporaryDirectory();self.temp=TemporaryExports(Path(self.t.name)/'exports')
    def tearDown(self):self.t.cleanup()
    def test_permissions_and_success_cleanup(self):
        with self.temp.task() as d:
            p=self.temp.write(d,'a.xls',b'file');self.assertEqual(d.stat().st_mode&0o777,0o700);self.assertEqual(p.stat().st_mode&0o777,0o600)
        self.assertFalse(d.exists())
    def test_failure_cleanup(self):
        with self.assertRaises(RuntimeError):
            with self.temp.task() as d:
                self.temp.write(d,'a.xls',b'file');raise RuntimeError('parse failure')
        self.assertFalse(d.exists())
    def test_cleanup_only_marked_expired_not_active(self):
        import uuid
        root=self.temp.root;unmarked=root/str(uuid.uuid4());unmarked.mkdir();(unmarked/'user.xls').write_bytes(b'user')
        expired=root/str(uuid.uuid4());expired.mkdir();(expired/'.owner.json').write_text(json.dumps(dict(owner=OWNER,created_at=0)));(expired/'.lock').touch();(expired/'a.xls').write_bytes(b'own')
        with self.temp.task() as active:
            self.assertEqual(self.temp.cleanup_expired(now=9999999999),1);self.assertTrue(active.exists());self.assertTrue(unmarked.exists())
        self.assertFalse(expired.exists())
    def test_symlink_not_followed(self):
        import uuid
        dest=Path(self.t.name)/'external';dest.mkdir();(self.temp.root/str(uuid.uuid4())).symlink_to(dest)
        self.assertEqual(self.temp.cleanup_expired(now=9999999999),0);self.assertTrue(dest.exists())
    def test_path_escape_rejected(self):
        with self.temp.task() as d:
            with self.assertRaisesRegex(ExportError,'TEMP_UNSAFE'):self.temp.write(d,'../bad',b'')
    def test_exact_native_download_copy_fails_without_deleting_it(self):
        home=Path(self.t.name);(home/'Downloads').mkdir();copy=home/'Downloads/order-test.xls';copy.write_bytes(b'user-or-export-copy')
        source=ExportConnector(home,downloader=lambda *a:b'file',temporary=self.temp)
        captured={'budget':Budget(ExportSettings()),'batches':[{'url':'https://tms-cos.mabangerp.com/order-test.xls'}]}
        with self.temp.task() as d,patch('pathlib.Path.home',return_value=home):
            with self.assertRaisesRegex(ExportError,'DOWNLOAD_NOT_CONTROLLED'):source.finalize(captured,SCOPE,d)
        self.assertTrue(copy.exists())
