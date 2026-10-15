"""Additive card configuration and resumable batch queue on the existing database."""
from copy import deepcopy
import json
from .store import Store,dump,now
from .cards import migrate_config,collection_mapping,apply_mapping
from .domain import WINDOWS,LOOKBACK_HOURS,ruleset,rule,digest,instant
from datetime import timedelta
from .cycle import is_cycle,cycle_hours,order_identity

def collection_scope(c):
    return [{'mode':c.get('collection_mode','ROLLING'),'cycle_hours':c.get('cycle_hours',2)}]+[{k:s.get(k) for k in ('id','miaoshou_shop_id','source_mode','mapping_choice','sku_aliases','currency','default_rules')}|{'products':[{k:p.get(k) for k in ('family_id','override','rules')} for p in s['products'] if p.get('override',True)]} for s in c['shops']]

class CardStore(Store):
    def __init__(self,path):
        super().__init__(path)
        from .incremental import setup
        with self.db() as db:setup(db)
        from .refund_estimates import setup as setup_refunds
        with self.db() as db:setup_refunds(db)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS aov_monitor_directory(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aov_monitor_jobs(run_id TEXT NOT NULL,id TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,result TEXT,PRIMARY KEY(run_id,id));
            CREATE TABLE IF NOT EXISTS aov_monitor_cycle_coverage(run_id TEXT NOT NULL,shop_id TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,identity_hash TEXT NOT NULL,alert_eligible INTEGER NOT NULL,saved_at TEXT NOT NULL,PRIMARY KEY(run_id,shop_id));
            CREATE INDEX IF NOT EXISTS aov_cycle_shop_end ON aov_monitor_cycle_coverage(shop_id,end);''')
    def prepare(self,stores):
        directory=self.directory()
        shops=directory['tms'] if directory.get('status')=='COMPLETE' else [dict(id=str(s['shop_id']),name=s['name']) for s in stores if s.get('platform_id')==104]
        with self.db() as db:
            c=json.loads(db.execute('SELECT payload FROM aov_monitor_settings WHERE id=1').fetchone()[0]);m=migrate_config(c,shops)
            if directory.get('status')=='COMPLETE':
                mapping=collection_mapping(directory,{s['id']:s['mapping_choice'] for s in m['shops'] if s.get('mapping_choice')})
                apply_mapping(m['shops'],mapping)
                if directory.get('mapping')!=mapping:
                    directory['mapping']=mapping;db.execute('UPDATE aov_monitor_directory SET payload=? WHERE id=1',(dump(directory),))
            if m!=c:
                m['revision']=c['revision']+1;db.execute('UPDATE aov_monitor_settings SET payload=? WHERE id=1',(dump(m),))
            return m
    def directory(self,value=None):
        with self.db() as db:
            if value is not None:db.execute('INSERT OR REPLACE INTO aov_monitor_directory VALUES(1,?)',(dump(value),))
            r=db.execute('SELECT payload FROM aov_monitor_directory WHERE id=1').fetchone()
            return json.loads(r[0]) if r else {}
    def apply_directory(self,value):
        value=deepcopy(value);value['checked_at']=now();value['status']='COMPLETE'
        with self.db() as db:
            c=json.loads(db.execute('SELECT payload FROM aov_monitor_settings WHERE id=1').fetchone()[0])
            c=migrate_config(c,value['tms']);mapping=collection_mapping(value,{s['id']:s['mapping_choice'] for s in c['shops'] if s.get('mapping_choice')})
            apply_mapping(c['shops'],mapping)
            value['mapping']=mapping;c['revision']+=1
            db.execute('UPDATE aov_monitor_settings SET payload=? WHERE id=1',(dump(c),));db.execute('INSERT OR REPLACE INTO aov_monitor_directory VALUES(1,?)',(dump(value),))
        return c
    def save_cards(self,value,catalog,stores):
        old=self.config()
        if not isinstance(value,dict) or set(value)-{'revision','view_version','enabled','shops','cycle_hours','collection_mode'} or value.get('revision')!=old['revision']:raise ValueError('设置已变化，请重新打开')
        if type(value.get('enabled')) is not bool or not isinstance(value.get('shops'),list):raise ValueError('监控设置无效')
        if any(not isinstance(s,dict) or not isinstance(s.get('id'),str) for s in value['shops']):raise ValueError('店铺配置无效')
        expected={s['id']:s for s in old['shops']};provided={s.get('id'):s for s in value['shops'] if isinstance(s,dict)}
        if len(provided)!=len(value['shops']) or set(provided)!=set(expected):raise ValueError('店铺目录已变化，请重新打开')
        directory=self.directory();ms={s['id']:s for s in directory.get('miaoshou',[]) if s.get('status')=='normal' and s.get('auth_status')=='valid'}
        known_cards={c['id']:c for c in (self.snapshot() or {}).get('shop_cards',[])}
        known_products={p['family_id']:p for c in known_cards.values() for p in c.get('products',[])}
        result=[];choices=[]
        for sid,s in provided.items():
            saved=deepcopy(expected[sid])
            for k in ('hidden','push'):
                if type(s.get(k,False)) is not bool:raise ValueError('店铺开关无效')
                saved[k]=s.get(k,False)
            for k in ('display_name',):
                label=s.get(k,'')
                if not isinstance(label,str) or len(label)>100 or any(ord(c)<32 for c in label):raise ValueError('显示名称须为100字以内的单行文字')
                saved[k]=label.strip()
            choice=s.get('mapping_choice') or None
            if choice and (not isinstance(choice,str) or choice not in ms):raise ValueError('请选择真实已授权的妙手店铺')
            if choice:choices.append(choice)
            saved['mapping_choice']=choice
            defaults=s.get('default_rules',saved['default_rules'])
            saved['default_rules']=ruleset(defaults)
            currency=s.get('currency','AUTO')
            if currency not in ('AUTO','BRL','MXN','THB','VND','USD','GBP','EUR','MYR','SGD','PHP','IDR','JPY','CNY'):raise ValueError('币种无效')
            saved['currency']=currency
            aliases=s.get('sku_aliases',saved['sku_aliases']);all_skus={x for f in catalog.values() for x in f['member_skus']}
            if not isinstance(aliases,dict) or set(aliases)-{'tms','miaoshou'} or any(not isinstance(m,dict) or any(not isinstance(k,str) or not k or len(k)>256 or not isinstance(v,str) or v not in all_skus for k,v in m.items()) for m in aliases.values()):raise ValueError('SKU映射须指向现有产品成员')
            saved['sku_aliases']=aliases;products=[];seen=set()
            hidden=s.get('hidden_skus',saved.get('hidden_skus',[]))
            actual={sku for p in known_cards.get(sid,{}).get('products',[]) for sku in p.get('observed_skus',p.get('skus',[]))}
            allowed=actual|set(saved.get('hidden_skus',[]))
            if not isinstance(hidden,list) or len(hidden)>5000 or any(not isinstance(x,str) or not x or len(x)>256 or any(ord(c)<32 for c in x) or x not in allowed for x in hidden):raise ValueError('隐藏 SKU 须来自该店实采订单')
            saved['hidden_skus']=sorted(set(hidden))
            if not isinstance(s.get('products',[]),list):raise ValueError('产品配置无效')
            for p in s.get('products',[]):
                if not isinstance(p,dict):raise ValueError('产品配置无效')
                fid=p.get('family_id')
                if not isinstance(fid,str):raise ValueError('产品无效')
                source=catalog.get(fid);known=known_products.get(fid)
                if fid in seen or not (source or known):raise ValueError('产品须来自目录或实采订单')
                seen.add(fid);label=p.get('display_name','')
                if not isinstance(label,str) or len(label)>100 or any(ord(c)<32 for c in label):raise ValueError('产品显示名称无效')
                override=p.get('override',True)
                if type(override) is not bool:raise ValueError('产品规则无效')
                rules=p.get('rules',{})
                products.append(dict(family_id=fid,name=source['product_name'] if source else known['name'],skus=sorted(source['member_skus'] if source else known['skus']),display_name=label.strip(),override=override,rules=ruleset(rules)))
            saved['products']=products;result.append(saved)
        if len(choices)!=len(set(choices)):raise ValueError('同一妙手店铺不能重复匹配')
        mapping=collection_mapping(directory,{s['id']:s['mapping_choice'] for s in result if s['mapping_choice']})
        apply_mapping(result,mapping)
        mode=value.get('collection_mode',old.get('collection_mode','CYCLE'))
        if mode not in ('CYCLE','ROLLING','ROLLING_INCREMENTAL'):raise ValueError('采集方式无效')
        new=dict(view_version=2,revision=old['revision']+1,enabled=value['enabled'],shops=result,collection_mode=mode,cycle_hours=cycle_hours(value if 'cycle_hours' in value else old))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.config()['revision']!=old['revision']:raise ValueError('设置已在其他页面修改')
            db.execute('UPDATE aov_monitor_settings SET payload=? WHERE id=1',(dump(new),))
            if new['collection_mode']!=old.get('collection_mode') or cycle_hours(new)!=cycle_hours(old):db.execute('DELETE FROM aov_monitor_alerts')
            # Invalidate immediately, including A -> B -> A edits between runs.
            # Hidden/display changes never invalidate a notification counter.
            prior={s['id']:s for s in old['shops']}
            for key, in db.execute('SELECT key FROM aov_monitor_alerts').fetchall():
                sid,fid,_=key.rsplit(':',2);a=prior.get(sid);b=next((s for s in result if s['id']==sid),None)
                if not a or not b:continue
                ap=next((p for p in a['products'] if p['family_id']==fid),{})
                bp=next((p for p in b['products'] if p['family_id']==fid),{})
                ar=ap['rules'] if ap.get('override',True) and 'rules' in ap else a['default_rules']
                br=bp['rules'] if bp.get('override',True) and 'rules' in bp else b['default_rules']
                if ar!=br or a['sku_aliases']!=b['sku_aliases'] or a.get('miaoshou_shop_id')!=b.get('miaoshou_shop_id') or a.get('source_mode')!=b.get('source_mode') or a['push']!=b['push']:
                    db.execute('DELETE FROM aov_monitor_alerts WHERE key=?',(key,))
            if directory:
                directory['mapping']=mapping;db.execute('UPDATE aov_monitor_directory SET payload=? WHERE id=1',(dump(directory),))
        return new
    def add_jobs(self,rid,jobs):
        with self.db() as db:
            for job in jobs:db.execute('INSERT OR IGNORE INTO aov_monitor_jobs VALUES(?,?,?, ?,NULL)',(rid,job['id'],'QUEUED',dump(job)))
    def jobs(self,rid):
        with self.db() as db:return [dict(json.loads(p),status=s,result=json.loads(r) if r else None) for p,s,r in db.execute('SELECT payload,status,result FROM aov_monitor_jobs WHERE run_id=? ORDER BY id',(rid,))]
    def job_done(self,rid,jid,value,status='COMPLETE'):
        with self.db() as db:
            row=db.execute('SELECT payload FROM aov_monitor_jobs WHERE run_id=? AND id=?',(rid,jid)).fetchone()
            job=json.loads(row[0]);job['finished_at']=now()
            if job.get('started_at'):job['elapsed_seconds']=max(0,round((instant(job['finished_at'])-instant(job['started_at'])).total_seconds(),2))
            db.execute('UPDATE aov_monitor_jobs SET status=?,result=?,payload=? WHERE run_id=? AND id=?',(status,dump(value),dump(job),rid,jid))
    def job_started(self,rid,jid):
        with self.db() as db:
            row=db.execute('SELECT payload FROM aov_monitor_jobs WHERE run_id=? AND id=?',(rid,jid)).fetchone()
            job=json.loads(row[0]);job.update(started_at=now());job.pop('finished_at',None)
            db.execute('UPDATE aov_monitor_jobs SET payload=? WHERE run_id=? AND id=?',(dump(job),rid,jid))
    def pause(self,rid,context,code):
        context.update(phase=code,waiting_since=context.get('waiting_since') or now(),wait_error=code)
        with self.db() as db:
            db.execute('UPDATE aov_monitor_runs SET payload=? WHERE id=?',(dump(context),rid))
            if context.get('slot'):
                for key,raw in db.execute('SELECT key,payload FROM aov_monitor_alerts').fetchall():
                    state=json.loads(raw);state.update(candidate=None,streak=0)
                    db.execute('UPDATE aov_monitor_alerts SET payload=? WHERE key=?',(dump(state),key))
    def skip_unstarted(self,rid,context,slot):
        """Skip a stale automatic slot only before any source receipt exists."""
        if not context.get('slot') or instant(context['slot'])>=slot:return False
        with self.db() as db:
            if db.execute('SELECT 1 FROM aov_monitor_jobs WHERE run_id=? LIMIT 1',(rid,)).fetchone():return False
            db.execute("UPDATE aov_monitor_runs SET status='SUPERSEDED',payload=? WHERE id=? AND status='RUNNING'",(dump(dict(context,status='SUPERSEDED',finished_at=now(),reason='LATEST_SLOT_ONLY')),rid))
        return True
    def finish(self,rid,payload,states=None,events=(),orders=None):
        if (self.context(rid) or {}).get('card_run'):
            payload=dict(payload,finished_at=now())
            jobs=self.jobs(rid)
            payload['timing']={'collection_seconds':round(sum(j.get('elapsed_seconds',0) for j in jobs if not j.get('reused')),2),'reused_batches':sum(j.get('reused',False) for j in jobs),'by_source':{k:round(sum(j.get('elapsed_seconds',0) for j in jobs if j['kind']==k and not j.get('reused')),2) for k in ('tms','miaoshou','refunds')}}
        if 'timing' in payload:
            payload['timing']['directory_seconds']=(payload.get('resume_context') or {}).get('directory_seconds',0)
            payload['timing']['calculation_seconds']=payload.get('calculation_seconds',0)
            payload['timing']['active_seconds']=round(payload['timing']['collection_seconds']+payload['timing']['directory_seconds']+payload['timing']['calculation_seconds'],2)
        super().finish(rid,payload,states,events,orders)
        if payload.get('collection_mode')=='CYCLE':
            with self.db() as db:
                for card in payload.get('shop_cards',[]):
                    if card['status']=='COMPLETE' and card['id'] in (orders or {}):
                        db.execute('INSERT OR REPLACE INTO aov_monitor_cycle_coverage VALUES(?,?,?,?,?,?,?)',(rid,card['id'],payload['start'],payload['cutoff'],card['order_identity'],int(card.get('current_alert_eligible',False)),payload['finished_at']))
    def previous_cycle(self,sid,start,end,identity):
        """Read complete local coverage without querying either ERP again."""
        a,b=instant(start),instant(end);cursor=a;rows=[];runs=[];eligible=True
        with self.db() as db:
            receipts=db.execute('SELECT run_id,start,end,alert_eligible,saved_at FROM aov_monitor_cycle_coverage WHERE shop_id=? AND identity_hash=? AND end>? AND start<? ORDER BY saved_at DESC',(sid,identity,start,end)).fetchall()
            while cursor<b:
                receipt=next((r for r in receipts if instant(r[1])<=cursor<instant(r[2])),None)
                if not receipt:return {'complete':False,'orders':[],'alert_eligible':False,'runs':[]}
                rid,_,stop,valid,_=receipt;stop=min(b,instant(stop));eligible=eligible and bool(valid);runs.append(rid)
                for raw, in db.execute('SELECT payload FROM aov_monitor_orders WHERE run_id=? AND shop_id=?',(rid,sid)):
                    order=json.loads(raw)
                    if cursor<=instant(order['paid_at'])<stop:rows.append(order)
                cursor=stop
        unique={}
        for order in rows:
            if order['order_key'] in unique and unique[order['order_key']]!=order:return {'complete':False,'orders':[],'alert_eligible':False,'runs':runs}
            unique[order['order_key']]=order
        return {'complete':True,'orders':list(unique.values()),'alert_eligible':eligible,'runs':sorted(set(runs))}
    def context(self,rid,value=None):
        with self.db() as db:
            if value is not None:db.execute('UPDATE aov_monitor_runs SET payload=? WHERE id=?',(dump(value),rid))
            r=db.execute('SELECT payload FROM aov_monitor_runs WHERE id=?',(rid,)).fetchone()
            return json.loads(r[0]) if r else None
    def narrow_run(self,rid,context):
        """Narrow an unfinished queue without changing completed snapshots.

        Whole completed batches inside the six-day scope are reused verbatim.
        A boundary-crossing receipt stays archived; its smaller range is read
        again rather than pretending filtered rows prove complete coverage.
        """
        c=deepcopy(context)
        if c.get('collection_mode')=='CYCLE':return c
        if not c.get('card_run') or not c.get('cutoff') or not c.get('start'):return c
        end=instant(c['cutoff']);start=end-timedelta(hours=LOOKBACK_HOURS)
        if instant(c['start'])<start:
            c.setdefault('original_start',c['start']);c['start']=start.isoformat();c['current']=None
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                for jid,status,payload,result in db.execute('SELECT id,status,payload,result FROM aov_monitor_jobs WHERE run_id=?',(rid,)).fetchall():
                    j=json.loads(payload)
                    if status=='ARCHIVED' or j['kind']=='refunds':continue
                    a,b=instant(j['start']),instant(j['end'])
                    if a>=start and b<=end:continue
                    db.execute("UPDATE aov_monitor_jobs SET status='ARCHIVED' WHERE run_id=? AND id=?",(rid,jid))
                    if status=='SPLIT' or b<=start or a>=end:continue
                    clipped=dict(j,id=jid+'-six-days',start=max(a,start).isoformat(),end=min(b,end).isoformat())
                    db.execute('INSERT OR IGNORE INTO aov_monitor_jobs VALUES(?,?,?, ?,NULL)',(rid,clipped['id'],'QUEUED',dump(clipped)))
        c.update(active_windows=list(WINDOWS),lookback_hours=LOOKBACK_HOURS)
        self.context(rid,c)
        return c
    def pending(self):
        with self.db() as db:
            row=db.execute("SELECT id,payload FROM aov_monitor_runs WHERE status='RUNNING' ORDER BY rowid DESC LIMIT 1").fetchone()
            return (row[0],json.loads(row[1])) if row else None
    def progress(self):
        pending=self.pending()
        if not pending:return (self.snapshot() or {}).get('progress',{})
        rid,c=pending;j=self.jobs(rid)
        elapsed=sum(x.get('elapsed_seconds',0) for x in j)
        active=next((x for x in j if x['id']==c.get('current') and x.get('started_at') and not x.get('finished_at')),None)
        if active and not c.get('waiting_since'):elapsed+=max(0,(instant(now())-instant(active['started_at'])).total_seconds())
        return dict(run_id=rid,phase=c.get('phase','DIRECTORY'),cutoff=c.get('cutoff'),start=c.get('start'),waiting_since=c.get('waiting_since'),wait_error=c.get('wait_error'),collection_seconds=round(elapsed),active_windows=c.get('active_windows'),lookback_hours=c.get('lookback_hours'),total=sum(x['status'] not in ('SPLIT','ARCHIVED') for x in j),completed=sum(x['status']=='COMPLETE' for x in j),failed=sum(x['status']=='FAILED' for x in j),current=c.get('current'),matched=c.get('matched',0),shops=c.get('shop_count',0))
    def recover(self):
        # A card run stores every completed slice, so resume it instead of
        # inventing an interrupted snapshot or rereading already saved batches.
        with self.db() as db:
            for rid,p in db.execute("SELECT id,payload FROM aov_monitor_runs WHERE status='RUNNING'").fetchall():
                if not json.loads(p).get('card_run'):db.execute("UPDATE aov_monitor_runs SET status='INTERRUPTED' WHERE id=?",(rid,))
            db.execute("UPDATE aov_monitor_events SET status='uncertain' WHERE status IN ('sending','pending')")
