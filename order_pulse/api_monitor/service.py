"""Durable daily schedule, quota/health episodes and transactional outbox."""
import asyncio,json,re,uuid
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from .domain import PROVIDERS,TZ,amount,reading,ratio,next_at
from ..browser import blocking
from ..integrations.ego_runtime import execute
from ..feishu import FeishuClient

def utc():return datetime.now(timezone.utc)
class Monitor:
    def __init__(self,host,clock=utc,collector=None,sender=None):
        self.host=host;self.repo=host.repo;self.clock=clock;self.collector=collector or self.collect;self.sender=sender
        self.workers={};self.timer=None;self.send_lock=asyncio.Lock()
        with self.repo.db() as d:
            d.executescript('''CREATE TABLE IF NOT EXISTS api_monitor_providers(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_monitor_history(id TEXT PRIMARY KEY,provider TEXT NOT NULL,at TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_monitor_outbox(id TEXT PRIMARY KEY,provider TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,at TEXT NOT NULL,payload TEXT NOT NULL);''')
            for pid in PROVIDERS:
                d.execute('INSERT OR IGNORE INTO api_monitor_providers VALUES(?,?)',(pid,json.dumps(dict(enabled=True,push=True,threshold='15',time='09:00',baseline=None,baseline_at=None,last=None,low=False,low_episode=None,fault=None,last_day=None))))
    def get(self,pid):
        if pid not in PROVIDERS:raise ValueError('供应商不存在')
        with self.repo.db() as d:return json.loads(d.execute('SELECT payload FROM api_monitor_providers WHERE id=?',(pid,)).fetchone()[0])
    def put(self,d,pid,c):d.execute('UPDATE api_monitor_providers SET payload=? WHERE id=?',(json.dumps(c,ensure_ascii=False),pid))
    def state(self):
        now=self.clock();items=[]
        for pid in PROVIDERS:
            c=self.get(pid);last=c['last'];items.append(dict(c,id=pid,name=PROVIDERS[pid],busy=pid in self.workers,next_at=next_at(now,c['time'],c['last_day']) if c['enabled'] else None,ratio=ratio(pid,last,c['baseline']) if last else None))
        return {'providers':items,'timezone':'Asia/Shanghai'}
    def history(self,pid):
        self.get(pid)
        with self.repo.db() as d:
            return dict(readings=[dict(id=r[0],at=r[1],**json.loads(r[2])) for r in d.execute('SELECT id,at,payload FROM api_monitor_history WHERE provider=? ORDER BY at DESC LIMIT 100',(pid,))],notifications=[dict(id=r[0],kind=r[1],status=r[2],at=r[3]) for r in d.execute('SELECT id,kind,status,at FROM api_monitor_outbox WHERE provider=? ORDER BY at DESC LIMIT 100',(pid,))])
    async def settings(self,pid,value):
        c=self.get(pid)
        if pid in self.workers:raise ValueError('正在读取，请结束后保存设置')
        if not isinstance(value,dict) or set(value)-{'enabled','push','threshold','time','baseline','reset_baseline','rebind'}:raise ValueError('设置字段无效')
        for k in ('enabled','push','reset_baseline','rebind'):
            if k in value and type(value[k]) is not bool:raise ValueError('开关格式无效')
        if 'threshold' in value and not 0<=amount(value['threshold'])<=100:raise ValueError('阈值应为 0–100%')
        if 'time' in value and (not isinstance(value['time'],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value['time'])):raise ValueError('时间应为 HH:mm')
        was=c['enabled'];c.update({k:v for k,v in value.items() if k in ('enabled','push','threshold','time')})
        if 'baseline' in value or value.get('reset_baseline'):
            if pid!='deepseek':raise ValueError('此平台使用当期总额度')
            base=value.get('baseline') if not value.get('reset_baseline') else (c['last'] or {}).get('remaining')
            if base is None or amount(base)<=0:raise ValueError('基准必须为正数，请先成功读取余额')
            c.update(baseline=base,baseline_at=self.clock().isoformat())
        if value.get('rebind'):
            # An explicit user action starts a new identity/episode; history remains.
            c.update(last=None,baseline=None,baseline_at=None,low=False,low_episode=None,fault=None,last_day=None)
            with self.repo.db() as d:d.execute("UPDATE api_monitor_outbox SET status='cancelled' WHERE provider=? AND status IN ('pending','failed')",(pid,))
        with self.repo.db() as d:self.put(d,pid,c)
        if c['enabled'] and (not was or value.get('rebind')):await self.start(pid)
        return self.state()
    async def start(self,pid,manual=True):
        self.get(pid)
        if manual:self.repo.set('finance_user_action',None)
        if pid not in self.workers:
            task=asyncio.create_task(self.check(pid));self.workers[pid]=task
            task.add_done_callback(lambda t:self.workers.pop(pid,None))
        return {'message':'正在读取额度','provider':pid}
    def collect(self,pid):
        binding=self.host.balance_browser_space.get()
        if not binding:raise ValueError('请先绑定财务浏览器空间')
        lock=self.host.balance_browser_space.lock
        if not lock.acquire(timeout=120):raise ValueError('财务浏览器正在执行任务，请稍后重试')
        try:
            if self.repo.get('finance_user_action'):raise ValueError('财务空间正在等待本人完成登录或验证码；完成后请手动刷新')
            value=execute(Path(__file__).with_name('ego_quota.js'),dict(binding=binding,provider=pid),'ORDER_PULSE_QUOTA:',timeout=110,max_output=50000)
            if value.get('needs_user'):self.repo.set('finance_user_action',dict(provider=pid,at=self.clock().isoformat()))
            if not value.get('ok'):raise ValueError(value.get('error','额度读取失败'))
            return value['reading']
        finally:
            self.host.browser_control.show_workbench('api-monitor',locked=True)
            lock.release()
    def queue(self,d,pid,kind,c,now,episode,stage=0):
        event=f'api-{pid}-{episode}-{kind}-{stage}'
        last=c.get('last') or {};r=ratio(pid,last,c['baseline']) if last else None
        title={'low':'低额度预警','recovery':'额度恢复正常','fault':'额度监控故障','reminder':'额度监控持续故障','fault_recovery':'额度采集恢复','test':'API 额度监控测试'}[kind]
        lines=[f'{PROVIDERS[pid]} · {title}',f'账户／工作区：{last.get("identity","尚未核验")}',f'剩余额度：{last.get("remaining","—")} {last.get("unit","")}',f'剩余比例：{Decimal(r):.2f}%' if r is not None else '剩余比例：—',f'预警阈值：{c["threshold"]}%',f'读取时间：{datetime.fromisoformat(last.get("at") or now.isoformat()).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")}（北京时间）']
        if kind in ('fault','reminder'):lines.append('原因：'+c['fault']['error'])
        if kind=='test':lines.append('此为测试消息，未消耗 API 或语音额度。')
        payload=dict(text='\n'.join(lines),attempt_at=None)
        d.execute('INSERT OR IGNORE INTO api_monitor_outbox VALUES(?,?,?,?,?,?)',(event,pid,kind,'pending',now.isoformat(),json.dumps(payload,ensure_ascii=False)))
        return event
    def accept(self,pid,raw):
        now=self.clock();c=self.get(pid);v=reading(pid,raw,c['last']);v['at']=now.isoformat()
        if pid=='deepseek' and c['baseline'] is None and amount(v['remaining'])>0:c.update(baseline=v['remaining'],baseline_at=now.isoformat())
        old=c['last'];c['last']=v;c['last_day']=now.astimezone(TZ).date().isoformat()
        r=ratio(pid,v,c['baseline']);is_low=r is not None and Decimal(r)<=amount(c['threshold'])
        with self.repo.db() as d:
            d.execute('INSERT INTO api_monitor_history VALUES(?,?,?,?)',(uuid.uuid4().hex,pid,now.isoformat(),json.dumps(dict(ok=True,reading=v,ratio=r,baseline=c['baseline']),ensure_ascii=False)))
            if c['fault']:
                ep=c['fault']['episode']
                sent=d.execute("SELECT 1 FROM api_monitor_outbox WHERE provider=? AND id LIKE ? AND kind IN ('fault','reminder') AND status IN ('success','uncertain')",(pid,f'api-{pid}-{ep}-%')).fetchone()
                if sent and c['push']:self.queue(d,pid,'fault_recovery',c,now,ep)
                c['fault']=None
            if r is not None:
                if is_low:
                    if not c['low']:c['low_episode']=uuid.uuid4().hex
                    c['low']=True
                    if c['push']:self.queue(d,pid,'low',c,now,c['low_episode'])
                elif c['low'] and old and (amount(v['remaining'])>amount(old['remaining']) or (pid=='elevenlabs' and v.get('reset_label')!=old.get('reset_label'))):
                    sent=d.execute("SELECT 1 FROM api_monitor_outbox WHERE id=? AND status IN ('success','uncertain')",(f'api-{pid}-{c["low_episode"]}-low-0',)).fetchone()
                    if sent and c['push']:self.queue(d,pid,'recovery',c,now,c['low_episode'])
                    c['low']=False
            self.put(d,pid,c)
    def fail(self,pid,error):
        c=self.get(pid);now=self.clock()
        if not c['fault']:c['fault']=dict(episode=uuid.uuid4().hex,since=now.isoformat(),error=error)
        else:c['fault']['error']=error
        fault=c['fault'];stage=int((now-datetime.fromisoformat(fault['since'])).total_seconds()//21600)
        with self.repo.db() as d:
            if c['enabled'] and c['push']:self.queue(d,pid,'fault' if stage==0 else 'reminder',c,now,fault['episode'],stage)
            self.put(d,pid,c)
    async def check(self,pid):
        now=self.clock()
        try:self.accept(pid,await blocking(self.collector,pid))
        except asyncio.CancelledError:raise
        except Exception as e:
            error=str(e) if isinstance(e,ValueError) else '供应商页面暂不可读取，已有额度保留'
            self.fail(pid,error)
            with self.repo.db() as d:
                d.execute('INSERT INTO api_monitor_history VALUES(?,?,?,?)',(uuid.uuid4().hex,pid,now.isoformat(),json.dumps(dict(ok=False,error=error),ensure_ascii=False)))
                c=self.get(pid);c['last_day']=now.astimezone(TZ).date().isoformat();self.put(d,pid,c)
        await self.dispatch()
    async def dispatch(self):
        async with self.send_lock:
            with self.repo.db() as d:rows=d.execute("SELECT id,provider,kind,status,payload FROM api_monitor_outbox WHERE status IN ('pending','failed') ORDER BY at").fetchall()
            for eid,pid,kind,status,payload in rows:
                c=self.get(pid);p=json.loads(payload)
                if status=='failed' and p['attempt_at'] and (self.clock()-datetime.fromisoformat(p['attempt_at'])).total_seconds()<900:continue
                valid=c['enabled'] and c['push'] and (kind not in ('fault','reminder') or c['fault'] and c['fault']['episode'] in eid) and (kind!='low' or c['low'] and c['low_episode'] in eid)
                if kind=='low':
                    current=ratio(pid,c['last'],c['baseline']) if c['last'] else None
                    valid=bool(valid and current is not None and Decimal(current)<=amount(c['threshold']))
                if kind=='recovery':valid=bool(valid and not c['low'] and c['low_episode'] in eid)
                if kind=='fault_recovery':valid=bool(valid and not c['fault'])
                if kind=='test':valid=True
                if not valid:
                    with self.repo.db() as d:d.execute("UPDATE api_monitor_outbox SET status='cancelled' WHERE id=?",(eid,))
                    continue
                p['attempt_at']=self.clock().isoformat()
                with self.repo.db() as d:d.execute("UPDATE api_monitor_outbox SET status='sending',payload=? WHERE id=?",(json.dumps(p),eid))
                result={}
                try:
                    if self.sender:result=await self.sender(p['text'],eid)
                    else:
                        creds=self.host.balance.feishu.get();client=FeishuClient(creds.get('webhook',''),creds.get('secret',''),self.host.balance.store.settings().get('keyword',''))
                        result=await client.send(p['text'],eid)
                    outcome=result['status']
                except Exception:outcome='failed'
                p['reason']=result.get('reason') or ('发送未完成' if outcome=='failed' else None)
                with self.repo.db() as d:d.execute('UPDATE api_monitor_outbox SET status=?,payload=? WHERE id=?',(outcome,json.dumps(p,ensure_ascii=False),eid))
    async def test(self):
        with self.repo.db() as d:eid=self.queue(d,'deepseek','test',self.get('deepseek'),self.clock(),uuid.uuid4().hex)
        await self.dispatch()
        with self.repo.db() as d:status=d.execute('SELECT status FROM api_monitor_outbox WHERE id=?',(eid,)).fetchone()[0]
        return dict(message='测试消息已处理',status=status)
    async def tick(self):
        now=self.clock();local=now.astimezone(TZ)
        for pid in PROVIDERS:
            c=self.get(pid)
            if not c['enabled']:continue
            last=c['last']
            if c['fault']:self.fail(pid,c['fault']['error'])
            elif last and (now-datetime.fromisoformat(last['at'])).total_seconds()>=93600:self.fail(pid,'超过 26 小时未成功读取，已有额度保留')
            h,m=map(int,c['time'].split(':'))
            if c['last_day']!=local.date().isoformat() and (c['last_day'] is None or (local.hour,local.minute)>=(h,m)):await self.start(pid,manual=False)
        await self.dispatch()
    async def loop(self):
        while True:
            try:await self.tick()
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(60)
    async def startup(self):
        with self.repo.db() as d:d.execute("UPDATE api_monitor_outbox SET status='uncertain' WHERE status='sending'")
        self.timer=asyncio.create_task(self.loop())
    async def shutdown(self):
        if self.timer:self.timer.cancel();await asyncio.gather(self.timer,return_exceptions=True)
        if self.workers:await asyncio.gather(*list(self.workers.values()),return_exceptions=True)
