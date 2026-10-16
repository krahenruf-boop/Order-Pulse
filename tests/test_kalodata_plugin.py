import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient
from order_pulse.storage import Repository as MainRepository, Credentials
from order_pulse.kalodata.connectors.plugin_client import (KalodataPluginClient, PREFIX, OPERATIONS,
    MINIMUM_SUPPORTED_PLUGIN_VERSION, REQUIRED_BUILD_ID, REQUIRED_KALODATA_ADAPTER)
from order_pulse.kalodata.connectors.browser_provider import BrowserProvider
from order_pulse.kalodata.module import KalodataModule
from order_pulse.kalodata.models import KalodataError, now_iso, validate_context, product_url
from order_pulse.kalodata.repository import Repository
from order_pulse.kalodata.service import Service
from order_pulse.kalodata.exports import make_export


class PluginHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();MainRepository(self.tmp.name)
        self.credentials=Credentials(self.tmp.name)
        self.credentials.save_namespace('kalodata',{'plugin_bridge':{'token':'test-token','extension_id':'a'*32,'session_ref':'plugin-test'}})
        self.plugin=KalodataPluginClient(self.tmp.name,self.credentials)
        self.headers={'Origin':'chrome-extension://'+'a'*32,'Authorization':'Bearer test-token'}
        self.http=TestClient(self.plugin.asgi,base_url='http://127.0.0.1:8876')
    def tearDown(self):self.http.close();self.tmp.cleanup()
    def heartbeat(self,extra=None):
        if extra is None:extra=f'&version={MINIMUM_SUPPORTED_PLUGIN_VERSION}&build_id={REQUIRED_BUILD_ID}&kalodata_adapter={REQUIRED_KALODATA_ADAPTER}'
        return self.http.get(PREFIX+'/job?bound=true'+extra,headers=self.headers)
    async def test_health_offline_never_operates_browser(self):
        self.plugin.observe=AsyncMock()
        status=await self.plugin.health(True)
        self.assertEqual(status['state'],'PLUGIN_OFFLINE');self.plugin.observe.assert_not_awaited()
    async def test_existing_plugin_heartbeat_and_passive_health(self):
        self.assertEqual(self.heartbeat().status_code,200)
        self.plugin.observe=AsyncMock();status=await self.plugin.health()
        self.assertTrue(status['plugin_connected']);self.assertTrue(status['browser_connected'])
        self.assertIsNone(status['kalodata_logged_in']);self.assertEqual(status['plugin_version'],MINIMUM_SUPPORTED_PLUGIN_VERSION)
        self.assertEqual(status['build_id'],REQUIRED_BUILD_ID);self.assertEqual(status['kalodata_adapter'],REQUIRED_KALODATA_ADAPTER)
        self.plugin.observe.assert_not_awaited()
        self.assertNotIn('token',json.dumps(status));self.assertNotIn('pairing_code',status)
    async def test_login_success_and_required(self):
        self.heartbeat();self.plugin.observe=AsyncMock(return_value={'success':True})
        self.assertTrue((await self.plugin.health(True))['kalodata_logged_in'])
        self.plugin.observe.return_value={'success':False,'login_required':True}
        self.assertEqual((await self.plugin.health(True))['state'],'KALODATA_LOGIN_REQUIRED')
        cap=await BrowserProvider(self.plugin).capabilities()
        self.assertEqual(cap['blocked_reason']['code'],'KALODATA_LOGIN_REQUIRED')
    async def test_version_handshake_distinguishes_reload_and_mismatch(self):
        self.heartbeat('')
        self.assertEqual((await self.plugin.health())['state'],'PLUGIN_RELOAD_REQUIRED')
        cap=await BrowserProvider(self.plugin).capabilities()
        self.assertEqual(cap['blocked_reason']['code'],'PLUGIN_RELOAD_REQUIRED')
        self.heartbeat('&version=0.1.0&build_id=old&kalodata_adapter=old')
        self.assertEqual((await self.plugin.health())['state'],'PLUGIN_RELOAD_REQUIRED')
        self.heartbeat(f'&version={MINIMUM_SUPPORTED_PLUGIN_VERSION}&build_id={REQUIRED_BUILD_ID}&kalodata_adapter=old')
        self.assertEqual((await self.plugin.health())['state'],'PLUGIN_VERSION_MISMATCH')
        self.assertEqual(self.http.get(PREFIX+'/job?bound=true',headers={'Origin':'https://evil.invalid','Authorization':'Bearer test-token'}).status_code,403)

    async def test_old_plugin_never_receives_work(self):
        self.heartbeat('')
        task=asyncio.create_task(self.plugin.observe('bind_product',{'operation_id':'1','run_id':'r','generation':1}))
        await asyncio.sleep(0)
        self.assertIsNone(self.heartbeat('').json()['job'])
        self.plugin.future.set_result({'session_ref':'plugin-test','operation_id':'1','run_id':'r','generation':1,'success':True,'data':{'bound':True}})
        await task
    async def test_correlation_reuses_existing_job_result_protocol(self):
        self.heartbeat()
        task=asyncio.create_task(self.plugin.observe('bind_product',{'operation_id':'1','run_id':'r','generation':1}))
        await asyncio.sleep(0)
        job=self.heartbeat().json()['job']
        self.assertEqual(job['operation'],'bind_product')
        response=self.http.post(PREFIX+'/result',json={'id':job['id'],'success':True,'data':{'bound':True}},headers=self.headers)
        self.assertTrue(response.json()['accepted'])
        data=await task;self.assertEqual(data['session_ref'],'plugin-test');self.assertEqual(data['run_id'],'r')
        self.assertIsNone(self.plugin.job)
    async def test_cancel_drops_pending_plugin_job(self):
        task=asyncio.create_task(self.plugin.observe('read_visible_page',{'run_id':'r','generation':1}))
        await asyncio.sleep(0);await self.plugin.cancel('r',1)
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertIsNone(self.plugin.job)
    async def test_challenge_and_login_response_preserve_source_for_user(self):
        for reason in ({'challenge':True}, {'http_status':401}, {'error_code':'KALODATA_LOGIN_REQUIRED'}):
            with self.subTest(reason=reason):
                self.plugin.logged_in=True
                task=asyncio.create_task(self.plugin.observe('inspect_ui',{'operation_id':'health','run_id':None,'generation':0}))
                await asyncio.sleep(0)
                job=self.heartbeat().json()['job']
                reply=self.http.post(PREFIX+'/result',json={'id':job['id'],'success':False,**reason},headers=self.headers)
                self.assertTrue(reply.json()['accepted'])
                await task
                self.assertFalse(self.plugin.logged_in)
    async def test_control_precedes_plugin_dispatch_and_failure_does_not_enqueue(self):
        self.heartbeat();events=[]
        @asynccontextmanager
        async def guard():
            events.append('control')
            self.assertIsNone(self.plugin.job)
            raise KalodataError('BROWSER_CONTROL_UNAVAILABLE',503)
            yield
        self.plugin.operation_guard=guard
        provider=BrowserProvider(self.plugin,min_interval=0)
        await provider.capabilities()
        with self.assertRaises(KalodataError) as exc:await provider._call('bind_product',{'run_id':'r','generation':1})
        self.assertEqual(exc.exception.code,'BROWSER_CONTROL_UNAVAILABLE')
        self.assertEqual(events,['control']);self.assertIsNone(self.plugin.job)
    async def test_rendering_survives_operation_boundaries_and_nested_run_lifecycle(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        lease=SimpleNamespace(start=Mock(),stop=Mock())
        self.plugin.background_factory=lambda:lease
        await self.plugin.begin_run()
        async with self.plugin.control():pass
        async with self.plugin.control():pass
        lease.start.assert_called_once();lease.stop.assert_not_called()
        await self.plugin.begin_run()
        await self.plugin.end_run()
        lease.stop.assert_not_called()
        await self.plugin.end_run()
        lease.stop.assert_called_once();self.assertIsNone(self.plugin.background)
        async with self.plugin.control():pass
        self.assertEqual(lease.start.call_count,2);self.assertEqual(lease.stop.call_count,2)

    async def test_plugin_timeout_has_specific_error_and_clears_pending_job(self):
        from unittest.mock import patch
        async def expired(awaitable,seconds):
            awaitable.cancel()
            raise asyncio.TimeoutError()
        with patch('order_pulse.kalodata.connectors.plugin_client.asyncio.wait_for',side_effect=expired):
            with self.assertRaises(KalodataError) as exc:await self.plugin.observe('bind_product',{'run_id':'r','generation':1})
        self.assertEqual(exc.exception.code,'PLUGIN_RESPONSE_TIMEOUT');self.assertIsNone(self.plugin.job)
    async def test_module_has_no_browser_lock_or_automation_transport(self):
        main=MainRepository(self.tmp.name);module=KalodataModule(self.tmp.name,main,self.credentials)
        self.assertIsInstance(module.transport,KalodataPluginClient)
        self.assertFalse(hasattr(module.transport,'browser_lock'))
        self.assertNotIn('browser_target',module.settings())
        for file in ('module.py','connectors/plugin_client.py','connectors/browser_provider.py','service.py'):
            source=(Path(__file__).parents[1]/'order_pulse/kalodata'/file).read_text()
            for forbidden in ('EgoTransport','takeOver','WAITING_USER','WAITING_BROWSER','space_id'):
                self.assertNotIn(forbidden,source)
        host=(Path(__file__).parents[1]/'order_pulse/service.py').read_text()
        self.assertIn('browser_lock=self.balance_browser_space.lock',host)
        self.assertIn('KalodataModule(root,self.repo,self.secrets)',host)

    async def test_health_during_active_job_does_not_interleave_plugin_operations(self):
        module=KalodataModule(self.tmp.name,MainRepository(self.tmp.name),self.credentials)
        current={'connection':{'active_job':'running-job'}}
        module.status=AsyncMock(return_value=current)
        module.runtime.dispatch=AsyncMock()
        self.assertEqual(await module.health(),current)
        module.runtime.dispatch.assert_not_awaited()


class KalodataVersionAndDateContracts(unittest.TestCase):
    def test_url_discards_stale_filter_params_and_normalizes_pasted_whitespace(self):
        value='https://www.kalodata.com/product/detail?id=1731358065995256014&language=zh-CN+&currency=USD&region=US&dateRange=%5B%222026-02-03%22%2C%222026-03-04%22%5D&cateValue=%5B1%5D&oldFilter=x'
        canonical,params=product_url(value)
        self.assertEqual(canonical,'https://www.kalodata.com/product/detail?id=1731358065995256014&region=US&language=zh-CN&currency=USD')
        self.assertNotIn('dateRange',params);self.assertNotIn('cateValue',canonical);self.assertNotIn('oldFilter',canonical)

    def test_advisory_date_drift_does_not_change_page_context_fingerprint(self):
        request={'product_url':'https://www.kalodata.com/product/detail?id=1&region=US','period':'30D','start_page':1,'end_page':1,'session_ref':'plugin-test'}
        base=dict(product_id='1',region='US',period='30D',actual_start_date='2026-08-28',actual_end_date='2026-09-26',site_date_label='30天',site_timezone=None,date_basis='page_visible_range',page_size=10,visible_total=10,visible_pages=1,observed_sort=[],filters={},source_mode='ui_dom',captured_at=now_iso())
        first=validate_context(base,request)
        drifted=dict(base,period='unknown',actual_start_date=None,actual_end_date=None,date_basis='unknown',site_date_label='')
        second=validate_context(drifted,request)
        self.assertEqual(first['context_fingerprint'],second['context_fingerprint'])
        self.assertEqual(second['date_status'],'UNKNOWN')

    def test_run_schema_upgrade_is_idempotent_and_persists_safe_date_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path=Path(temp)/'kalodata.sqlite3'
            import sqlite3
            db=sqlite3.connect(db_path);db.executescript((Path(__file__).parents[1]/'order_pulse/kalodata/migrations/001_init.sql').read_text());db.close()
            repo=Repository(db_path);repo.open(True)
            versions=[x[0] for x in repo.db.execute('SELECT version FROM kalodata_schema_migrations')]
            self.assertEqual(versions,[1,2])
            request={'product_url':'https://www.kalodata.com/product/detail?id=1731358065995256014&region=US','period':'30D','start_page':1,'end_page':1,'session_ref':'plugin-test'}
            repo.create('run1','client1',request,{'visible_pages':None})
            repo.finish('run1','failed','DATE_CONTEXT_UNVERIFIED',{'expected_range':'30D','observed_range':'7D','observed_start':'2026-09-21','observed_end':'2026-09-27','token':'must drop'})
            run=repo.get_run('run1',False)
            self.assertEqual(run['diagnostics'],{'expected_range':'30D','observed_range':'7D','observed_start':'2026-09-21','observed_end':'2026-09-27'})
            repo.close();repo.open(True)
            self.assertEqual([x[0] for x in repo.db.execute('SELECT version FROM kalodata_schema_migrations')],[1,2])
            repo.close()


class FakePlugin:
    session_ref='plugin-test'
    def __init__(self):self.calls=[];self.error=None;self.partial=False;self.gate=None;self.last_success_at=None;self.date_mode='matched';self.request_period='7D'
    async def probe_session(self):return dict(deployable=True,authorized=True,browser='plugin',session_ref=self.session_ref,operations=OPERATIONS)
    async def observe(self,operation,envelope):
        self.calls.append(operation)
        if operation=='apply_period':self.request_period=envelope.get('period','7D')
        if self.gate and operation=='bind_product':await self.gate.wait()
        if self.error:return dict(envelope,success=False,error_code=self.error,data={})
        if self.date_mode=='unknown':period=None;start=end=None;label='';basis='unknown'
        elif self.date_mode=='approximate':period='30D';start='2026-08-29';end='2026-09-26';label='过去30天';basis='page_visible_range'
        else:period='7D';start='2026-01-01';end='2026-01-07';label='过去7天';basis='page_visible_range'
        context=dict(product_id='1',region='US',period=period,actual_start_date=start,actual_end_date=end,site_date_label=label,site_timezone=None,date_basis=basis,page_size=3,visible_total=3,visible_pages=1,observed_sort=[],filters={},source_mode='ui_dom',captured_at=now_iso())
        request=dict(product_url='https://www.kalodata.com/product/detail?id=1&region=US',period=self.request_period,start_page=1,end_page=1)
        context=validate_context(context,request)
        if operation in ('apply_period','verify_context'):data=context
        elif operation=='read_visible_page':
            rows=[]
            for index,video_id in enumerate(['12','11','12'],1):
                row={'video_id':video_id,'kalodata_video_url':f'https://www.kalodata.com/video/detail?id={video_id}'}
                if not(self.partial and index==2):row['link_evidence']=dict(kind='list_anchor',observed_href=f'https://www.tiktok.com/@test/video/{video_id}',source_url=request['product_url'],observed_at=now_iso(),source_page=1,source_index=index)
                else:row['kalodata_video_url']=None
                rows.append(row)
            data=dict(context_fingerprint=context['context_fingerprint'],product_id='1',region='US',source_page=1,page_size=3,visible_pages=1,verified=True,rows=rows,observed_at=now_iso(),terminal_page=True)
        else:data={'bound':True}
        return dict(envelope,success=True,data=data)
    async def cancel(self,*args):pass


class PluginJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();MainRepository(self.tmp.name)
        self.repo=Repository(Path(self.tmp.name)/'order_pulse.sqlite3');self.repo.open(True)
        self.plugin=FakePlugin();self.connector=BrowserProvider(self.plugin,min_interval=0)
        self.service=Service(self.repo,self.connector,enabled=True)
        self.request=dict(product_url='https://www.kalodata.com/product/detail?id=1&region=US',period='7D',start_page=1,end_page=1,client_request_id='request-1')
    async def asyncTearDown(self):await self.service.shutdown();self.repo.close();self.tmp.cleanup()
    async def finish(self):
        tasks=list(self.service.tasks.values())
        if tasks:await asyncio.gather(*tasks)
        return self.repo.by_client('request-1')
    async def test_job_created_before_external_work_and_refresh_does_not_resubmit(self):
        self.plugin.gate=asyncio.Event()
        created=await self.service.extract_product(self.request)
        self.assertEqual(created['status'],'QUEUED');self.assertEqual(self.plugin.calls,[])
        same=await self.service.extract_product(self.request);self.assertEqual(same['run_id'],created['run_id'])
        for _ in range(3):self.repo.get_run(created['run_id']);self.repo.list_runs()
        self.assertEqual(len(self.repo.list_runs()),1)
        self.plugin.gate.set();result=await self.finish();self.assertEqual(result['status'],'SUCCEEDED')
    async def test_single_active_job(self):
        self.plugin.gate=asyncio.Event();await self.service.extract_product(self.request)
        with self.assertRaises(KalodataError) as e:await self.service.extract_product(dict(self.request,client_request_id='request-2'))
        self.assertEqual(e.exception.code,'KALODATA_BUSY');self.plugin.gate.set();await self.finish()
    async def test_success_order_dedupe_exports_persistence(self):
        await self.service.extract_product(self.request);run=await self.finish()
        self.assertEqual(run['transport'],'plugin');self.assertEqual(run['status'],'SUCCEEDED')
        self.assertEqual(run['counts']['visited_rows'],3);self.assertEqual(run['counts']['resolved_unique'],2)
        self.assertEqual([x['video_id'] for x in run['items']],['12','11'])
        expected=['https://www.tiktok.com/@test/video/12','https://www.tiktok.com/@test/video/11']
        self.assertEqual(self.service.links(run['run_id'])['links'],expected)
        self.assertEqual(make_export(run,'txt')[0].decode().splitlines(),expected)
        csv=make_export(run,'csv')[0].decode('utf-8-sig');self.assertLess(csv.index('/video/12'),csv.index('/video/11'))
        self.repo.close();self.repo.open(True);self.assertEqual(self.repo.get_run(run['run_id']),run)
    async def test_date_two_days_late_is_approximate_and_does_not_block(self):
        self.plugin.date_mode='approximate';request=dict(self.request,period='30D');await self.service.extract_product(request);run=await self.finish()
        self.assertEqual(run['status'],'SUCCEEDED_WITH_WARNING');self.assertEqual(run['counts']['visited_rows'],3)
        self.assertEqual(run['query_context']['date_status'],'APPROXIMATE')
    async def test_unknown_date_does_not_block_readable_video_list(self):
        self.plugin.date_mode='unknown';await self.service.extract_product(self.request);run=await self.finish()
        self.assertEqual(run['status'],'SUCCEEDED_WITH_WARNING');self.assertEqual(run['counts']['resolved_unique'],2)
        self.assertEqual(run['query_context']['date_status'],'UNKNOWN')
    async def test_partial_success_keeps_valid_order(self):
        self.plugin.partial=True;await self.service.extract_product(self.request);run=await self.finish()
        self.assertEqual(run['status'],'PARTIAL_SUCCESS');self.assertEqual(run['counts']['resolved_unique'],1)
        self.assertEqual(run['items'][0]['video_id'],'12')
    async def test_schema_error_is_failed_without_fallback(self):
        self.plugin.error='SCHEMA_CHANGED';await self.service.extract_product(self.request);run=await self.finish()
        self.assertEqual(run['status'],'FAILED');self.assertEqual(run['stop_reason'],'SCHEMA_CHANGED')
        self.assertEqual(self.plugin.calls,['bind_product'])
    async def test_login_error_is_failed_without_fallback(self):
        self.plugin.error='LOGIN_REQUIRED';await self.service.extract_product(self.request);run=await self.finish()
        self.assertEqual(run['stop_reason'],'LOGIN_REQUIRED')
    async def test_material_list_failure_remains_blocking(self):
        self.plugin.error='SOURCE_LIST_NOT_READY';await self.service.extract_product(self.request);run=await self.finish()
        self.assertEqual(run['status'],'FAILED');self.assertEqual(run['stop_reason'],'SOURCE_LIST_NOT_READY')
    async def test_cancelled_state(self):
        self.plugin.gate=asyncio.Event();run=await self.service.extract_product(self.request)
        result=await self.service.cancel(run['run_id']);self.assertEqual(result['status'],'CANCELLED')
        self.plugin.gate.set();await self.finish()
    def test_ui_plugin_only_and_background_job_entry(self):
        root=Path(__file__).parents[1]
        for name in ('static/kalodata-settings.js',):
            text=(root/name).read_text()
            for old in ('ego lite','ownership','takeOver','Space','检查本机通道状态','WAITING_USER','/browser-binding'):self.assertNotIn(old,text)
        ui=(root/'static/modules/kalodata/module.js').read_text()
        self.assertIn('打开 ego lite 当前采集浏览器',ui)
        self.assertIn('PLUGIN_RELOAD_REQUIRED',ui)
        self.assertIn("await api('/jobs',request)",ui)
        self.assertNotIn("await api('/preflight'",ui)
        self.assertIn('const latest=model.run||model.runs[0]',ui)


if __name__=='__main__':unittest.main()
