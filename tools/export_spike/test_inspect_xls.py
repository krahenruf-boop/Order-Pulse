"""Synthetic cell tests only. Real XLS evidence is documented separately."""
import unittest
from types import SimpleNamespace
from inspect_xls import BASE, identifier, inspect_sheet

class Sheet:
    def __init__(self, rows, headers=None):
        self.data=[headers or BASE]+rows
        self.nrows=len(self.data); self.ncols=len(self.data[0])
    def cell(self,r,c):
        v=self.data[r][c]
        return SimpleNamespace(value=v,ctype=1 if isinstance(v,str) else 2)
    def cell_value(self,r,c):return self.data[r][c]
    def col(self,c):return [self.cell(r,c) for r in range(self.nrows)]

def row(order='7641475860268400660', sku='HBYDK1*2', amount=56.07, qty=1, platform='7641475860268400660'):
    return [amount,'2026-09-22 00:01','18店 中文',order,platform,'TRACK-001',sku,sku,qty,'测试商品']

class TestXlsEvidence(unittest.TestCase):
    def test_long_text_id_preserved(self):
        s=Sheet([row()]); self.assertEqual(identifier(s.cell(1,3)),'7641475860268400660')
        self.assertEqual(inspect_sheet(s)['id_lengths']['订单编号'],{19:1})
    def test_numeric_id_rejected(self):
        r=row();r[3]=7641475860268400660
        with self.assertRaisesRegex(ValueError,'NUMERIC_IDENTIFIER'):inspect_sheet(Sheet([r]))
    def test_missing_key_rejected(self):
        with self.assertRaisesRegex(ValueError,'ORDER_KEY_MISSING'):inspect_sheet(Sheet([row(order='',platform='')]))
    def test_order_number_fallback(self):
        self.assertEqual(inspect_sheet(Sheet([row(platform='')]))['unique_orders'],1)
    def test_multi_sku_amount_once(self):
        r=inspect_sheet(Sheet([row(),row(sku='HBYDK1')]))
        self.assertEqual((r['unique_orders'],r['row_amount_sum'],r['order_amount_sum_once']),(1,'112.14','56.07'))
    def test_amount_conflict_not_summed_or_picked(self):
        r=inspect_sheet(Sheet([row(),row(amount=99)]))
        self.assertIn('AMOUNT_CONFLICT',r['warnings']);self.assertIsNone(r['order_amount_sum_once'])
    def test_quantity_not_sku_multiplier(self):
        self.assertEqual(inspect_sheet(Sheet([row()]))['quantities'],{'1':1})
    def test_privacy_column_rejected_before_rows(self):
        with self.assertRaisesRegex(ValueError,'UNEXPECTED_COLUMNS'):inspect_sheet(Sheet([row()+['PRIVATE']],BASE+['买家姓名']))
    def test_missing_required_column(self):
        with self.assertRaisesRegex(ValueError,'SCHEMA_CHANGED'):inspect_sheet(Sheet([row()[:-1]],BASE[:-1]))
    def test_negative_quantity_rejected(self):
        with self.assertRaisesRegex(ValueError,'QUANTITY_INVALID'):inspect_sheet(Sheet([row(qty=-1)]))
    def test_fractional_quantity_rejected(self):
        with self.assertRaisesRegex(ValueError,'QUANTITY_INVALID'):inspect_sheet(Sheet([row(qty=1.5)]))
    def test_platform_and_currency_preserved(self):
        result=inspect_sheet(Sheet([row()+['RMB','tiktok']],BASE+['币种','平台ID']))
        self.assertEqual(result['currencies'],{'RMB':1});self.assertEqual(result['export_platforms'],{'tiktok':1})

if __name__=='__main__':unittest.main(verbosity=2)
