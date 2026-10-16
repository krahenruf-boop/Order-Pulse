"""Bounded normal-form login, only for the explicitly bound TMS account."""
import hashlib
import json
from pathlib import Path
import time
from .ego_runtime import execute,EgoRuntimeError
from .login_keychain import LoginKeychain,KeychainError

KEY='tms_auto_login'
BLOCKED={'VERIFICATION_REQUIRED','CREDENTIALS_REJECTED','ACCOUNT_SCOPE_CHANGED','SOURCE_CHANGED'}
MESSAGES={
 'READY':'已启用自动登录；仅在检测任务需要时使用本机钥匙串。',
 'SIGNED_IN':'马帮已登录，账号身份已核对。',
 'ATTEMPTING':'正在尝试马帮自动登录。',
 'VERIFICATION_REQUIRED':'马帮要求验证码或安全验证；完成后点击继续检测，账号密码由本机自动填写。',
 'CREDENTIALS_REJECTED':'马帮本次登录未通过，请在登录设置中核对账号密码；已停止自动重试。',
 'ACCOUNT_SCOPE_CHANGED':'登录账号与原授权账号不符，已停止采集。',
 'SOURCE_CHANGED':'马帮登录页面发生变化，已停止自动填写。',
 'KEYCHAIN_LOCKED':'本机钥匙串暂不可读取，请解锁或授权后继续检测。',
 'KEYCHAIN_UNAVAILABLE':'本机钥匙串暂不可用，账号密码未写入普通配置。',
 'CREDENTIALS_REQUIRED':'请在监控设置中保存一次马帮账号密码。',
 'UNAVAILABLE':'马帮暂未完成登录，稍后按检测周期重试。',
}

class TmsLogin:
    def __init__(self,host,keychain=None,runner=execute,clock=time.time):
        self.host=host;self.repo=host.repo;self.keychain=keychain or LoginKeychain();self.runner=runner;self.clock=clock

    def scope(self):
        b=self.host.browser_space.get() or {};account=(self.repo.get('binding') or {}).get('account')
        if not account or not b:return None
        # Numeric Space IDs/page labels are intentionally excluded: they change on restart.
        identity=[account,{k:b.get(k) for k in ('expected_name','profile_id','profile_name','purpose')}]
        return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()

    def state(self):
        saved=self.repo.get(KEY) or {};configured=bool(self.scope() and saved.get('scope')==self.scope() and saved.get('has_credentials'))
        status=saved.get('status','CREDENTIALS_REQUIRED') if configured else 'CREDENTIALS_REQUIRED'
        return dict(configured=configured,enabled=configured and saved.get('enabled') is True,status=status,
                    message=MESSAGES.get(status,MESSAGES['UNAVAILABLE']),last_attempt_at=saved.get('last_attempt_at'))

    def configure(self,value):
        if not isinstance(value,dict) or set(value)-{'username','password','enabled'} or type(value.get('enabled')) is not bool:raise ValueError('自动登录设置无效')
        scope=self.scope()
        if not scope:raise ValueError('请先确认原马帮账号和专用浏览器空间')
        saved=self.repo.get(KEY) or {}
        username=value.get('username','');password=value.get('password','')
        if username or password:
            if any(not isinstance(v,str) or not v or len(v)>1024 or any(ord(x)<32 for x in v) for v in (username,password)):raise ValueError('请同时填写有效账号与密码')
            try:self.keychain.save(scope,{'username':username,'password':password})
            except KeychainError:raise ValueError('无法写入本机钥匙串，请完成系统授权后重试') from None
        elif value['enabled'] and (saved.get('scope')!=scope or not saved.get('has_credentials')):raise ValueError('首次启用需要填写账号与密码')
        has_credentials=bool(username and password or saved.get('scope')==scope and saved.get('has_credentials'))
        saved.update(scope=scope,has_credentials=has_credentials,enabled=value['enabled'],status='READY',next_attempt_at=0)
        self.repo.set(KEY,saved)
        return self.state()

    def _record(self,scope,**changes):
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT value FROM settings WHERE key=?',(KEY,)).fetchone()
            saved=json.loads(row[0]) if row else {}
            if saved.get('scope')!=scope or not saved.get('enabled'):return False
            saved.update(changes)
            db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(saved),KEY))
        return True

    def attempt(self,binding):
        scope=self.scope();saved=self.repo.get(KEY) or {};now=self.clock()
        if not scope or saved.get('scope')!=scope or not saved.get('enabled'):return False
        verification=saved.get('status')=='VERIFICATION_REQUIRED'
        if not verification and (saved.get('status') in BLOCKED or saved.get('next_attempt_at',0)>now):return False
        if not self.host.browser_space.lock.acquire(blocking=False):return False
        try:
            if binding!=self.host.browser_space.get():return False
            if verification:
                # A manual verification can unblock the saved job. Observe only;
                # never click a challenge, resubmit it or ask for its solution.
                try:
                    probe=self.runner(Path(__file__).with_name('ego_tms_login.js'),dict(binding=binding,account=self.repo.get('binding')['account'],probe_only=True),
                        'ORDER_PULSE_TMS_LOGIN:',timeout=15,max_output=16000)
                except EgoRuntimeError:return False
                if probe.get('status') not in ('READY','SIGNED_IN'):return False
            try:credentials=self.keychain.read(scope)
            except KeychainError as exc:
                self._record(scope,status='KEYCHAIN_LOCKED' if str(exc)=='KEYCHAIN_LOCKED' else 'KEYCHAIN_UNAVAILABLE',next_attempt_at=now+600);return False
            if not isinstance(credentials,dict) or not all(isinstance(credentials.get(k),str) and credentials[k] for k in ('username','password')):
                self._record(scope,status='CREDENTIALS_REQUIRED',next_attempt_at=now+7200);return False
            # Persist before dispatch so a backend restart cannot duplicate the submission.
            if not self._record(scope,status='ATTEMPTING',last_attempt_at=now,next_attempt_at=now+600):return False
            try:
                result=self.runner(Path(__file__).with_name('ego_tms_login.js'),dict(binding=binding,account=self.repo.get('binding')['account'],credentials=credentials),
                    'ORDER_PULSE_TMS_LOGIN:',timeout=45,max_output=16000)
                status=result.get('status')
                if status not in MESSAGES:status='UNAVAILABLE'
            except EgoRuntimeError:status='UNAVAILABLE'
            finally:credentials.clear()
            self._record(scope,status=status)
            return status=='SIGNED_IN'
        finally:self.host.browser_space.lock.release()

    def observed_signed_in(self):
        # Ordinary collectors still check the exact account on every batch.
        saved=self.repo.get(KEY) or {}
        if saved.get('status')!='SIGNED_IN':self._record(self.scope(),status='SIGNED_IN',next_attempt_at=0)
