"""Bounded browser worker execution shared by the retained monitors."""
import asyncio

class BrowserError(Exception):
    pass

async def blocking(function, *args):
    """Cancellation waits for a bounded adapter operation before releasing control.

    asyncio.to_thread alone does not terminate a worker; releasing early would let
    the worker continue operating a user's browser. The source adapter is bounded.
    """
    worker=asyncio.create_task(asyncio.to_thread(function,*args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try: await worker
        except Exception: pass
        raise
