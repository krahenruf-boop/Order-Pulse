from fastapi import APIRouter,Request

def create_router(monitor):
    router=APIRouter(prefix='/api/api-monitor')
    @router.get('/state')
    async def state():return monitor.state()
    @router.post('/settings/{provider}')
    async def settings(provider:str,request:Request):return await monitor.settings(provider,await request.json())
    @router.post('/check/{provider}',status_code=202)
    async def check(provider:str,request:Request):
        if await request.json()!={}:raise ValueError('检测请求无效')
        return await monitor.start(provider)
    @router.get('/history/{provider}')
    async def history(provider:str):return monitor.history(provider)
    @router.post('/test')
    async def test(request:Request):
        if await request.json()!={}:raise ValueError('测试请求无效')
        return await monitor.test()
    return router
