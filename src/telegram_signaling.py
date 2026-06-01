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
from dataclasses import dataclass
from typing import Optional

from pyrogram import Client
from pyrogram.handlers import RawUpdateHandler
from pyrogram.raw import functions, types

from .config import TelegramConfig

log = logging.getLogger(__name__)


class CallDiscardedError(RuntimeError):
    pass


@dataclass
class IncomingTgCall:
    """An inbound Telegram call (phoneCallRequested), for the TG→SIP direction."""
    call_id: int
    access_hash: int
    caller_id: int       # admin_id — the Telegram user placing the call
    g_a_hash: bytes      # caller's g_a hash; fed to ntgcalls to derive g_b
    video: bool


def _protocol_tl(protocol: dict) -> types.PhoneCallProtocol:
    """Build a TL PhoneCallProtocol from the config's call_protocol dict.
    Telegram negotiates a library_version from the intersection of both sides'
    offers; ntgcalls then picks one it actually implements when ``connect()``
    is called. So config controls what we OFFER — what runs is still capped
    by what the installed ntgcalls supports."""
    return types.PhoneCallProtocol(
        min_layer=int(protocol["min_layer"]),
        max_layer=int(protocol["max_layer"]),
        udp_p2p=bool(protocol["udp_p2p"]),
        udp_reflector=bool(protocol["udp_reflector"]),
        library_versions=list(protocol["library_versions"]),
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
        self._accepted: Optional[asyncio.Future] = None    # -> g_b bytes (outgoing)
        self._established: Optional[asyncio.Future] = None  # -> PhoneCall (incoming)
        self._discarded: Optional[asyncio.Future] = None    # -> reason name
        self._on_remote_hangup = None
        self._on_signaling_in = None
        self._on_incoming_call = None
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

    def set_incoming_call_callback(self, cb) -> None:
        """cb(IncomingTgCall) — async; fired when someone calls us (TG→SIP)."""
        self._on_incoming_call = cb

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

    # ---- incoming call (TG→SIP): we are the callee ------------------------

    def bind_incoming(self, call: "IncomingTgCall") -> None:
        """Adopt an inbound call as the active one and arm the discard waiter.
        Call this once the gateway commits to handling it (before ringing SIP),
        so a caller cancel during SIP ringing is noticed."""
        loop = asyncio.get_event_loop()
        self._call_id = call.call_id
        self._access_hash = call.access_hash
        self._accepted = None
        self._established = None
        self._discarded = loop.create_future()
        self._sig_out_logged = False
        self._sig_in_logged = False

    async def received_call(self) -> None:
        """Tell Telegram we're ringing (caller's UI shows 'ringing', and it keeps
        the call alive while the SIP side is dialed)."""
        if self._call_id is None:
            return
        try:
            await self._client.invoke(functions.phone.ReceivedCall(
                peer=types.InputPhoneCall(id=self._call_id, access_hash=self._access_hash),
            ))
        except Exception as e:  # noqa: BLE001
            log.debug("receivedCall failed: %s", e)

    async def accept_call(self, g_b: bytes, protocol) -> None:
        """Send phone.acceptCall with our g_b. Arms the established waiter (the
        caller then confirms, arriving as an updatePhoneCall(phoneCall))."""
        if self._call_id is None:
            raise RuntimeError("no incoming call to accept")
        loop = asyncio.get_running_loop()
        self._established = loop.create_future()
        await self._client.invoke(functions.phone.AcceptCall(
            peer=types.InputPhoneCall(id=self._call_id, access_hash=self._access_hash),
            g_b=g_b,
            protocol=_protocol_tl(protocol),
        ))
        log.info("phone.acceptCall sent for %s", self._call_id)

    async def wait_established(self, timeout: float):
        """Block until the caller confirms (returns the established PhoneCall:
        g_a_or_b, key_fingerprint, connections, protocol, p2p_allowed) or the
        call is discarded."""
        assert self._established is not None and self._discarded is not None
        done, _ = await asyncio.wait(
            {self._established, self._discarded},
            timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise CallDiscardedError("timeout waiting for caller to confirm")
        fut = done.pop()
        if fut is self._discarded:
            raise CallDiscardedError(self._discarded.result())
        return self._established.result()

    async def discard_incoming(self, call: "IncomingTgCall", busy: bool = False) -> None:
        """Decline an inbound call we won't handle (busy / not whitelisted),
        without touching the active-call state."""
        reason = types.PhoneCallDiscardReasonBusy() if busy \
            else types.PhoneCallDiscardReasonHangup()
        try:
            await self._client.invoke(functions.phone.DiscardCall(
                peer=types.InputPhoneCall(id=call.call_id, access_hash=call.access_hash),
                duration=0, reason=reason, connection_id=0,
            ))
            log.info("declined inbound TG call %s (busy=%s)", call.call_id, busy)
        except Exception as e:  # noqa: BLE001
            log.warning("discard_incoming failed: %s", e)

    async def username_of(self, user_id: int) -> Optional[str]:
        """Return '@username' for a user id (for route matching), or None."""
        try:
            u = await self._client.get_users(user_id)
            return f"@{u.username}" if getattr(u, "username", None) else None
        except Exception as e:  # noqa: BLE001
            log.debug("username_of(%s) failed: %s", user_id, e)
            return None

    async def discard_call(self) -> None:
        if self._call_id is None:
            return
        call_id, access_hash = self._call_id, self._access_hash
        self._call_id = None
        self._access_hash = None
        self._accepted = self._established = self._discarded = None
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
        if isinstance(pc, types.PhoneCallRequested):
            incoming = IncomingTgCall(
                call_id=pc.id, access_hash=pc.access_hash, caller_id=pc.admin_id,
                g_a_hash=pc.g_a_hash, video=bool(getattr(pc, "video", False)),
            )
            log.info("incoming TG call from user %s (call_id=%s, video=%s)",
                     incoming.caller_id, incoming.call_id, incoming.video)
            if self._on_incoming_call:
                await self._on_incoming_call(incoming)
        elif isinstance(pc, types.PhoneCallAccepted):
            if self._accepted and not self._accepted.done():
                self._accepted.set_result(pc.g_b)
        elif isinstance(pc, types.PhoneCall):
            # Established: the caller confirmed our acceptCall (incoming/TG→SIP).
            if (self._established is not None and not self._established.done()
                    and self._call_id is not None and pc.id == self._call_id):
                self._established.set_result(pc)
        elif isinstance(pc, types.PhoneCallDiscarded):
            if self._call_id is None or pc.id != self._call_id:
                return
            reason = type(pc.reason).__name__ if pc.reason else "unknown"
            # If a setup waiter (wait_accepted / wait_established) is pending, let
            # it surface the discard; otherwise the call was live → it's a hangup.
            pending = ((self._accepted is not None and not self._accepted.done())
                       or (self._established is not None and not self._established.done()))
            if pending and self._discarded is not None and not self._discarded.done():
                self._discarded.set_result(reason)
            else:
                log.info("remote discarded active call (%s)", reason)
                self._call_id = None
                self._access_hash = None
                if self._on_remote_hangup:
                    await self._on_remote_hangup()
