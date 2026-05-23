"""Interactive one-shot Telegram login.

Run via:
    docker compose run --rm gateway python -m src.auth

Produces `sessions/<session_name>.session` which the main process reuses.
"""
from __future__ import annotations

import asyncio

from pyrogram import Client

from . import log as logmod
from .config import load


async def amain() -> None:
    cfg = load()
    logmod.setup(cfg.log_level)
    client = Client(
        name=cfg.telegram.session_name,
        api_id=cfg.telegram.api_id,
        api_hash=cfg.telegram.api_hash,
        workdir=str(cfg.telegram.session_dir),
    )
    await client.start()
    me = await client.get_me()
    print(f"signed in as {me.username or me.phone_number} (id={me.id})")
    print(f"session file: {cfg.telegram.session_dir / (cfg.telegram.session_name + '.session')}")
    await client.stop()


if __name__ == "__main__":
    asyncio.run(amain())
