from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = 'linkme-owner-bc'


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def default_settings():
    from .templates import DEFAULT_TEMPLATES, VARIABLES
    return dict(timezone='Asia/Shanghai', source_timezone='Asia/Shanghai',
                source_timezone_confirmed=False, currency='BRL', currency_confirmed=False,
                interval_minutes=15, max_age_minutes=30, log_retention_days=30,
                max_retries=2, paused=True, setup_complete=False,
                schedules=[dict(id=uuid.uuid4().hex, time=t, enabled=False) for t in ['09:00', '14:00', '20:00']],
                fields=list(VARIABLES), templates=dict(DEFAULT_TEMPLATES), amount_format='decimal', keyword='',
                browser_mode='ego_lite', cdp_url='http://127.0.0.1:9222', expected_profile='Order Pulse · Balance + Kalodata',
                account_scope_confirmed=False)


def validate_settings(settings):
    from .templates import validate_templates, VARIABLES, DEFAULT_TEMPLATES, LEGACY_FAILURE_TEMPLATE_KEYS
    allowed = set(default_settings()) | LEGACY_FAILURE_TEMPLATE_KEYS
    if set(settings) - allowed:
        raise ValueError('设置含不支持的字段')
    for name in ['timezone', 'source_timezone']:
        try: ZoneInfo(settings[name])
        except Exception: raise ValueError('时区无效，请使用 Asia/Shanghai 等时区名称')
    for name, low, high in [('interval_minutes',1,1440),('max_age_minutes',1,1440),
                            ('log_retention_days',1,365),('max_retries',0,5)]:
        value=settings.get(name)
        if type(value) not in (int,float) or value != int(value) or not low <= value <= high:
            raise ValueError(f'{name} 必须为 {low} 至 {high} 的整数')
    for name in ['paused','setup_complete','currency_confirmed','source_timezone_confirmed','account_scope_confirmed']:
        if type(settings.get(name)) is not bool: raise ValueError('开关值无效：'+name)
    if not re.fullmatch('[A-Z]{3}', settings.get('currency','')): raise ValueError('币种请输入三个大写字母，如 BRL')
    if settings.get('amount_format') not in ('decimal','local'): raise ValueError('金额格式无效')
    if settings.get('browser_mode') != 'ego_lite': raise ValueError('余额采集只使用已授权的 ego lite 专用Space')
    from urllib.parse import urlsplit
    url=urlsplit(settings.get('cdp_url',''))
    if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost') or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
        raise ValueError('现有会话连接地址必须是本机 HTTP 调试地址')
    if not isinstance(settings.get('schedules'),list) or len(settings['schedules'])>48: raise ValueError('每日最多设置 48 个时间点')
    ids=set(); times=set()
    for s in settings['schedules']:
        if not isinstance(s,dict) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d',s.get('time','')): raise ValueError('发送时间格式应为 HH:MM')
        if not isinstance(s.get('id'),str) or not s['id'] or s['id'] in ids: raise ValueError('时间点编号重复或为空')
        if s['time'] in times: raise ValueError('发送时间不能重复')
        if type(s.get('enabled')) is not bool: raise ValueError('时间点开关无效')
        ids.add(s['id']);times.add(s['time'])
    if not isinstance(settings.get('fields'),list) or len(set(settings['fields']))!=len(settings['fields']) or set(settings['fields'])-set(VARIABLES): raise ValueError('消息字段配置无效')
    templates=settings.get('templates');active=set(DEFAULT_TEMPLATES)
    if not isinstance(templates,dict) or not active<=set(templates)<=active|LEGACY_FAILURE_TEMPLATE_KEYS: raise ValueError('消息模板结构无效')
    # Legacy failure fields are inert data; never render or validate their text.
    if any(not isinstance(templates[k],str) or len(templates[k])>20000 for k in active): raise ValueError('模板过长或不是文本')
    errors=validate_templates(settings['templates'],settings['fields'])
    if errors: raise ValueError('；'.join(errors))
    if len(settings.get('keyword',''))>200: raise ValueError('关键词过长')
    if settings['setup_complete'] and not all(settings[n] for n in ('currency_confirmed','source_timezone_confirmed','account_scope_confirmed')):
        raise ValueError('完成首次配置前，请确认币种、来源时区和账户范围')
    return settings


