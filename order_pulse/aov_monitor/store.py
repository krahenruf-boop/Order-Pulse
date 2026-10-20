from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3
import uuid
from .domain import WINDOWS, rule, ruleset, monitor_scope, digest, group_scope_hash


def now(): return datetime.now(timezone.utc).isoformat()
def dump(value): return json.dumps(value,ensure_ascii=False,separators=(',',':'))
DEFAULT = {'revision':1,'enabled':False,'shops':[]}


class Store:
    def __init__(self, path):
        self.path=path
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS aov_monitor_settings(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aov_monitor_runs(id TEXT PRIMARY KEY,slot TEXT UNIQUE,at TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aov_monitor_snapshots(run_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aov_monitor_alerts(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aov_monitor_events(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aov_monitor_orders(run_id TEXT NOT NULL,shop_id TEXT NOT NULL,order_key TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(run_id,shop_id,order_key));
            CREATE TABLE IF NOT EXISTS aov_monitor_fx(currency TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aov_monitor_verified_scopes(key TEXT PRIMARY KEY,scope_hash TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO aov_monitor_settings VALUES(1,?)',(dump(DEFAULT),))

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            with db: yield db
        finally: db.close()

    def config(self):
        with self.db() as db:return json.loads(db.execute('SELECT payload FROM aov_monitor_settings WHERE id=1').fetchone()[0])

    def _seed_verified(self,db,config):
        """Retain real acceptance from the original whole-scope gate."""
        scope_hash=digest(monitor_scope(config['shops']))
        for (raw,) in db.execute('SELECT payload FROM aov_monitor_snapshots ORDER BY rowid DESC'):
            proof=json.loads(raw)
            if (proof.get('status')=='SUCCEEDED' and proof.get('real_sources_verified')
                    and proof.get('scope_hash')==scope_hash):
                for shop in config['shops']:
                    if shop['monitor']:
                        for product in shop['products']:
                            db.execute('INSERT OR IGNORE INTO aov_monitor_verified_scopes VALUES(?,?)',
                                       (shop['id']+':'+product['family_id'],group_scope_hash(shop,product)))
                break

    def verification(self,config):
        with self.db() as db:
            self._seed_verified(db,config)
            verified=dict(db.execute('SELECT key,scope_hash FROM aov_monitor_verified_scopes'))
        ready=[];pending=[]
        for shop in config['shops']:
            if not shop['monitor']:continue
            for product in shop['products']:
                key=shop['id']+':'+product['family_id']
                (ready if verified.get(key)==group_scope_hash(shop,product) else pending).append(key)
        return dict(ready=ready,pending=pending)

    def save_config(self, value, catalog, stores):
        if not isinstance(value,dict) or set(value)-{'revision','enabled','shops'}:raise ValueError('监控设置无效')
        if type(value.get('enabled')) is not bool or type(value.get('revision')) is not int:raise ValueError('监控设置无效')
        shops=value.get('shops');known={str(s['shop_id']):s for s in stores if s.get('platform_id')==104}
        if not isinstance(shops,list) or len(shops)>200:raise ValueError('店铺配置无效')
        result=[];ids=set();ms_ids=set()
        for s in shops:
            if not isinstance(s,dict):raise ValueError('店铺配置无效')
            sid=s.get('id')
            if not isinstance(sid,str) or sid not in known or sid in ids:raise ValueError('店铺重复或不在授权目录')
            ids.add(sid)
            if any(type(s.get(k)) is not bool for k in ('monitor','push','mapping_confirmed')):raise ValueError('店铺开关无效')
            if s['push'] and not s['monitor']:raise ValueError('推送店铺必须参与监控')
            ms=s.get('miaoshou_shop_id','')
            if not isinstance(ms,str) or len(ms)>200:raise ValueError('妙手店铺标识无效')
            if ms and ms in ms_ids:raise ValueError('同一妙手店铺不能重复映射')
            if ms:ms_ids.add(ms)
            ms_name=s.get('miaoshou_shop_name','')
            if not isinstance(ms_name,str) or len(ms_name)>200:raise ValueError('妙手店铺名称无效')
            currency=s.get('currency','BRL')
            if currency not in ('BRL','MXN','THB','VND','USD','GBP','EUR','MYR','SGD','PHP','IDR','JPY','CNY'):
                raise ValueError('请选择支持的市场币种')
            products=[];seen=set()
            if not isinstance(s.get('products',[]),list):raise ValueError('产品配置无效')
            for p in s.get('products',[]):
                if not isinstance(p,dict):raise ValueError('产品配置无效')
                fid=p.get('family_id')
                if not isinstance(fid,str) or fid not in catalog or fid in seen:raise ValueError('产品组无效或重复')
                seen.add(fid);f=catalog[fid]
                label=p.get('display_name','')
                if not isinstance(label,str) or len(label)>100 or any(ord(c)<32 for c in label):
                    raise ValueError('产品显示名称须为100字以内的单行文字')
                product=dict(family_id=fid,name=f['product_name'],skus=sorted(f['member_skus']),
                             rules=ruleset(p.get('rules',{})))
                if label.strip():product['display_name']=label.strip()
                products.append(product)
            if s['monitor'] and (not products or not ms or not ms_name.strip() or not s['mapping_confirmed']):
                raise ValueError('启用前请选择产品并确认妙手店铺映射')
            aliases=s.get('sku_aliases',{'tms':{},'miaoshou':{}})
            # A product can be unchecked without deleting a confirmed alias.
            all_skus={k for p in catalog.values() for k in p['member_skus']}
            if not isinstance(aliases,dict) or set(aliases)-{'tms','miaoshou'}:raise ValueError('SKU映射无效')
            for mapping in aliases.values():
                if not isinstance(mapping,dict) or any(not isinstance(k,str) or not k or len(k)>256 or not isinstance(v,str) or v not in all_skus for k,v in mapping.items()):raise ValueError('SKU映射须指向现有产品成员')
            result.append(dict(id=sid,name=known[sid]['name'],tms_shop_id=sid,miaoshou_shop_id=ms,miaoshou_shop_name=ms_name.strip(),
                               currency=currency,monitor=s['monitor'],push=s['push'],mapping_confirmed=s['mapping_confirmed'],
                               sku_aliases=aliases,products=products))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=json.loads(db.execute('SELECT payload FROM aov_monitor_settings WHERE id=1').fetchone()[0])
            if old['revision']!=value['revision']:raise ValueError('设置已在其他页面修改，请重新载入')
            self._seed_verified(db,old)
            new=dict(revision=old['revision']+1,enabled=value['enabled'],shops=result)
            scope_changed=monitor_scope(old['shops'])!=monitor_scope(new['shops'])
            if new['enabled'] and not any(s['monitor'] for s in result):raise ValueError('请先选择监控店铺')
            # Save valid settings first. The scheduler admits only verified
            # shop/product scopes; new or changed scopes remain pending.
            db.execute('UPDATE aov_monitor_settings SET payload=? WHERE id=1',(dump(new),))
            if scope_changed or old['enabled']!=new['enabled']:
                db.execute('DELETE FROM aov_monitor_alerts')
        return new

    def start(self, slot, payload):
        rid=uuid.uuid4().hex
        with self.db() as db:
            try:db.execute('INSERT INTO aov_monitor_runs VALUES(?,?,?,?,?)',(rid,slot,now(),'RUNNING',dump(payload)))
            except sqlite3.IntegrityError:return None
        return rid

    def finish(self,rid,payload,states=None,events=(),orders=None):
        with self.db() as db:
            db.execute('UPDATE aov_monitor_runs SET status=?,payload=? WHERE id=?',(payload['status'],dump(payload),rid))
            db.execute('INSERT OR REPLACE INTO aov_monitor_snapshots VALUES(?,?)',(rid,dump(payload)))
            for sid,rows in (orders or {}).items():
                for row in rows:db.execute('INSERT OR REPLACE INTO aov_monitor_orders VALUES(?,?,?,?)',(rid,sid,row['order_key'],dump(row)))
            for key,state in (states or {}).items():db.execute('INSERT OR REPLACE INTO aov_monitor_alerts VALUES(?,?)',(key,dump(state)))
            for event in events:db.execute('INSERT OR IGNORE INTO aov_monitor_events VALUES(?,?,?,?)',(event['id'],rid,'pending',dump(event)))
            if payload.get('trigger')=='manual' and payload.get('real_sources_verified'):
                for group in payload.get('groups',[]):
                    if group.get('status')=='COMPLETE' and group.get('verification_hash'):
                        db.execute('INSERT OR REPLACE INTO aov_monitor_verified_scopes VALUES(?,?)',
                                   (group['key'],group['verification_hash']))

    def history(self):
        with self.db() as db:return [dict(id=r[0],slot=r[1],at=r[2],status=r[3]) for r in db.execute('SELECT id,slot,at,status FROM aov_monitor_runs ORDER BY rowid DESC LIMIT 100')]
    def snapshot(self,rid=None):
        with self.db() as db:
            r=db.execute('SELECT payload FROM aov_monitor_snapshots WHERE run_id=?',(rid,)).fetchone() if rid else db.execute('SELECT payload FROM aov_monitor_snapshots ORDER BY rowid DESC LIMIT 1').fetchone()
            return json.loads(r[0]) if r else None
    def orders(self,rid,shop_id,page=1):
        if not 1<=page<=1000:raise ValueError('页码无效')
        with self.db() as db:
            count=db.execute('SELECT COUNT(*) FROM aov_monitor_orders WHERE run_id=? AND shop_id=?',(rid,shop_id)).fetchone()[0]
            rows=db.execute('SELECT payload FROM aov_monitor_orders WHERE run_id=? AND shop_id=? ORDER BY order_key LIMIT 50 OFFSET ?',(rid,shop_id,(page-1)*50)).fetchall()
            return dict(total=count,page=page,items=[json.loads(r[0]) for r in rows],details_cleared=not count,message='历史统计结果保留；本次未保留订单明细，避免重复占用空间。' if not count else None)
    def alert_states(self):
        with self.db() as db:return {k:json.loads(v) for k,v in db.execute('SELECT key,payload FROM aov_monitor_alerts')}
    def events(self):
        with self.db() as db:return [dict(json.loads(v),status=s) for s,v in db.execute('SELECT status,payload FROM aov_monitor_events ORDER BY rowid DESC LIMIT 100')]
    def event_status(self,eid,status,reason=None):
        with self.db() as db:
            r=db.execute('SELECT payload FROM aov_monitor_events WHERE id=?',(eid,)).fetchone()
            p=json.loads(r[0]);p['reason']=reason
            db.execute('UPDATE aov_monitor_events SET status=?,payload=? WHERE id=?',(status,dump(p),eid))
    def recover(self):
        with self.db() as db:
            db.execute("UPDATE aov_monitor_runs SET status='INTERRUPTED' WHERE status='RUNNING'")
            # Network delivery may have completed just before a crash. Never
            # automatically resend an uncertain notification.
            db.execute("UPDATE aov_monitor_events SET status='uncertain' WHERE status IN ('sending','pending')")
            for key,payload in db.execute('SELECT key,payload FROM aov_monitor_alerts').fetchall():
                p=json.loads(payload);p.update(candidate=None,streak=0)
                db.execute('UPDATE aov_monitor_alerts SET payload=? WHERE key=?',(dump(p),key))
    def fx(self,currency,value=None):
        with self.db() as db:
            if value is not None:db.execute('INSERT OR REPLACE INTO aov_monitor_fx VALUES(?,?)',(currency,dump(value)))
            r=db.execute('SELECT payload FROM aov_monitor_fx WHERE currency=?',(currency,)).fetchone()
            return json.loads(r[0]) if r else None
