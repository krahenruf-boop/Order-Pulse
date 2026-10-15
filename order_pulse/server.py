import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
from pathlib import Path
import secrets
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from .service import Service, ERRORS
from .browser import BrowserError

PORT=8786

def create_app(root=None, service=None, port=PORT):
    if type(port) is not int or not 1024 <= port <= 65535 or port == 8765:
        raise ValueError('独立本机端口无效')
    root=Path(root or os.environ.get('ORDER_PULSE_HOME',Path.home()/'Library/Application Support/Order Pulse'))
    s=service or Service(root)
    from .aov_monitor.service import Monitor
    from .aov_monitor.router import create_router as aov_router
    aov=Monitor(s)
    csrf=secrets.token_urlsafe(32)
    allowed={f'127.0.0.1:{port}',f'localhost:{port}'}
    origins={'http://'+h for h in allowed}
    @asynccontextmanager
    async def life(app):
        await s.startup()
        await aov.startup()
        try:yield
        finally:
            await aov.shutdown()
            await s.shutdown()
    app=FastAPI(lifespan=life,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.service=s
    app.include_router(s.kalodata.runtime.router)
    app.include_router(aov_router(aov))
    app.state.aov_monitor=aov

    @app.middleware('http')
    async def local_only(request,call_next):
        if request.headers.get('host','') not in allowed:
            return JSONResponse({'error':'仅允许本机访问'},403)
        origin=request.headers.get('origin','')
        if origin and origin not in origins:
            return JSONResponse({'error':'来源页面不受信任'},403)
        if request.method not in ('GET','HEAD'):
            if origin not in origins or not secrets.compare_digest(request.headers.get('x-csrf-token',''),csrf):
                return JSONResponse({'error':'请刷新页面后再试'},403)
            body=await request.body()
            if len(body)>32000:return JSONResponse({'error':'请求过大'},413)
        response=await call_next(request)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
         'Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"})
        return response

    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse({'error':str(exc)},400)

    @app.exception_handler(BrowserError)
    async def browser_unavailable(request,exc):
        return JSONResponse({'error':ERRORS.get(str(exc),'浏览器暂不可用，请检查TMS页面。')},409)

    @app.exception_handler(Exception)
    async def failed(request,exc):return JSONResponse({'error':'操作未完成，已有结果保留。'},500)

    @app.get('/api/health')
    async def health():
        import hashlib
        return {'app':'order-pulse','status':'ok','pid':os.getpid(),
                'data_root_id':hashlib.sha256(str(root.expanduser().resolve()).encode()).hexdigest()}

    @app.get('/api/state')
    async def state():return dict(s.state(),csrf=csrf)

    @app.get('/api/balance/state')
    async def balance_state():return s.balance.state()

    @app.get('/api/browser-binding')
    async def browser_binding_get():return s.balance_browser_space.state()

    @app.post('/api/browser-binding')
    async def browser_binding_save(request:Request):return s.save_balance_binding(await request.json())

    @app.post('/api/balance/settings')
    async def balance_settings(request:Request):return s.balance.save_settings(await request.json())

    @app.post('/api/balance/accounts')
    async def balance_accounts(request:Request):
        value=await request.json();return s.balance.save_accounts(value.get('accounts') if isinstance(value,dict) else None)

    @app.post('/api/balance/credentials')
    async def balance_credentials(request:Request):return s.balance.save_credentials(await request.json())

    @app.post('/api/balance/action')
    async def balance_action(request:Request):
        value=await request.json()
        if not isinstance(value,dict) or set(value)!={'action'} or not isinstance(value['action'],str):raise ValueError('余额操作无效')
        return await s.balance.action(value['action'])

    @app.post('/api/balance/browser-target')
    async def balance_browser_target(request:Request):
        return s.balance.bind_browser(await request.json())

    @app.post('/api/balance/preview')
    async def balance_preview(request:Request):return s.balance.preview(await request.json())

    @app.get('/api/kalodata/settings')
    async def kalodata_settings_get():return s.kalodata.settings()

    @app.post('/api/kalodata/settings')
    async def kalodata_settings_save(request:Request):return s.kalodata.save_settings(await request.json())

    @app.post('/api/kalodata/browser-target')
    async def kalodata_browser_target(request:Request):return s.kalodata.bind_browser(await request.json())

    @app.get('/api/kalodata/status')
    async def kalodata_status():return await s.kalodata.status()

    @app.post('/api/kalodata/health')
    async def kalodata_health():return await s.kalodata.health()

    @app.get('/api/materials/kalodata/v1/connection')
    async def kalodata_connection():return s.kalodata.connection()

    @app.post('/api/materials/kalodata/v1/connection/reset')
    async def kalodata_connection_reset():return s.kalodata.reset_connection()

    @app.post('/api/credentials')
    async def credentials(request:Request):
        data=await request.json()
        if set(data)-{'webhook','secret','keyword'} or any(not isinstance(v,str) or len(v)>2048 for v in data.values()):raise ValueError('飞书设置无效')
        current=s.secrets.get()
        for k,v in data.items():
            if v:current[k]=v
        s.secrets.save(current)
        return {'configured':s.secrets.configured()}

    static=Path(__file__).parent.parent/'static'
    @app.get('/')
    async def index():return FileResponse(static/'index.html')
    app.mount('/static',StaticFiles(directory=static),name='static')
    return app

def main():
    import uvicorn
    # Single-instance file lock survives closing the web page; never shares old port.
    import fcntl
    root=Path(os.environ.get('ORDER_PULSE_HOME',Path.home()/'Library/Application Support/Order Pulse'))
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=(root/'service.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('Order Pulse is already running')
    uvicorn.run(create_app(root),host='127.0.0.1',port=PORT,access_log=False,log_level='warning')

if __name__=='__main__':main()
