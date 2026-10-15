import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from order_pulse.kalodata.item_normalizer import KalodataItemNormalizer
from order_pulse.kalodata.connectors.browser_provider import BrowserProvider
from order_pulse.kalodata.models import KalodataError
from order_pulse.kalodata.models import now_iso
from order_pulse.kalodata.repository import Repository as KalodataRepository
from order_pulse.kalodata.service import Service
from order_pulse.storage import Repository


class ItemCompatibilityTests(unittest.TestCase):
    def item(self, index=1, **changes):
        return dict(source_page=1, source_index=index, status="resolved", video_id="123",
                    tiktok_url="https://www.tiktok.com/@author/video/123",
                    kalodata_video_url="https://www.kalodata.com/video/detail?id=123",
                    error_code=None, **changes)

    def test_observed_seven_item_shape_has_explicit_status_and_order(self):
        rows=[self.item(i) for i in range(1,8)]
        rows[-1].update(status="unresolved",tiktok_url=None,error_code="DETAIL_QUOTA_EXHAUSTED")
        normalized=[KalodataItemNormalizer.normalize(x,page_size=10) for x in rows]
        self.assertEqual([x['rank'] for x in normalized],list(range(1,8)))
        self.assertEqual([x['status'] for x in normalized],['VALID_VIDEO']*6+['PARSE_FAILED'])
        self.assertIsNone(normalized[-1]['video_url'])
        self.assertEqual(normalized[-1]['reason_code'],'DETAIL_QUOTA_EXHAUSTED')

    def test_missing_url_does_not_mean_non_video_and_never_synthesizes_url(self):
        row=self.item();row.update(status='unresolved',tiktok_url=None)
        result=KalodataItemNormalizer.normalize(row,page_size=10)
        self.assertEqual(result['status'],'PARSE_FAILED')
        self.assertIsNone(result['video_url'])
        row.update(error_code='NOT_VIDEO_ITEM')
        self.assertEqual(KalodataItemNormalizer.normalize(row,page_size=10)['status'],'PARSE_FAILED')

    def test_only_verified_schema_and_matching_video_url(self):
        row=self.item();row['source_page']=2
        self.assertEqual(KalodataItemNormalizer.normalize(row,page_size=10)['rank'],11)
        self.assertEqual(KalodataItemNormalizer.normalize(row,page_size=10,schema_version='unknown')['status'],'PARSE_FAILED')
        for url in ('https://www.tiktok.com/@x/video/999','https://www.tiktok.com.evil.invalid/@x/video/123','javascript:alert(1)'):
            row['tiktok_url']=url
            self.assertIsNone(KalodataItemNormalizer.normalize(row,page_size=10)['video_url'])

    def test_actual_quota_and_load_failure_page_shapes(self):
        source=(Path(__file__).parents[1]/'order_pulse/integrations/ego_kalodata.js').read_text().split("const MARKER=")[0]
        program=source+"""
globalThis.document={body:{innerText:''}};
const cases=[
 ['试用版账号每天可查看 30 次详情页，今天剩余 0 次。加载失败，请刷新重试。','DETAIL_QUOTA_EXHAUSTED'],
 ['试用版账号每天可查看\\n30\\n次详情页，今天剩余\\n0\\n次。','DETAIL_QUOTA_EXHAUSTED'],
 ['加载失败，请刷新重试。 如果依然无法恢复，请复制下方的Trace ID','SOURCE_LOAD_FAILED'],
 ['您的账号已经在新的设备上登录，请再次登录','LOGIN_REQUIRED']
];
for(const [body,expected] of cases){
 document.body.innerText=body;
 const result=await observeDOM({operation:'resolve_observed_link',payload:{}});
 if(result.error_code!==expected)throw Error(JSON.stringify(result));
}
console.log('PASS');
"""
        result=subprocess.run(['node','--input-type=module'],input=program,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('PASS',result.stdout)

    def test_quota_is_not_retried_or_misreported_as_schema(self):
        class Transport:
            count=0
            async def observe(self,operation,envelope):
                self.count+=1
                return dict(envelope,success=False,error_code='DETAIL_QUOTA_EXHAUSTED',data={})
            async def probe_session(self):return {'state':'DETAIL_QUOTA_EXHAUSTED'}
        async def run():
            transport=Transport();provider=BrowserProvider(transport,min_interval=0)
            cap=await provider.capabilities()
            self.assertFalse(cap['transport_ready'])
            self.assertEqual(cap['blocked_reason']['code'],'DETAIL_QUOTA_EXHAUSTED')
            provider._session_ref='space6'
            with self.assertRaises(KalodataError) as error:
                await provider._call('resolve_observed_link',{'run_id':'test','generation':1})
            self.assertEqual(error.exception.code,'DETAIL_QUOTA_EXHAUSTED')
            self.assertEqual(transport.count,1)
        asyncio.run(run())

    def test_seventh_item_quota_failure_is_persisted_without_losing_first_six(self):
        class Connector:
            calls=0
            async def read_page(self,page,context,run_id,generation):
                return dict(source_page=page,context_fingerprint='test',product_id='1',region='US',
                    page_size=7,verified=True,visible_pages=1,observed_at=now_iso(),
                    rows=[{'video_id':str(i),'kalodata_video_url':f'https://www.kalodata.com/video/detail?id={i}'} for i in range(1,8)])
            async def resolve(self,row,context,run_id,generation):
                self.calls+=1
                if row['source_index']==7:raise KalodataError('DETAIL_QUOTA_EXHAUSTED',403)
                return dict(kind='detail_anchor',observed_href=f"https://www.tiktok.com/@test/video/{row['video_id']}",
                    source_url=row['kalodata_video_url'],observed_at=now_iso(),source_page=1,source_index=row['source_index'])
        with tempfile.TemporaryDirectory() as d:
            Repository(d);repo=KalodataRepository(Path(d)/'order_pulse.sqlite3');repo.open(True)
            request=dict(start_page=1,end_page=1,period='7D')
            context=dict(context_fingerprint='test',product_id='1',region='US',visible_pages=1,visible_total=7,
                         page_size=7,actual_start_date='2026-01-01',actual_end_date='2026-01-07')
            repo.create('run','client',request,context)
            connector=Connector();service=Service(repo,connector,enabled=True)
            asyncio.run(service.execute('run'))
            run=repo.get_run('run')
            self.assertEqual(run['state'],'blocked')
            self.assertEqual(run['stop_reason'],'DETAIL_QUOTA_EXHAUSTED')
            self.assertEqual(run['counts']['resolved_unique'],6)
            self.assertEqual(len(run['items']),7)
            self.assertEqual(run['items'][-1]['error_code'],'DETAIL_QUOTA_EXHAUSTED')
            self.assertEqual(run['items'][-1]['normalized']['status'],'PARSE_FAILED')
            self.assertEqual(connector.calls,7)
            repo.close();repo.open(True)
            self.assertEqual(repo.get_run('run')['items'][-1]['normalized']['reason_code'],'DETAIL_QUOTA_EXHAUSTED')
            repo.close()


if __name__=='__main__':unittest.main()
