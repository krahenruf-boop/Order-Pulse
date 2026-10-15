"""Regression: late pagination updates before reading are not an identity change."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from order_pulse.kalodata.models import KalodataError, now_iso, validate_context
from order_pulse.kalodata.connectors.browser_provider import BrowserProvider
from order_pulse.kalodata.repository import Repository
from order_pulse.kalodata.service import Service


class RefreshContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request=dict(product_url='https://www.kalodata.com/product/detail?id=1&region=TH',period='180D',start_page=1,end_page=1,session_ref='plugin-test')
        self.raw=dict(product_id='1',region='TH',period='180D',actual_start_date='2026-04-13',actual_end_date='2026-10-09',site_date_label='180天',site_timezone=None,date_basis='page_visible_range',page_size=10,visible_total=None,visible_pages=130,observed_sort=[dict(field='成交金额',direction='DESC')],filters={},source_mode='ui_dom',captured_at=now_iso())
        self.before=validate_context(self.raw,self.request)
        self.after=validate_context(dict(self.raw,visible_pages=431),self.request)

    async def test_late_page_count_uses_final_verified_context(self):
        provider=BrowserProvider();provider._session_ref='plugin-test';provider._call=AsyncMock(return_value=self.after)
        current=await provider.revalidate(self.request,self.before,'run',1)
        self.assertEqual(current['visible_pages'],431)
        self.assertEqual(provider._call.call_args.args[1]['period'],'180D')
        self.assertEqual(current['context_fingerprint'],self.before['context_fingerprint'])

    async def test_actual_product_market_sort_and_page_size_changes_still_stop(self):
        for change in (dict(product_id='2'),dict(region='US'),dict(observed_sort=[dict(field='播放量',direction='DESC')]),dict(page_size=20)):
            with self.subTest(change=change):
                provider=BrowserProvider();provider._session_ref='plugin-test';provider._call=AsyncMock(return_value=dict(self.raw,**change))
                with self.assertRaises(KalodataError) as exc:await provider.revalidate(self.request,self.before,'run',1)
                self.assertEqual(exc.exception.code,'CONTEXT_CHANGED')

    async def test_final_pagination_still_enforces_requested_page_range(self):
        provider=BrowserProvider();provider._session_ref='plugin-test';provider._call=AsyncMock(return_value=dict(self.raw,visible_pages=1))
        with self.assertRaises(KalodataError) as exc:await provider.revalidate(dict(self.request,end_page=2),self.before,'run',1)
        self.assertEqual(exc.exception.code,'RANGE_EXCEEDS_VISIBLE')

    async def test_final_context_is_persisted_before_first_read_for_both_entry_points(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Repository(Path(temp)/'kalodata.sqlite3');repo.open(True)
            connector=type('Connector',(),{})();connector.preflight=AsyncMock(return_value=self.before);connector.revalidate=AsyncMock(return_value=self.after)
            service=Service(repo,connector,enabled=True);service.available=AsyncMock();service.execute=AsyncMock()
            try:
                repo.create('job','client',self.request,{'visible_pages':None})
                await service._prepare_product('job')
                self.assertEqual(repo.get_run('job')['query_context']['visible_pages'],431)
                service.execute.assert_awaited_once_with('job')
                repo.finish('job','completed')
                preflight=await service.preflight(self.request)
                run=await service.start(dict(preflight_id=preflight['preflight_id'],client_request_id='legacy'))
                await asyncio.gather(*service.tasks.values())
                self.assertEqual(run['query_context']['visible_pages'],431)
            finally:repo.close()


if __name__=='__main__':unittest.main()
