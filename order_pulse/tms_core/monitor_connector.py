"""Fixed ego program for autonomous UI preparation and scoped collection."""
import json
from pathlib import Path
import os
import subprocess
import threading
from .monitor_domain import binding_input
from .source_cooldown import SourceCooldown

ERROR_CODES={'WAITING_USER','WAITING_CONNECTION','AUTH_REQUIRED','PERMISSION_DENIED','RATE_LIMITED',
             'SOURCE_CHANGED','QUERY_FAILED','ACCOUNT_SCOPE_CHANGED','CATALOG_CHANGED','SCOPE_TOO_LARGE',
             'REQUEST_SCOPE_MISMATCH','INCOMPLETE_PAGINATION','SNAPSHOT_CHANGED','BROWSER_BUSY','UI_CLEANUP_FAILED'}


class EgoMonitorConnector:
    def __init__(self,root):
        self.cooldown=SourceCooldown(root);self.lock=threading.Lock()

    def execute(self,spec):
        if not self.lock.acquire(False):return {'error':'BROWSER_BUSY'}
        try:
            until=self.cooldown.until()
            if until:return {'error':'RATE_LIMITED','retry_at':until.isoformat()}
            script=Path(__file__).with_name('ego_monitor.js').read_text()
            program='const INPUT='+json.dumps(spec,ensure_ascii=True)+';\n'+script
            env={'HOME':str(Path.home()),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','TMPDIR':'/private/tmp'}
            try:
                p=subprocess.run([str(Path.home()/'.local/bin/ego-browser'),'nodejs'],input=program,text=True,capture_output=True,
                                 timeout=240,env=env,shell=False)
            except (OSError,subprocess.TimeoutExpired):return {'error':'WAITING_CONNECTION'}
            output=p.stdout+'\n'+p.stderr;marker='TMS_MONITOR_RESULT:'
            lines=[x.strip()[len(marker):] for x in output.splitlines() if x.strip().startswith(marker)]
            if p.returncode or len(output)>4_000_000 or len(lines)!=1:return {'error':'WAITING_CONNECTION'}
            try:result=json.loads(lines[0])
            except ValueError:return {'error':'SOURCE_CHANGED'}
            if not isinstance(result,dict):return {'error':'SOURCE_CHANGED'}
            if result.get('error'):
                code=result['error'] if result['error'] in ERROR_CODES else 'QUERY_FAILED'
                if code=='RATE_LIMITED':return {'error':code,'retry_at':self.cooldown.record(result.get('retry_after')).isoformat()}
                return {'error':code}
            return result
        finally:self.lock.release()

    def inspect(self,target):
        target=binding_input(target);r=self.execute(dict(target,mode='inspect'))
        if r.get('error'):return r
        account=r.get('account');stores=r.get('stores')
        if not isinstance(account,dict) or set(account)!={'employee_id','customer_id','supplier_id'} or any(not isinstance(v,str) or not v.isascii() or not v.isdigit() for v in account.values()):raise ValueError('账号身份不可验证')
        if not isinstance(stores,list) or not stores or len(stores)>1000:raise ValueError('真实店铺目录不可验证')
        clean=[];seen=set()
        for s in stores:
            if not isinstance(s,dict) or set(s)!={'shop_id','name','platform_id','company_id'}:raise ValueError('店铺字段不可验证')
            if not isinstance(s['shop_id'],str) or not s['shop_id'].isdigit() or s['shop_id'] in seen or s['platform_id']!=104 or s['company_id']!=account['customer_id'] or not isinstance(s['name'],str) or not s['name'] or len(s['name'])>256:raise ValueError('店铺范围不可验证')
            seen.add(s['shop_id']);clean.append(s)
        return dict(target,account=account,stores=clean,origin='https://tms.mabangerp.com',scope='RESTRICTED_DIAGNOSTIC')

    def collect(self,binding,scope):
        return self.execute({'mode':'collect','space_id':binding['space_id'],'page_label':binding['page_label'],
                             'account':binding['account'],'scope':scope})
