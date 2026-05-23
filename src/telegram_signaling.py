"""Telegram P2P call signaling via Pyrogram raw API.

Thin MTProto layer: it shuttles the `phone.requestCall` / `phone.confirmCall` /
`phone.discardCall` messages and routes incoming `updatePhoneCall` events. It
deliberately does **not** compute the Diffie-Hellman key — ntgcalls owns the DH
and the call key (see `telegram_media.py`). This module only moves the public
material (`g_a_hash`, `g_b`, `g_a`, `key_fingerprint`, relay connections).
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Optional

from pyrogram import Client
from pyrogram.handlers import RawUpdateHandler
from pyrogram.raw import functions, types

from .config import TelegramConfig

log = logging.getLogger(__name__)


class CallDiscardedError(RuntimeError):
    pass


def _protocol_tl(protocol) -> types.PhoneCallProtocol:
    """Build a TL PhoneCallProtocol from an ntgcalls Protocol object."""
    return types.PhoneCallProtocol(
        min_layer=protocol.min_layer,
        max_layer=protocol.max_layer,
        udp_p2p=protocol.udp_p2p,
        udp_reflector=protocol.udp_reflector,
        library_versions=list(protocol.library_versions),
    )


class TelegramSignaling:
    """One signaling session. Wraps a Pyrogram Client and tracks one active call."""

    def __init__(self, cfg: TelegramConfig):
        self._cfg = cfg
        self._client = Client(
            name=cfg.session_name,
            api_id=cfg.api_id,
            api_hash=cfg.api_hash,
            workdir=str(cfg.session_dir),
            no_updates=False,
        )
        self._call_id: Optional[int] = None
        self._access_hash: Optional[int] = None
        self._accepted: Optional[asyncio.Future] = None   # -> g_b bytes
        self._discarded: Optional[asyncio.Future] = None   # -> reason name
        self._on_remote_hangup = None
        self._on_signaling_in = None
        self._sig_out_logged = False
        self._sig_in_logged = False
        self._peer_cache: dict[str, types.InputUser] = {}

    async def start(self) -> None:
        await self._client.start()
        self._client.add_handler(RawUpdateHandler(self._on_update))
        me = await self._client.get_me()
        log.info("telegram signed in as %s (id=%s)", me.username or me.phone_number, me.id)

    async def stop(self) -> None:
        await self._client.stop()

    def set_remote_hangup_callback(self, cb) -> None:
        self._on_remote_hangup = cb

    def set_signaling_in_callback(self, cb) -> None:
        """cb(data: bytes) — async; fed incoming updatePhoneCallSignalingData."""
        self._on_signaling_in = cb

    async def send_signaling_out(self, user_id: int, data: bytes) -> None:
        """Forward ntgcalls' outgoing ICE/handshake blob to the peer."""
        if self._call_id is None:
            return
        try:
            await self._client.invoke(
                functions.phone.SendSignalingData(
                    peer=types.InputPhoneCall(id=self._call_id, access_hash=self._access_hash),
                    data=data,
                )
            )
            if not self._sig_out_logged:
                self._sig_out_logged = True
                log.debug("phone.sendSignalingData OK (first, %d bytes)", len(data))
        except Exception as e:  # noqa: BLE001
            log.warning("sendSignalingData failed: %s", e)

    async def send_text(self, user_id: int, text: str) -> None:
        try:
            await self._client.send_message(user_id, text)
        except Exception as e:  # noqa: BLE001
            log.warning("send_message failed: %s", e)

    async def get_dh_config(self) -> tuple[int, bytes, bytes]:
        dh = await self._client.invoke(
            functions.messages.GetDhConfig(version=0, random_length=256)
        )
        if not isinstance(dh, types.messages.DhConfig):
            raise RuntimeError(f"unexpected DhConfig response: {type(dh).__name__}")
        return dh.g, dh.p, dh.random

    async def resolve_target(self, target) -> tuple[int, "types.InputUser"]:
        """Resolve a call target to (user_id, InputUser). Accepts an int TG user
        id, a '+<digits>' phone number (looked up / imported as a contact), or a
        '@username'. Results are cached for the session."""
        key = str(target).strip()
        if key in self._peer_cache:
            iu = self._peer_cache[key]
            return iu.user_id, iu

        if isinstance(target, str) and re.fullmatch(r"\+\d{5,15}", key):
            iu = await self._resolve_phone(key)
        else:
            peer = await self._client.resolve_peer(target)  # int id or @username
            if not isinstance(peer, types.InputPeerUser):
                raise RuntimeError(f"target {target!r} is not a user ({type(peer).__name__})")
            iu = types.InputUser(user_id=peer.user_id, access_hash=peer.access_hash)

        self._peer_cache[key] = iu
        return iu.user_id, iu

    async def _resolve_phone(self, phone: str) -> "types.InputUser":
        # Import the number as a contact to discover the Telegram user (also helps
        # the callee get a proper ring, since the gateway becomes a contact).
        imported = await self._client.invoke(
            functions.contacts.ImportContacts(
                contacts=[types.InputPhoneContact(
                    client_id=random.getrandbits(63),
                    phone=phone, first_name="tg2sip", last_name="",
                )]
            )
        )
        for u in imported.users:
            return types.InputUser(user_id=u.id, access_hash=u.access_hash)
        raise RuntimeError(f"phone {phone} is not a Telegram user")

    async def request_call(self, input_user: "types.InputUser", g_a_hash: bytes,
                           protocol, video: bool = False) -> None:
        """Send phone.requestCall to an already-resolved peer. Arms the
        accepted/discarded futures."""
        if self._call_id is not None:
            raise RuntimeError("another call is already active")

        loop = asyncio.get_running_loop()
        self._accepted = loop.create_future()
        self._discarded = loop.create_future()
        self._sig_out_logged = False
        self._sig_in_logged = False

        result = await self._client.invoke(
            functions.phone.RequestCall(
                user_id=input_user,
                random_id=random.randint(0, 0x7FFFFFFF - 1),
                g_a_hash=g_a_hash,
                protocol=_protocol_tl(protocol),
                video=video,
            )
        )
        self._call_id = result.phone_call.id
        self._access_hash = result.phone_call.access_hash
        log.info("phone.requestCall sent id=%s", self._call_id)

    async def wait_accepted(self, timeout: float) -> bytes:
        """Block until the callee accepts (returns g_b) or the call is discarded."""
        assert self._accepted is not None and self._discarded is not None
        done, _ = await asyncio.wait(
            {self._accepted, self._discarded},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise CallDiscardedError("timeout waiting for answer")
        fut = done.pop()
        if fut is self._discarded:
            raise CallDiscardedError(self._discarded.result())
        return self._accepted.result()

    async def confirm_call(self, g_a: bytes, key_fingerprint: int, protocol):
        """Send phone.confirmCall. Returns (connections, library_versions, p2p_allowed)."""
        if self._call_id is None:
            raise RuntimeError("no active call to confirm")
        confirmed = await self._client.invoke(
            functions.phone.ConfirmCall(
                peer=types.InputPhoneCall(id=self._call_id, access_hash=self._access_hash),
                g_a=g_a,
                key_fingerprint=key_fingerprint,
                protocol=_protocol_tl(protocol),
            )
        )
        pc = confirmed.phone_call
        log.info("phone.confirmCall ok, fingerprint=%x", key_fingerprint & 0xFFFFFFFFFFFFFFFF)
        return pc.connections, pc.protocol.library_versions, pc.p2p_allowed

    async def discard_call(self) -> None:
        if self._call_id is None:
            return
        call_id, access_hash = self._call_id, self._access_hash
        self._call_id = None
        self._access_hash = None
        try:
            await self._client.invoke(
                functions.phone.DiscardCall(
                    peer=types.InputPhoneCall(id=call_id, access_hash=access_hash),
                    duration=0,
                    reason=types.PhoneCallDiscardReasonHangup(),
                    connection_id=0,
                )
            )
            log.info("phone.discardCall sent for %s", call_id)
        except Exception as e:  # noqa: BLE001
            log.warning("discardCall failed: %s", e)

    async def _on_update(self, _client, update, _users, _chats) -> None:
        if isinstance(update, types.UpdatePhoneCallSignalingData):
            if self._call_id is not None and update.phone_call_id == self._call_id:
                if not self._sig_in_logged:
                    self._sig_in_logged = True
                    log.debug("recv updatePhoneCallSignalingData (first, %d bytes)", len(update.data))
                if self._on_signaling_in:
                    await self._on_signaling_in(update.data)
            return
        if not isinstance(update, types.UpdatePhoneCall):
            return
        pc = update.phone_call
        if isinstance(pc, types.PhoneCallAccepted):
            if self._accepted and not self._accepted.done():
                self._accepted.set_result(pc.g_b)
        elif isinstance(pc, types.PhoneCallDiscarded):
            reason = type(pc.reason).__name__ if pc.reason else "unknown"
            if self._discarded and not self._discarded.done():
                self._discarded.set_result(reason)
            elif self._call_id is not None and pc.id == self._call_id:
                log.info("remote discarded active call (%s)", reason)
                self._call_id = None
                self._access_hash = None
                if self._on_remote_hangup:
                    await self._on_remote_hangup()
