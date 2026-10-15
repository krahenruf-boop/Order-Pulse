"""Existing TikTok OPS material extension v2 protocol, hosted by Order Pulse.
No browser automation fallback; incompatible plugin builds are blocked at the bridge.
"""
import asyncio
import json
import re
import secrets
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

OPERATIONS=['bind_product','apply_period','verify_context','read_visible_page','resolve_observed_link']
PREFIX='/materials-bridge/v1'
MINIMUM_SUPPORTED_PLUGIN_VERSION='1.1.4'
REQUIRED_BUILD_ID='20261010.1'
REQUIRED_KALODATA_ADAPTER='2026-09-v2'

def _version_tuple(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d+\.\d+\.\d+',value):return None
    return tuple(int(part) for part in value.split('.'))

class KalodataPluginClient:
    def __init__(self,root,credentials,port=8876,legacy_auth=None):
        self.root=Path(root);self.credentials=credentials;self.port=port
        self.lock=threading.RLock();self.job=None;self.future=None;self.last_seen=0;self.bound=False
        self.logged_in=None;self.plugin_version=None;self.build_id=None;self.kalodata_adapter=None;self.last_success_at=None;self.bridge_error=None
        self.server=None;self.server_task=None
        self.code=secrets.token_urlsafe(18);self.code_expires=time.time()+1800
        saved=credentials.namespace('kalodata').get('plugin_bridge',{})
        if not saved and legacy_auth and Path(legacy_auth).is_file():
            legacy=json.loads(Path(legacy_auth).read_text())
            if (isinstance(legacy.get('token'),str) and legacy['token'] and
                re.fullmatch('[a-p]{32}',legacy.get('extension_id','')) and
                re.fullmatch('[A-Za-z0-9_-]{1,100}',legacy.get('session_ref',''))):
                saved={k:legacy[k] for k in ('token','extension_id','session_ref')}
                namespace=credentials.namespace('kalodata');namespace['plugin_bridge']=saved
                credentials.save_namespace('kalodata',namespace)
        self.token=saved.get('token');self.extension_id=saved.get('extension_id')
        self.session_ref=saved.get('session_ref') or 'kalodata-plugin-'+secrets.token_hex(8)
        self.asgi=self._app()

    def save(self):
        namespace=self.credentials.namespace('kalodata')
        namespace['plugin_bridge']=dict(token=self.token,extension_id=self.extension_id,session_ref=self.session_ref)
        self.credentials.save_namespace('kalodata',namespace)

    def info(self):
        connected=bool(self.token and self.bound and time.monotonic()-self.last_seen<45 and not self.bridge_error)
        version=_version_tuple(self.plugin_version)
        code=('PLUGIN_OFFLINE' if not connected else
              'PLUGIN_RELOAD_REQUIRED' if version is None or version<_version_tuple(MINIMUM_SUPPORTED_PLUGIN_VERSION) or not self.build_id or not self.kalodata_adapter else
              'PLUGIN_VERSION_MISMATCH' if self.kalodata_adapter!=REQUIRED_KALODATA_ADAPTER or (version==_version_tuple(MINIMUM_SUPPORTED_PLUGIN_VERSION) and self.build_id!=REQUIRED_BUILD_ID) else
              'KALODATA_LOGIN_REQUIRED' if self.logged_in is False else None)
        return dict(plugin_connected=connected,plugin_version=self.plugin_version,build_id=self.build_id,
                    kalodata_adapter=self.kalodata_adapter,minimum_supported_plugin_version=MINIMUM_SUPPORTED_PLUGIN_VERSION,
                    required_build_id=REQUIRED_BUILD_ID,required_kalodata_adapter=REQUIRED_KALODATA_ADAPTER,protocol_version=2,
                    browser_connected=connected,kalodata_logged_in=self.logged_in if connected else None,
                    active_job=(self.job or {}).get('payload',{}).get('run_id'),last_success_at=self.last_success_at,
                    connected=connected,state=code or 'CONNECTED',transport='plugin',paired=bool(self.token))

    def status(self):return self.info()

    async def health(self,check_login=False):
        # Heartbeats do not open or refresh any product. An explicit user check
        # may inspect the plugin's already selected page via its existing op.
        status=self.info()
        if check_login and status['state']=='CONNECTED' and self.job is None:
            try:
                reply=await self.observe('inspect_ui',dict(operation_id='health',run_id=None,generation=0))
                if reply.get('success') is True:self.logged_in=True
                elif reply.get('login_required') or reply.get('error_code')=='LOGIN_REQUIRED':self.logged_in=False
            except (ConnectionError,asyncio.TimeoutError):pass
        return self.info()

    async def probe_session(self):
        info=self.info();ready=info['plugin_connected'] and info['state']=='CONNECTED'
        return dict(deployable=ready,authorized=ready,browser='plugin',session_label='Kalodata Plugin',
                    session_ref=self.session_ref,operations=OPERATIONS,state=info['state'])

    async def start_bridge(self):
        import socket
        import uvicorn
        from contextlib import nullcontext
        if self.server_task:return
        sock=socket.socket();sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        try:sock.bind(('127.0.0.1',self.port));sock.listen(128);sock.setblocking(False)
        except OSError:
            sock.close();self.bridge_error='PLUGIN_OFFLINE';return
        class LocalServer(uvicorn.Server):
            def capture_signals(self):return nullcontext()
            def install_signal_handlers(self):pass
        self.server=LocalServer(uvicorn.Config(self.asgi,log_level='critical',access_log=False,lifespan='off'))
        self.server_task=asyncio.create_task(self.server.serve(sockets=[sock]))
        for _ in range(40):
            if self.server.started:return
            if self.server_task.done():break
            await asyncio.sleep(.05)
        self.bridge_error='PLUGIN_OFFLINE'

    async def stop_bridge(self):
        if self.server:
            self.server.should_exit=True
            await self.server_task
        self.server=None;self.server_task=None

    async def observe(self,operation,payload):
        if operation not in OPERATIONS+['inspect_ui']:raise ValueError('unsupported operation')
        with self.lock:
            if self.job is not None:raise ConnectionError('material channel busy')
            future=Future();ident=secrets.token_hex(16)
            self.job=dict(id=ident,operation=operation,payload=payload);self.future=future
        try:return await asyncio.wait_for(asyncio.wrap_future(future),28)
        finally:
            with self.lock:
                if self.job and self.job['id']==ident:self.job=None;self.future=None
    async def cancel(self,run_id,generation):
        with self.lock:
            if self.job and self.job['payload'].get('run_id')==run_id:
                if self.future and not self.future.done():self.future.cancel()
                self.job=None;self.future=None
    def _app(self):
        app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
        @app.middleware('http')
        async def protect(request,call_next):
            host=request.headers.get('host','');origin=request.headers.get('origin','')
            if host not in (f'127.0.0.1:{self.port}',f'localhost:{self.port}','testserver'):return JSONResponse({'error':'local only'},403)
            is_extension=bool(re.fullmatch(r'chrome-extension://[a-p]{32}',origin))
            pair=request.url.path==PREFIX+'/pair'
            token=request.headers.get('authorization','')
            authenticated=bool(self.token and secrets.compare_digest(token,'Bearer '+self.token))
            pinned=bool(self.extension_id and origin=='chrome-extension://'+self.extension_id)
            poll_without_origin=not origin and authenticated and request.method=='GET' and request.url.path==PREFIX+'/job'
            if not ((pair and is_extension and (not self.extension_id or pinned)) or pinned or poll_without_origin):return JSONResponse({'error':'extension origin required'},403)
            if request.method=='OPTIONS':
                response=JSONResponse({})
            elif not pair and not authenticated:return JSONResponse({'error':'invalid pairing'},401)
            else:
                try:
                    if int(request.headers.get('content-length','0'))>1000000:return JSONResponse({'error':'too large'},413)
                except ValueError:return JSONResponse({'error':'invalid length'},400)
                response=await call_next(request)
            if origin:response.headers.update({'Access-Control-Allow-Origin':origin,'Access-Control-Allow-Methods':'GET,POST,OPTIONS','Access-Control-Allow-Headers':'Content-Type,Authorization','Access-Control-Allow-Private-Network':'true'})
            response.headers['Cache-Control']='no-store';response.headers['X-Content-Type-Options']='nosniff'
            return response
        @app.post(PREFIX+'/pair')
        async def pair(request:Request):
            d=await request.json()
            if not isinstance(d,dict):return JSONResponse({'error':'invalid payload'},400)
            ext=d.get('extension_id','')
            if not isinstance(ext,str):return JSONResponse({'error':'invalid extension'},400)
            if time.time()>self.code_expires or not isinstance(d.get('code'),str) or not secrets.compare_digest(d['code'],self.code) or not re.fullmatch('[a-p]{32}',ext) or request.headers.get('origin')!='chrome-extension://'+ext:return JSONResponse({'error':'pairing code invalid'},403)
            with self.lock:
                self.token=secrets.token_urlsafe(32);self.extension_id=ext;self.save();self.code_expires=0
            return dict(token=self.token,session_ref=self.session_ref)
        @app.get(PREFIX+'/job')
        async def job(bound:bool=False,version:str=None,build_id:str=None,kalodata_adapter:str=None):
            with self.lock:
                self.last_seen=time.monotonic();self.bound=bound
                if version is not None:self.plugin_version=version[:40]
                if build_id is not None:self.build_id=build_id[:80]
                if kalodata_adapter is not None:self.kalodata_adapter=kalodata_adapter[:80]
                return dict(job=None if self.info()['state'] not in (None,'CONNECTED') else self.job)
        @app.post(PREFIX+'/result')
        async def result(request:Request):
            d=await request.json()
            if not isinstance(d,dict):return JSONResponse({'error':'invalid payload'},400)
            with self.lock:
                if not self.job or d.get('id')!=self.job['id'] or not self.future or self.future.done():return {'accepted':False}
                p=self.job['payload']
                # Correlation is server-controlled, not trusted from page contents.
                response={k:d[k] for k in ['success','error_code','data','http_status','login_required','challenge','entitlement_denied','retry_after'] if k in d}
                response.update(session_ref=self.session_ref,operation_id=p.get('operation_id'),run_id=p.get('run_id'),generation=p.get('generation'))
                if response.get('success') is True:self.logged_in=True
                elif response.get('login_required') or response.get('error_code')=='LOGIN_REQUIRED':self.logged_in=False
                self.future.set_result(response)
            return {'accepted':True}
        return app