CONFIG_DEFAULTS = dict(enabled=False,include_summary=False,alert_enabled=False,shop_number='',shop_name='',
                       display_name='',sort_order=0,alert_metric='cash_balance',threshold=None,
                       recovery_enabled=False,currency='BRL',configured=False,revision=0)


def validate_account(patch):
    permitted=set(CONFIG_DEFAULTS)-{'revision','configured'}
    if set(patch)-permitted-{'asset_id','source'}: raise ValueError('账户修改包含不允许的字段')
    if not isinstance(patch.get('asset_id'),str): raise ValueError('资产 ID 必须保留为文本')
    clean={k:v for k,v in patch.items() if k in permitted}
    for key in ('enabled','include_summary','alert_enabled','recovery_enabled'):
        if key in clean and type(clean[key]) is not bool: raise ValueError('账户开关无效')
    for key in ('shop_number','shop_name','display_name'):
        if key in clean and (not isinstance(clean[key],str) or len(clean[key])>500): raise ValueError('账户名称必须是 500 字以内的文本')
    if 'sort_order' in clean and (type(clean['sort_order']) is not int or abs(clean['sort_order'])>1000000): raise ValueError('排序应为整数')
    if 'alert_metric' in clean and clean['alert_metric'] not in ('cash_balance','grant_balance'): raise ValueError('预警依据无效')
    if 'currency' in clean and not re.fullmatch('[A-Z]{3}',clean['currency']): raise ValueError('币种格式无效')
    if 'threshold' in clean:
        value=clean['threshold']
        if value is None or value=='':clean['threshold']=None
        else:
            try:
                if not isinstance(value,str):raise ValueError()
                amount=Decimal(value)
                if not amount.is_finite() or amount<0 or amount>Decimal('999999999999.99') or amount!=amount.quantize(Decimal('.01')): raise ValueError()
                clean['threshold']=format(amount,'.2f')
            except (ValueError,InvalidOperation): raise ValueError('额度应为非负金额，最多两位小数；空白表示未配置')
    return clean


