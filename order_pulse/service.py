"""Local host for Balance, Kalodata and AOV monitoring."""
import asyncio
from .storage import Repository, Credentials
from .tms_core.monitor_domain import stamp
from .balance.module import BalanceModule
from .kalodata.module import KalodataModule
from .integrations.browser_binding import BrowserSpaceBinding, OperationsSpaceBinding
from .integrations.browser_control import BrowserControl
from .integrations.tms_login import TmsLogin

ERRORS={
 'WAITING_FOR_BROWSER':'浏览器使用中，自动任务仍保持启用。完成操作后可确认交还并查询。',
 'WAITING_USER':'浏览器使用中，请完成操作后再查询。',
 'WAITING_CONNECTION':'请保持已登录的马帮TMS订单页面打开。',
 'AUTH_REQUIRED':'请在马帮TMS页面重新登录。',
 'PERMISSION_DENIED':'TMS访问权限不可用，请检查账号权限。',
 'ACCOUNT_SCOPE_CHANGED':'TMS账号已改变，请确认原授权账号。',
 'RATE_LIMITED':'TMS暂时限制访问，请等待冷却结束。',
 'RELEASE_REQUIRED':'浏览器交还尚未确认，请先在浏览器中接回控制。',
 'SOURCE_CHANGED':'TMS页面暂时无法识别，已有结果保留。',
 'BINDING_REQUIRED':'请先在设置中选择已登录的TMS页面。',
 'INPUT_CHANGED':'查询期间设置已改变，未覆盖已有结果。',
 'SERVICE_INTERRUPTED':'上次查询已中断，已有结果保留。',
 'STORAGE_ERROR':'本机结果保存失败，已有结果保留。',
 'QUERY_FAILED':'本次查询失败，已有结果保留。',
}

class Service:
    def __init__(self, root, clock=stamp):
        self.repo=Repository(root)
        self.secrets=Credentials(root)
        self.clock=clock
        self.balance_browser_space=BrowserSpaceBinding(self.repo)
        self.browser_space=OperationsSpaceBinding(self.repo)
        self.balance=BalanceModule(root,self.secrets,
            browser_lock=self.balance_browser_space.lock,browser_binding=self.balance_browser_space)
        self.kalodata=KalodataModule(root,self.repo,self.secrets)
        self.browser_control=BrowserControl(self.browser_space,self.balance_browser_space)
        self.balance.bridge.return_to_workbench=lambda: self.browser_control.show_workbench('balance')
        self.browser_control.material_needs_user=lambda: self.kalodata.transport.logged_in is False
        self.tms_login=TmsLogin(self)
        if self.kalodata.production:
            self.kalodata.transport.operation_guard=self.browser_control.operation
        self.lock=asyncio.Lock()
        self.stopping=False
        self.timer=None
        self._apply_balance_binding()

    def _apply_balance_binding(self):
        target=self.balance_browser_space.target('balance')
        if target:
            self.balance.bridge.target=target
            self.balance.store.set('browser_target',target)

    def save_balance_binding(self,payload):
        self.balance_browser_space.save(payload)
        self._apply_balance_binding()
        return self.balance_browser_space.state()

    async def startup(self):
        await self.balance.startup()
        await self.kalodata.startup()
        if self.kalodata.production:self.browser_control.start()
        self.timer=asyncio.create_task(self.scheduler())

    async def shutdown(self):
        self.stopping=True
        if self.timer:
            self.timer.cancel()
            await asyncio.gather(self.timer,return_exceptions=True)
        await self.browser_control.stop()
        await self.kalodata.shutdown()
        await self.balance.shutdown()

    def state(self):
        return dict(busy=self.lock.locked(),feishu_configured=self.secrets.configured(),
            browser_bindings={'balance':self.balance_browser_space.state(),'operations':self.browser_space.state()},
            modules={
                'balance':{'status':'running' if self.balance.started else 'stopped',
                           'paused':self.balance.store.settings()['paused']},
                'kalodata':{'status':'ready' if self.kalodata.transport.info()['state']=='CONNECTED' else 'waiting_connection'},
                'aov-monitor':{'status':'running'},
            })

    async def scheduler(self):
        while not self.stopping:
            try:await self.balance.tick()
            except asyncio.CancelledError:raise
            except Exception:self.balance.store.log('scheduler','余额调度内部错误，将在下一轮重试',level='error')
            await asyncio.sleep(2)
