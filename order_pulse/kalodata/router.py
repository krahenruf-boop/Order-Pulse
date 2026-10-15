import json
import uuid
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse,Response
from .models import KalodataError,is_id
from .exports import make_export

PREFIX='/api/materials/kalodata/v1'
def create_router(runtime):
    router=APIRouter(prefix=PREFIX)
    async def safe(action,status=200):
        try:return await action()
        except KalodataError as exc:return JSONResponse(exc.payload(uuid.uuid4().hex),status_code=exc.status)
        except Exception:return JSONResponse(KalodataError('STORAGE_ERROR',503).payload(uuid.uuid4().hex),status_code=503)
    async def payload(request):
        try:return await request.json()
        except (ValueError,UnicodeError):raise KalodataError('INVALID_REQUEST',422)
    @router.get('/capabilities')
    async def capabilities():return await safe(lambda:runtime.call('capabilities'))
    @router.post('/jobs',status_code=202)
    async def extract_product(request:Request):
        async def action():return await runtime.call('extract_product',await payload(request))
        return await safe(action)
    @router.post('/preflight')
    async def preflight(request:Request):
        async def action():return await runtime.call('preflight',await payload(request))
        return await safe(action)
    @router.post('/runs',status_code=202)
    async def start(request:Request):
        async def action():return await runtime.call('start',await payload(request))
        return await safe(action)
    @router.get('/runs')
    async def runs(limit:str='20'):
        async def action():
            if not limit.isdigit() or not 1<=int(limit)<=100:raise KalodataError('INVALID_REQUEST',422)
            return dict(runs=await runtime.call_repo('list_runs',int(limit)),next_cursor=None)
        return await safe(action)
    @router.get('/runs/{run_id}')
    async def run(run_id:str):return await safe(lambda:runtime.call_repo('get_run',run_id))
    @router.get('/runs/{run_id}/items')
    async def items(run_id:str,cursor:str=None,limit:str='100'):
        async def action():
            if not limit.isdigit():raise KalodataError('INVALID_REQUEST',422)
            return await runtime.call('items',run_id,cursor,int(limit))
        return await safe(action)
    @router.post('/runs/{run_id}/cancel',status_code=202)
    async def cancel(run_id:str):return await safe(lambda:runtime.call('cancel',run_id))
    @router.post('/runs/{run_id}/retry',status_code=202)
    async def retry(run_id:str,request:Request):
        async def action():return await runtime.call('start',await payload(request),run_id)
        return await safe(action)
    @router.get('/runs/{run_id}/links')
    async def links(run_id:str,scope:str='all'):return await safe(lambda:runtime.call('links',run_id,scope))
    @router.get('/runs/{run_id}/export')
    async def export(run_id:str,format:str='txt',scope:str='all'):
        async def action():
            run=await runtime.call_repo('get_run',run_id)
            body,mime,filename=make_export(run,format,scope)
            return Response(body,media_type=mime,headers={'Content-Disposition':'attachment; filename="'+filename+'"','X-Result-Revision':str(run['result_revision'])})
        return await safe(action)
    @router.get('/history')
    async def history(product_id:str=None,region:str=None,cursor:str=None,limit:str='100'):
        async def action():
            if product_id and not is_id(product_id):raise KalodataError('INVALID_PRODUCT_URL',422)
            if not limit.isdigit() or not 1<=int(limit)<=500 or (cursor and not cursor.isdigit()):raise KalodataError('INVALID_CURSOR',422)
            size=int(limit);offset=int(cursor or '0')
            rows=await runtime.call_repo('history',product_id,region,offset,size+1)
            return dict(items=rows[:size],next_cursor=str(offset+size) if len(rows)>size else None)
        return await safe(action)
    return router