class Store:
    def __init__(self, root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.root.chmod(0o700)
        self.path=self.root/'order_pulse.sqlite3'
        self.lock=threading.RLock()
        self._transaction_depth=0
        self.db=sqlite3.connect(str(self.path),check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS balance_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS balance_accounts (source TEXT NOT NULL, asset_id TEXT NOT NULL, data TEXT NOT NULL, config TEXT NOT NULL, alert TEXT NOT NULL, PRIMARY KEY(source,asset_id));
          CREATE TABLE IF NOT EXISTS balance_logs (id INTEGER PRIMARY KEY, at TEXT NOT NULL, level TEXT, kind TEXT, message TEXT, task_id TEXT, details TEXT);
          CREATE TABLE IF NOT EXISTS balance_push_events (id TEXT PRIMARY KEY, kind TEXT, created_at TEXT, status TEXT, reason TEXT, attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS balance_schedules (key TEXT PRIMARY KEY, at TEXT, status TEXT, event_id TEXT);
        '''); self.db.commit();self.path.chmod(0o600)
        if self.get('settings') is None:self.set('settings',default_settings())

    def recover_sending(self):
        with self.transaction() as db:
            db.execute("UPDATE balance_push_events SET status='uncertain',reason='程序在发送期间中断，飞书可能已收到；未自动重复发送' WHERE status='sending'")
            db.execute("UPDATE balance_push_events SET status='cancelled',reason='余额采集故障通知已停用，保留历史但不发送' WHERE kind IN ('failure','system_recovery') AND status='pending'")

    @contextmanager
    def transaction(self):
        with self.lock:
            self._transaction_depth+=1
            try:
                yield self.db
                if self._transaction_depth==1:self.db.commit()
            except BaseException:
                if self._transaction_depth==1:self.db.rollback()
                raise
            finally:self._transaction_depth-=1

    def get(self,key,default=None):
        with self.lock:
            row=self.db.execute('SELECT value FROM balance_kv WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self,key,value):
        with self.transaction() as db:db.execute('INSERT OR REPLACE INTO balance_kv VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))

    def settings(self):return self.get('settings')

    def log(self,kind,message,level='info',task_id=None,**details):
        # Callers supply short controlled messages; secrets are never logged as exception reprs.
        with self.transaction() as db:db.execute('INSERT INTO balance_logs(at,level,kind,message,task_id,details) VALUES(?,?,?,?,?,?)',
            (now_iso(),level,kind,message,task_id,json.dumps(details,ensure_ascii=False)))

    def logs(self):
        with self.lock:
            return [dict(r,details=json.loads(r['details'])) for r in self.db.execute('SELECT * FROM balance_logs ORDER BY id DESC LIMIT 300')]

    def raw_accounts(self):
        with self.lock:return [dict(source=r['source'],asset_id=r['asset_id'],data=json.loads(r['data']),config=json.loads(r['config']),alert=json.loads(r['alert'])) for r in self.db.execute('SELECT * FROM balance_accounts')]

    def accounts(self):
        settings=self.settings();rows=[];last=self.get('collection',{})
        for r in self.raw_accounts():
            a=dict(r['data'],**r['config'],source=r['source'],asset_id=r['asset_id'],balance_status=r['alert'].get('state','unknown'))
            status=a.get('data_status','missing')
            stamp=parse_time(a.get('source_updated_at'))
            if status=='valid' and (not stamp or (datetime.now(timezone.utc)-stamp).total_seconds()>settings['max_age_minutes']*60):status='stale'
            if status=='valid' and last.get('status')=='failed':status='failed'
            a['data_status']=status
            metric=a.get('alert_metric','cash_balance')
            if status=='valid' and a.get('threshold') is not None and a.get(metric) is not None:
                a['balance_status']='low' if Decimal(a[metric])<Decimal(a['threshold']) else 'normal'
            elif status!='valid':a['balance_status']='unknown'
            rows.append(a)
        return sorted(rows,key=lambda a:(a['sort_order'],a['source_name'],a['asset_id']))

    def update_accounts(self, patches):
        if not isinstance(patches,list) or not patches:raise ValueError('请选择账户')
        validated=[(p,validate_account(p)) for p in patches]
        with self.transaction() as db:
            for p,clean in validated:
                source=p.get('source',SOURCE)
                row=db.execute('SELECT config,alert,data FROM balance_accounts WHERE source=? AND asset_id=?',(source,p['asset_id'])).fetchone()
                if not row:raise ValueError('未找到账户 '+p['asset_id'])
                config=json.loads(row['config']);alert=json.loads(row['alert'])
                source_currency=json.loads(row['data']).get('source_currency',config['currency'])
                if 'currency' in clean and clean['currency']!=source_currency:
                    raise ValueError('账户币种必须与已核验的数据源一致；请在推送设置中核对来源币种后重新采集')
                changed=any(config.get(k)!=v for k,v in clean.items())
                if any(k in clean and clean[k]!=config.get(k) for k in ('threshold','alert_metric')):alert['settings_changed']=True
                if changed:
                    for event in db.execute("SELECT payload FROM balance_push_events WHERE kind IN ('alert','recovery') AND status IN ('pending','sending')").fetchall():
                        payload=json.loads(event['payload'])
                        if payload.get('source')==source and payload.get('asset_id')==p['asset_id']:
                            alert['needs_initial']=True;alert['settings_changed']=True
                            break
                if clean.get('alert_enabled') and not config.get('alert_enabled'):alert['needs_initial']=True
                if clean.get('enabled') and not config.get('enabled'):alert['needs_initial']=True
                config.update(clean);config['configured']=True
                if changed:config['revision']+=1
                db.execute('UPDATE balance_accounts SET config=?,alert=? WHERE source=? AND asset_id=?',(json.dumps(config,ensure_ascii=False),json.dumps(alert),source,p['asset_id']))

    def ingest(self,result,meta,sample=False):
        ids={a['asset_id'] for a in result['accounts']}
        settings=self.settings();missing=[]
        with self.transaction() as db:
            for existing in db.execute('SELECT * FROM balance_accounts WHERE source=?',(SOURCE,)).fetchall():
                if existing['asset_id'] not in ids:
                    data=json.loads(existing['data']);data['data_status']='missing'
                    db.execute('UPDATE balance_accounts SET data=? WHERE source=? AND asset_id=?',(json.dumps(data,ensure_ascii=False),SOURCE,existing['asset_id']))
                    if json.loads(existing['config'])['enabled']:missing.append(existing['asset_id'])
            for a in result['accounts']:
                missing_balances=[k for k in ('cash_balance','grant_balance') if a.get(k) is None]
                data=dict(a,**meta,data_status='sample' if sample else ('missing' if missing_balances else 'valid'),source_currency=settings['currency'])
                if meta.get('data_source')=='json':
                    # A fresh list must not hide an older account balance timestamp.
                    data['list_source_updated_at']=meta['source_updated_at']
                    data['source_updated_at']=a['balance_updated_at']
                if missing_balances:data['data_error']='现金或赠款余额未提供，不能判定余额状态'
                row=db.execute('SELECT config,alert FROM balance_accounts WHERE source=? AND asset_id=?',(SOURCE,a['asset_id'])).fetchone()
                if row:config=json.loads(row['config']);alert=json.loads(row['alert'])
                else:
                    config=dict(CONFIG_DEFAULTS,currency=settings['currency'])
                    if a['asset_id']=='7641475860268400660':config.update(shop_number='18店',shop_name='Samba Glow')
                    alert=dict(state='unknown',generation=0,needs_initial=True)
                config['currency']=settings['currency']
                db.execute('INSERT OR REPLACE INTO balance_accounts(source,asset_id,data,config,alert) VALUES (?,?,?,?,?)',(SOURCE,a['asset_id'],json.dumps(data,ensure_ascii=False),json.dumps(config,ensure_ascii=False),json.dumps(alert)))
        return missing

    def set_alert(self,source,asset_id,alert,db=None):
        if db is not None:db.execute('UPDATE balance_accounts SET alert=? WHERE source=? AND asset_id=?',(json.dumps(alert),source,asset_id))
        else:
            with self.transaction() as tx:self.set_alert(source,asset_id,alert,tx)

    def event(self,kind,payload,event_id=None,db=None):
        event_id=event_id or uuid.uuid4().hex
        payload=dict(payload)
        payload.setdefault('settings_revision',self.get('settings_revision',0))
        args=(event_id,kind,now_iso(),'pending','等待发送',0,json.dumps(payload,ensure_ascii=False))
        if db is not None:db.execute('INSERT OR IGNORE INTO balance_push_events(id,kind,created_at,status,reason,attempts,payload) VALUES(?,?,?,?,?,?,?)',args)
        else:
            with self.transaction() as tx:tx.execute('INSERT OR IGNORE INTO balance_push_events(id,kind,created_at,status,reason,attempts,payload) VALUES(?,?,?,?,?,?,?)',args)
        return event_id

    def events(self,include_payload=False):
        with self.lock:
            rows=[dict(r) for r in self.db.execute('SELECT * FROM balance_push_events ORDER BY created_at DESC LIMIT 300')]
        for r in rows:
            if include_payload:r['payload']=json.loads(r['payload'])
            else:r.pop('payload')
        return rows

    def pending_events(self):
        with self.lock:
            return [dict(r,payload=json.loads(r['payload'])) for r in self.db.execute("SELECT * FROM balance_push_events WHERE status='pending' ORDER BY created_at,id")]

    def update_event(self,event_id,status,reason,attempts=0,payload=None):
        with self.transaction() as db:
            db.execute('UPDATE balance_push_events SET status=?,reason=?,attempts=? WHERE id=?',(status,reason,attempts,event_id))
            if payload is not None:db.execute('UPDATE balance_push_events SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),event_id))

    def purge(self):
        cutoff=(datetime.now(timezone.utc)-timedelta(days=self.settings()['log_retention_days'])).isoformat()
        with self.transaction() as db:
            db.execute('DELETE FROM balance_logs WHERE at<?',(cutoff,))
            db.execute("DELETE FROM balance_push_events WHERE created_at<? AND status NOT IN ('pending','sending')",(cutoff,))
            # Schedule identities are kept to prevent repeats after clock/timezone edits.

    def backup(self):
        return dict(version=1,created_at=now_iso(),settings=self.settings(),accounts=[dict(source=r['source'],asset_id=r['asset_id'],**{k:v for k,v in r['config'].items() if k not in ('configured','revision')}) for r in self.raw_accounts()])

    def close(self):self.db.close()
