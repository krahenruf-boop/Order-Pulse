from __future__ import annotations

import json
from pathlib import Path
from .connectors.browser_provider import BrowserProvider
from .connectors.plugin_client import KalodataPluginClient
from .lifecycle import create_runtime


DEFAULTS={'enabled':True,'default_period':'30D','default_start_page':1,'default_end_page':1,
          'output_order':'source_rank','save_video_files':False,'paid_api_enabled':False,
          'transport':'plugin'}
PERIODS={'YESTERDAY','7D','30D','90D','180D','365D'}


class KalodataModule:
    def __init__(self,root,repo,credentials,production=None):
        self.root=Path(root);self.repo=repo
        self.production=(self.root.resolve()==(Path.home()/'Library/Application Support/Order Pulse').resolve()) if production is None else production
        legacy=(Path.home()/'Library/Application Support/TikTokOPS-Kalodata-Pilot/modules/kalodata/extension-auth.json') if self.production else None
        self.transport=KalodataPluginClient(root,credentials,legacy_auth=legacy)
        self.runtime=create_runtime(root,enabled=self.settings()['enabled'],connector=BrowserProvider(self.transport))
        self.started=False
        self.migration_status='NOT_REQUIRED'

    def settings(self):
        with self.repo.db() as db:
            rows={r[0]:json.loads(r[1]) for r in db.execute('SELECT key,value_json FROM kalodata_settings')}
        return {**DEFAULTS,**{k:v for k,v in rows.items() if k in DEFAULTS},'transport':'plugin'}

    def save_settings(self,payload):
        if not isinstance(payload,dict) or set(payload)-set(DEFAULTS):raise ValueError('素材情报设置字段无效')
        value=dict(self.settings(),**payload)
        if type(value['enabled']) is not bool:raise ValueError('素材情报开关无效')
        if value['default_period'] not in PERIODS:raise ValueError('默认时间范围无效')
        for key in ('default_start_page','default_end_page'):
            if type(value[key]) is not int or not 1<=value[key]<=1000:raise ValueError('默认页码无效')
        if value['default_start_page']>value['default_end_page']:raise ValueError('结束页不能早于起始页')
        if value['transport']!='plugin':raise ValueError('素材情报仅使用现有插件采集')
        if value['output_order']!='source_rank' or value['save_video_files'] is not False or value['paid_api_enabled'] is not False:
            raise ValueError('第一版固定按原排名输出，不下载视频且不使用付费API')
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for key,item in value.items():db.execute('INSERT OR REPLACE INTO kalodata_settings VALUES(?,?)',(key,json.dumps(item,ensure_ascii=False)))
        return value

    async def startup(self):
        if self.production:await self.transport.start_bridge()
        await self.runtime.startup();self.started=True
        self.transport.last_success_at=await self.runtime.call_repo('state','plugin_last_success_at')

    async def shutdown(self):
        await self.runtime.shutdown()
        await self.transport.stop_bridge()
        self.started=False

    def connection(self):
        return self.transport.info()

    def reset_connection(self):
        return self.connection()

    def bind_browser(self,payload):
        raise ValueError('素材情报使用已连接的插件，无需浏览器空间绑定')

    async def status(self):
        cap=await self.runtime.call('capabilities') if self.started else {'enabled':self.settings()['enabled'],'transport_ready':False}
        module_status=('migration_failed' if self.migration_status=='MIGRATION_FAILED' else
                       'ready' if cap.get('transport_ready') else 'waiting_connection')
        connection=self.connection()
        runs=await self.runtime.call_repo('list_runs',20)
        connection['active_job']=next((r['run_id'] for r in runs if r['status'] in ('QUEUED','RUNNING')),None)
        connection['last_success_at']=await self.runtime.call_repo('state','plugin_last_success_at')
        return dict(capabilities=cap,connection=connection,settings=self.settings(),
                    module_status=module_status,migration_status=self.migration_status)

    async def health(self):
        current=await self.status()
        if current['connection']['active_job']:return current
        await self.runtime.dispatch(self.transport.health,True)
        return await self.status()
