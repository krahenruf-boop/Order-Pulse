"""Explicitly verify an existing Materials Space after a rename or tab change."""
from pathlib import Path
from .browser_binding import OperationsSpaceBinding
from .ego_runtime import execute, EgoRuntimeError
from ..browser import blocking

SCRIPT=Path(__file__).with_name('ego_material_binding.js')
MARKER='ORDER_PULSE_MATERIAL_BINDING:'
IDENTITY={'space_id','expected_name','profile_id','profile_name'}

async def candidates():
    return await blocking(lambda:execute(SCRIPT,{},MARKER,timeout=10))

async def rebind(host,selection):
    if not isinstance(selection,dict) or set(selection)!=IDENTITY:
        raise ValueError('请选择实际浏览器空间')
    OperationsSpaceBinding.validate(dict(selection,purpose='material_aov',auto_takeover=True,
        tabs=dict(tms_tab='p1',miaoshou_tab='p2',kalodata_tab='p3')))
    def verify():
        owner=host.browser_space
        if not owner.lock.acquire(blocking=False):raise ValueError('素材空间正在执行任务，请结束后重新绑定')
        try:
            expected=owner.get()
            try:reply=execute(SCRIPT,dict(selection=selection,previous=expected),MARKER,timeout=15)
            except EgoRuntimeError:raise ValueError('空间核验未通过，请保留本站、马帮、妙手及 Kalodata 页面后重试') from None
            clean=OperationsSpaceBinding.validate(reply['binding'])
            if any(clean[k]!=selection[k] for k in IDENTITY):raise ValueError('浏览器空间身份已改变，请重新选择')
            owner.replace_verified(expected,clean)
            return owner.state()
        finally:owner.lock.release()
    return await blocking(verify)
