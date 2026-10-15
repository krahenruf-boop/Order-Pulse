"""Kalodata owns its thread, event loop, SQLite connection, tasks and cooldown.
The host event loop never performs SQLite or waits synchronously on this module.
"""
import asyncio
import inspect
import threading
from concurrent.futures import Future
from pathlib import Path
from .repository import Repository
from .service import Service
from .router import create_router
from .models import KalodataError
from .connectors.browser_provider import BrowserProvider

class KalodataRuntime:
    def __init__(self,data_root,enabled=False,connector=None):
        self.enabled=bool(enabled);self.repo=Repository(Path(data_root).resolve()/'order_pulse.sqlite3')
        self.service=Service(self.repo,connector or BrowserProvider(),False)
        self.router=create_router(self);self._loop=None;self._thread=None;self._lifecycle=None
    async def _ensure_thread(self):
        if self._loop:return
        ready=Future()
        def worker():
            loop=asyncio.new_event_loop();asyncio.set_event_loop(loop);self._loop=loop;ready.set_result(None)
            try:loop.run_forever()
            finally:loop.close()
        self._thread=threading.Thread(target=worker,name='kalodata-local-runtime',daemon=True);self._thread.start()
        await asyncio.wrap_future(ready)
    async def dispatch(self,func,*args):
        if self._loop is None:
            result=func(*args)
            return await result if inspect.isawaitable(result) else result
        async def invoke():
            result=func(*args)
            return await result if inspect.isawaitable(result) else result
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(invoke(),self._loop))
    async def call(self,method,*args):return await self.dispatch(getattr(self.service,method),*args)
    async def call_repo(self,method,*args):return await self.dispatch(getattr(self.repo,method),*args)
    async def startup(self):
        desired=self.enabled;self.enabled=False
        await self.set_enabled(desired)
    async def set_enabled(self,enabled):
        if self._lifecycle is None:self._lifecycle=asyncio.Lock()
        async with self._lifecycle:
            desired=bool(enabled)
            if desired==self.enabled and (self.repo.db is not None or not self.repo.path.exists()) and self.service.enabled==desired:return
            if desired or self.repo.path.exists():await self._ensure_thread()
            async def change():
                await self.service.shutdown()
                self.repo.open(desired)
                if desired:self.repo.recover()
                self.service.enabled=desired
            await self.dispatch(change)
            self.enabled=desired
    async def shutdown(self):
        try:
            await self.call('shutdown')
            await self.call_repo('close')
        finally:
            if self._loop:self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:await asyncio.get_running_loop().run_in_executor(None,self._thread.join,2)
            self._loop=None;self._thread=None

def create_runtime(data_root,enabled=False,connector=None):return KalodataRuntime(data_root,enabled,connector)
