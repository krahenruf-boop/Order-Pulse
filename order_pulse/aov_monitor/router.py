from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

def create_router(monitor):
    router=APIRouter(prefix='/api/aov-monitor')
    @router.get('/state')
    async def state():return monitor.state()
    @router.get('/config')
    async def config():return monitor.store.config()
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
    @router.get('/history/{rid}')
    async def detail(rid:str):
        result=monitor.store.snapshot(rid)
        return result if result else JSONResponse({'error':'记录不存在'},404)
    @router.get('/history/{rid}/orders/{shop_id}')
    async def orders(rid:str,shop_id:str,page:int=1):return monitor.store.orders(rid,shop_id,page)
    return router
