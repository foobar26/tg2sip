from __future__ import annotations

import asyncio
import signal

from . import log as logmod
from .config import load
from .gateway import Gateway


async def amain() -> None:
    cfg = load()
    logmod.setup(cfg.log_level)
    gw = Gateway(cfg)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _request_stop(*_):
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_stop)

    runner = asyncio.create_task(gw.run())
    await stop
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(amain())
