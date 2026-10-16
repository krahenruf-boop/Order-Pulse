from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

def create_router(monitor):
    router=APIRouter(prefix='/api/aov-monitor')
    @router.get('/state')
    async def state():return monitor.state()
    @router.get('/config')
    async def config():return monitor.store.config()
    @router.get('/login')
    async def login_state():return monitor.host.tms_login.state()
    @router.post('/login')
    async def login_save(request:Request):
        from ..browser import blocking
        if monitor.worker and not monitor.worker.done():raise ValueError('正在采集，请结束后修改登录设置')
        return await blocking(monitor.host.tms_login.configure,await request.json())
    @router.post('/config')
    async def save(request:Request):return monitor.settings(await request.json())
    @router.get('/mappings')
    async def mappings():
        return {'shops':monitor.store.config()['shops'],'source':monitor.source.status()}
    @router.post('/detect',status_code=202)
    async def detect(request:Request):
        body=await request.json()
        if not isinstance(body,dict) or set(body)-{'shop_ids','retry_id'}:raise ValueError('检测请求无效')
        if not body:return await monitor.start()
        return await monitor.start(shop_ids=body.get('shop_ids'),retry_id=body.get('retry_id'))
    @router.get('/history')
    async def history():return {'runs':monitor.store.history(),'notifications':monitor.store.events()}
    @router.post('/resume',status_code=202)
    async def resume(request:Request):
        if await request.json()!={}:raise ValueError('恢复请求无效')
        return await monitor.resume()
    @router.get('/history/{rid}')
    async def detail(rid:str):
        result=monitor.store.snapshot(rid)
        return result if result else JSONResponse({'error':'记录不存在'},404)
    @router.get('/history/{rid}/orders/{shop_id}')
    async def orders(rid:str,shop_id:str,page:int=1):return monitor.store.orders(rid,shop_id,page)
    return router
