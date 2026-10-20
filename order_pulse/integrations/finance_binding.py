from pathlib import Path
from .ego_runtime import execute
from .browser_binding import validate_binding
from ..browser import blocking

async def candidates():
    return await blocking(lambda:execute(Path(__file__).with_name('ego_finance_binding.js'),{},'ORDER_PULSE_BINDING:',timeout=10))

async def rebind(host,selection):
    if not isinstance(selection,dict) or set(selection)!={'space_id','expected_name','profile_id','profile_name'}:raise ValueError('请选择实际浏览器空间')
    probe=dict(selection,purpose='balance_kalodata',auto_takeover=True,tabs={'balance_tab':'p1','kalodata_tab':None});validate_binding(probe)
    if host.balance.engine.status().get('busy'):raise ValueError('正在采集，请结束后重新绑定')
    lock=host.balance_browser_space.lock
    def verify():
        if not lock.acquire(blocking=False):raise ValueError('财务浏览器正在执行任务')
        try:
            reply=execute(Path(__file__).with_name('ego_finance_binding.js'),dict(selection=selection),'ORDER_PULSE_BINDING:',timeout=15)
            b=validate_binding(reply['binding']);target=dict(space_id=b['space_id'],space_name=b['expected_name'],page_label=b['tabs']['balance_tab'],expected_profile_id=b['profile_id'],expected_profile_name=b['profile_name'],purpose=b['purpose'],auto_takeover=True)
            settings=host.balance.store.settings()
            result=execute(Path(__file__).with_name('ego_balance.js'),dict(target,job=dict(action='inspect',collection_mode='json',currency=settings['currency'],max_age_minutes=settings['max_age_minutes'],source_timezone=settings['source_timezone'])),'ORDER_PULSE_BALANCE:',timeout=120,max_output=4000000)
            if not result.get('ok'):raise ValueError('Linkme 登录或账户范围无法核验，请打开资产列表并登录')
            metadata=result.get('metadata',{})
            if metadata.get('owner_bc') is not True or metadata.get('filters_clear') is not True:raise ValueError('Linkme 账户范围无法核验')
            host.save_balance_binding(b)
            host.balance.store.set('browser_inspection',metadata)
            return host.balance_browser_space.state()
        finally:lock.release()
    try:return await blocking(verify)
    finally:await blocking(host.browser_control.show_workbench,'balance')
