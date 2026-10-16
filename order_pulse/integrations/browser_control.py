"""Independent control policies for the two explicitly authorized Spaces."""
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from ..browser import blocking
from ..kalodata.models import KalodataError
from .ego_runtime import execute, EgoRuntimeError
from .input_idle import input_idle_seconds


class BrowserControl:
    def __init__(self, operations, balance):
        self.operations, self.balance = operations, balance
        self.worker = None
        self.states = {}
        self.workbench_states = {}
        self.material_needs_user = lambda: False

    def show_workbench(self, module, *, locked=False):
        """Best-effort completion UI; never competes with another source operation."""
        if module not in ('balance', 'aov-monitor', 'kalodata'):
            return 'INVALID_MODULE'
        owner = self.balance if module == 'balance' else self.operations
        acquired = False
        if not locked:
            acquired = owner.lock.acquire(blocking=False)
            if not acquired:
                return 'BUSY'
        try:
            binding = owner.get()
            if not binding:
                return 'BINDING_REQUIRED'
            result = execute(Path(__file__).with_name('ego_workbench.js'),
                             dict(binding=binding, module=module),
                             'ORDER_PULSE_WORKBENCH:', timeout=8)
            state = result.get('state', 'UNAVAILABLE')
        except Exception:
            # A tab closing or an unavailable browser must not replace a task result.
            state = 'UNAVAILABLE'
        finally:
            if acquired:
                owner.lock.release()
        self.workbench_states[module] = state
        return state

    def ensure(self, binding, reason):
        if not binding:
            return 'BINDING_REQUIRED'
        try:
            result = execute(Path(__file__).with_name('ego_control.js'), dict(
                binding=binding, reason=reason, python=sys.executable,
                idle_script=str(Path(__file__).with_name('input_idle.py'))),
                'ORDER_PULSE_CONTROL:', timeout=10)
            state = result.get('state', 'UNAVAILABLE')
            if binding['purpose'] == 'material_aov' and result.get('browser'):
                self.operations.record_space(binding, result['browser'])
        except ValueError:
            state = 'BINDING_REQUIRED'
        except EgoRuntimeError:
            state = 'UNAVAILABLE'
        self.states[binding['purpose']] = state
        return state

    @asynccontextmanager
    async def operation(self):
        # Both runtimes share a thread mutex; never block either event loop.
        deadline = asyncio.get_running_loop().time() + 20
        while not self.operations.lock.acquire(blocking=False):
            if asyncio.get_running_loop().time() >= deadline:
                raise KalodataError('BROWSER_BUSY', 409, retryable=True)
            await asyncio.sleep(.1)
        controlled = False
        try:
            state = await blocking(self.ensure, self.operations.get(), 'task')
            if state != 'CONTROLLED':
                raise KalodataError('BROWSER_CONTROL_UNAVAILABLE', 503, retryable=True)
            controlled = True
            yield
        finally:
            try:
                if controlled and not self.material_needs_user():
                    await blocking(lambda: self.show_workbench('kalodata', locked=True))
            finally:
                # Normal idle state is Agent-controlled; release only the mutex.
                self.operations.lock.release()

    async def maintain_once(self):
        await blocking(self.ensure, self.balance.get(), 'balance')
        idle = input_idle_seconds()
        if idle is not None and idle >= 300 and not self.operations.lock.locked():
            await blocking(self.ensure, self.operations.get(), 'idle')
        # This local timer does not run source checks or fetch orders.
        return max(1, 300 - idle) if idle is not None and idle < 300 else 300

    async def maintain(self):
        while True:
            try:
                delay = await self.maintain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = 300
            await asyncio.sleep(delay)

    def start(self):
        if self.worker is None:
            self.worker = asyncio.create_task(self.maintain())

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
