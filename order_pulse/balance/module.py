from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from .bridge import BrowserBridge
from .engine import Engine, account_values
from .store import Store, validate_account, validate_settings
from .templates import VARIABLES, render_template
from ..feishu import split_messages, validate_webhook
from ..integrations.ego_runtime import BALANCE_SPACE_NAMES


class ScopedSecrets:
    """One branch inside the Order Pulse SecretStore."""
    def __init__(self, credentials, branch):
        self.credentials=credentials;self.branch=branch

    def get(self):
        value=self.credentials.namespace('balance').get(self.branch,{})
        return dict(value) if isinstance(value,dict) else {}

    def save(self,value):
        namespace=self.credentials.namespace('balance')
        namespace[self.branch]=dict(value)
        self.credentials.save_namespace('balance',namespace)

    def update(self,payload):
        data={} if payload.get('clear') else self.get()
        if payload.get('webhook'):
            validate_webhook(payload['webhook']);data['webhook']=payload['webhook']
        if payload.get('secret'):data['secret']=payload['secret']
        if payload.get('clear_secret'):data.pop('secret',None)
        self.save(data)

    def status(self):
        value=self.get()
        return dict(webhook_configured=bool(value.get('webhook')),secret_configured=bool(value.get('secret')))


class BalanceModule:
    def __init__(self,root,credentials,browser_lock=None,browser_binding=None):
        self.store=Store(root)
        current=self.store.settings()
        if current.get('browser_mode')!='ego_lite' or current.get('expected_profile') not in BALANCE_SPACE_NAMES:
            # Module-local compatibility migration.  Preserve every business and
            # scheduler value while replacing the retired extension transport.
            current=dict(current,browser_mode='ego_lite',expected_profile=BALANCE_SPACE_NAMES[0])
            self.store.set('settings',current)
        self.feishu=ScopedSecrets(credentials,'feishu')
        self.bridge=BrowserBridge(self.store,browser_lock=browser_lock,browser_binding=browser_binding)
        self.engine=Engine(self.store,self.bridge,self.feishu)
        self.started=False
        self.migration_status='NOT_REQUIRED'

    async def startup(self):
        self.store.recover_sending()
        previous=self.store.get('heartbeat')
        if previous:
            try:
                if (datetime.now(timezone.utc)-datetime.fromisoformat(previous)).total_seconds()>60:
                    self.store.log('interruption','后台曾停止、休眠或中断，恢复后将先采集再处理到期任务',level='warning',previous_heartbeat=previous)
            except (TypeError,ValueError):pass
        self.started=True

    async def shutdown(self):
        await self.engine.shutdown();self.store.close();self.started=False

    async def tick(self,now=None):
        if self.started:await self.engine.tick(now)

    def state(self):
        return dict(settings=self.store.settings(),accounts=self.store.accounts(),status=self.engine.status(),
                    logs=self.store.logs(),events=self.store.events(),variables=VARIABLES,
                    connection=dict(self.bridge.status(),**self.feishu.status(),last_success=self.store.get('feishu_success'),
                                    last_failure=self.store.get('feishu_failure')),
                    module_status=('migration_failed' if self.migration_status=='MIGRATION_FAILED' else
                                   'running' if self.started else 'stopped'),
                    migration_status=self.migration_status)

    def save_settings(self,payload):
        if not isinstance(payload,dict):raise ValueError('余额设置字段无效')
        from .templates import LEGACY_FAILURE_TEMPLATE_KEYS
        old=self.store.settings()
        active={k:v for k,v in payload.items() if k not in LEGACY_FAILURE_TEMPLATE_KEYS}
        if isinstance(active.get('templates'),dict):
            active['templates']=dict(old['templates'],**{
                k:v for k,v in active['templates'].items() if k not in LEGACY_FAILURE_TEMPLATE_KEYS})
        merged=dict(old,**active);validate_settings(merged)
        if not merged['paused'] and not merged['setup_complete']:raise ValueError('请先完成首次配置，再恢复自动任务')
        with self.store.transaction() as db:
            self.store.set('settings',merged);self.store.set('settings_revision',self.store.get('settings_revision',0)+1)
            if any(merged[k]!=old[k] for k in ('currency','source_timezone')):
                for row in db.execute('SELECT source,asset_id,data FROM balance_accounts').fetchall():
                    data=json.loads(row['data'])
                    if data.get('data_status')=='valid':data['data_status']='stale'
                    db.execute('UPDATE balance_accounts SET data=? WHERE source=? AND asset_id=?',(json.dumps(data,ensure_ascii=False),row['source'],row['asset_id']))
        if merged['setup_complete'] and not old['setup_complete']:self.store.set('schedule_cursor',datetime.now(timezone.utc).isoformat())
        self.store.log('settings','余额设置已保存')
        return {'message':'余额设置已保存，立即生效'}

    def save_accounts(self,patches):
        self.store.update_accounts(patches);self.store.log('settings','账户配置已保存')
        if not self.store.settings()['paused']:
            self.engine.evaluate_alerts()
        return {'message':'账户配置已保存'}

    def save_credentials(self,payload):
        if not isinstance(payload,dict) or set(payload)-{'webhook','secret','clear','clear_secret'}:
            raise ValueError('飞书设置包含不支持的字段')
        self.feishu.update(payload);self.store.set('feishu_failure',None)
        self.store.set('credentials_revision',self.store.get('credentials_revision',0)+1)
        self.store.log('settings','余额飞书凭据已更新（不记录凭据内容）')
        return {'message':'余额飞书配置已保存'}

    async def action(self,name):
        settings=self.store.settings()
        if name in ('query','push'):
            if not self.bridge.target:raise ValueError('请先绑定余额监控专用空间中的 Linkme 页面')
            if name=='push' and not self.feishu.status()['webhook_configured']:raise ValueError('请先配置余额飞书机器人')
            accepted=self.engine.request(name,summary=name=='push')
            return {'message':'正在采集；本轮成功后发送余额汇总' if name=='push' else '正在采集余额',
                    'accepted':accepted}
        if name in ('pause','resume'):
            if name=='resume':
                if not settings['setup_complete']:raise ValueError('请先完成余额配置')
                if not self.feishu.status()['webhook_configured']:raise ValueError('请先配置余额飞书机器人')
                if not self.bridge.target:raise ValueError('请先绑定余额监控专用空间中的 Linkme 页面')
            settings['paused']=name=='pause';self.store.set('settings',settings)
            if name=='resume':self.store.set('last_detection',None)
            self.store.log('service','余额自动任务已暂停' if name=='pause' else '余额自动任务已恢复')
            return {'message':'余额自动任务已暂停' if name=='pause' else '余额自动任务已恢复'}
        if name=='connect':
            inspection=await self.bridge.inspect()
            return {'message':'ego lite Linkme 页面连接已核验','inspection':inspection}
        if name=='cancel':
            if self.engine.cycle and not self.engine.cycle.done():self.engine.cycle.cancel()
            return {'message':'已请求取消余额采集'}
        raise ValueError('余额操作不受支持')

    def bind_browser(self,payload):
        return self.bridge.set_target(payload)

    def preview(self,payload):
        settings=dict(self.store.settings(),**(payload.get('settings',{}) if isinstance(payload,dict) else {}))
        validate_settings(settings)
        rows=self.store.accounts();a=rows[0] if rows else None
        if not a:return {'text':'暂无账户数据','messages':[],'errors':[]}
        values=account_values(a,settings,event_id='PREVIEW')
        title=render_template(settings['templates']['title'],values,settings['fields'])
        body=render_template(settings['templates']['account'],values,settings['fields'])
        messages=split_messages([body],title,'PREVIEW',settings['keyword'])
        return {'text':'\n\n'.join(messages),'messages':messages,'errors':[]}

    async def wait_idle(self,timeout=180):
        deadline=asyncio.get_running_loop().time()+timeout
        while self.engine.cycle and not self.engine.cycle.done():
            if asyncio.get_running_loop().time()>=deadline:raise TimeoutError('余额采集等待超时')
            await asyncio.sleep(.2)
        return self.state()
