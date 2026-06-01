"""Gateway orchestrator.

Maintains the SIP↔TG call state machine. Handles one call at a time. Threads
PJSUA2 callbacks (which fire on PJSIP's worker thread) onto the asyncio loop.

P2P setup order (ntgcalls owns the DH/key):

    create_p2p_call → init_exchange(g_a_hash) → phone.requestCall
    → wait phoneCallAccepted(g_b) → exchange_keys → phone.confirmCall
    → connect_p2p → answer SIP 200 OK → wire audio bridge port
"""
from __future__ import annotations

import asyncio
import logging
import re
from enum import Enum, auto
from typing import Optional

import pjsua2 as pj  # type: ignore[import-not-found]

from .audio_bridge import JitterBuffer
from .config import Config
from .sip_agent import IncomingCall, SipAgent, SipCall
from .telegram_media import TelegramMedia
from .telegram_signaling import (
    CallDiscardedError, IncomingTgCall, TelegramSignaling,
)

log = logging.getLogger(__name__)

# Telegram callee has this long to pick up before we give up on the call.
ANSWER_TIMEOUT_S = 60.0
# How long to ring the SIP side (TG→SIP) before giving up.
SIP_ANSWER_TIMEOUT_S = 45.0
# ntgcalls connection states that mean the media leg died mid-call.
_FAILED_STATES = ("FAIL", "TIMEOUT")


class State(Enum):
    IDLE = auto()
    SIP_RINGING = auto()      # accepted SIP, ringing TG
    TG_CONFIRMING = auto()    # TG accepted, exchanging keys / starting media
    TG_RINGING = auto()       # inbound TG call, ringing SIP (TG→SIP)
    BRIDGED = auto()
    TEARDOWN = auto()


class Gateway:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._state = State.IDLE
        self._lock = asyncio.Lock()

        self._sip_call: Optional[SipCall] = None
        self._playback: Optional[JitterBuffer] = None
        self._tg_media: Optional[TelegramMedia] = None
        self._sip_port: Optional[pj.AudioMediaPort] = None
        self._active_uid: Optional[int] = None   # resolved TG user id for the live call
        self._incoming_task: Optional[asyncio.Task] = None  # TG→SIP setup task

        self._sip = SipAgent(cfg.sip, on_incoming_call=self._on_incoming_sip_threadsafe)
        self._tg_sig = TelegramSignaling(cfg.telegram)
        self._tg_sig.set_remote_hangup_callback(self._on_tg_remote_hangup)
        self._tg_sig.set_signaling_in_callback(self._on_tg_signaling_in)
        self._tg_sig.set_incoming_call_callback(self._on_tg_incoming)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._sip.start()
        await self._tg_sig.start()
        log.info("gateway up (SIP↔TG); SIP→TG fallback uid=%s, TG→SIP routes=%d",
                 self._cfg.telegram.forward_user_id or "none",
                 len(self._cfg.telegram.inbound_routes))
        cp = self._cfg.telegram.call_protocol
        log.info("offering call_protocol: layers=%s-%s versions=%s",
                 cp.get("min_layer"), cp.get("max_layer"),
                 list(cp.get("library_versions") or []))

        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._teardown()
            await self._tg_sig.stop()
            self._sip.stop()

    def _on_incoming_sip_threadsafe(self, ic: IncomingCall, call: SipCall) -> None:
        # Called on PJSIP worker thread. Hand off to asyncio loop.
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._on_incoming_sip(ic, call), self._loop)

    async def _on_incoming_sip(self, ic: IncomingCall, call: SipCall) -> None:
        async with self._lock:
            if self._state is not State.IDLE:
                log.info("rejecting sip call from %s — busy", ic.caller_id)
                call.reject(486)
                return

            self._sip_call = call
            self._state = State.SIP_RINGING
            log.info("sip→tg bridging starting, caller=%s", ic.caller_id)

            call.set_callbacks(
                on_state=self._on_sip_state,
                on_media=self._on_sip_media_threadsafe,
            )
            # 180 Ringing immediately; final accept after TG side answers.
            ringing = pj.CallOpParam(True)
            ringing.statusCode = 180
            try:
                call.answer(ringing)
            except pj.Error as e:
                log.warning("could not send 180 ringing: %s", e)

        await self._spawn_tg_call(ic.caller_id, ic.destination)

    def _pick_target(self, destination: str):
        """Choose the TG call target from the dialed SIP destination, else the
        configured fallback. '+<digits>' → phone, '@name' → username, plain
        digits → TG user id; anything else (e.g. the gateway's own SIP user) →
        the TG_FORWARD_USER_ID fallback."""
        d = (destination or "").strip()
        if re.fullmatch(r"\+\d{5,15}", d) or d.startswith("@") or re.fullmatch(r"\d{5,}", d):
            return int(d) if d.isdigit() else d
        return self._cfg.telegram.forward_user_id or None

    async def _spawn_tg_call(self, caller_id: str, destination: str = "") -> None:
        target = self._pick_target(destination)
        if not target:
            log.warning("no TG target for call (dest=%r, no fallback); rejecting", destination)
            await self._teardown()
            return
        try:
            uid, input_user = await self._tg_sig.resolve_target(target)
        except Exception as e:  # noqa: BLE001
            log.warning("cannot resolve TG target %r: %s", target, e)
            await self._teardown()
            return
        self._active_uid = uid
        log.info("routing call from %s → TG user %d (target=%r)", caller_id, uid, target)

        if self._cfg.behaviour.caller_id_in_tg_message:
            await self._tg_sig.send_text(uid, f"📞 Incoming call from {caller_id}")

        try:
            # 1. NTgCalls owns the call + DH; create it before requesting.
            self._playback = JitterBuffer(_playback_cap_bytes(self._cfg.bridge))
            self._tg_media = TelegramMedia(
                self._playback, self._loop,
                sample_rate=self._cfg.bridge.tg_sample_rate,
                video=self._cfg.video,
            )
            self._tg_media.set_state_callback(self._on_tg_conn_state_threadsafe)
            self._tg_media.set_signaling_sender(self._tg_sig.send_signaling_out)
            await self._tg_media.create_call(uid)

            # 2. Start the DH exchange → g_a_hash.
            g, p, rnd = await self._tg_sig.get_dh_config()
            g_a_hash = await self._tg_media.init_exchange(uid, g, p, rnd)
            protocol = self._cfg.telegram.call_protocol

            # 3. phone.requestCall (video flag set when an MJPEG source is configured).
            await self._tg_sig.request_call(
                input_user, g_a_hash, protocol, video=self._tg_media.video_enabled
            )
            async with self._lock:
                self._state = State.TG_CONFIRMING
            g_b = await self._tg_sig.wait_accepted(ANSWER_TIMEOUT_S)

            # 4. Derive the key and confirm; ntgcalls connects to the relays.
            auth = await self._tg_media.exchange_keys(uid, g_b, 0)
            connections, versions, p2p_allowed = await self._tg_sig.confirm_call(
                auth.g_a_or_b, auth.key_fingerprint, protocol
            )
            await self._tg_media.connect(uid, connections, versions, p2p_allowed)
            self._tg_media.start_tx_pump()
            self._tg_media.start_video_feeder()
        except CallDiscardedError as e:
            log.info("tg side discarded before connect: %s", e)
            await self._teardown()
            return
        except Exception as e:  # noqa: BLE001
            log.exception("tg call setup failed; tearing down: %s", e)
            await self._teardown()
            return

        # 5. TG media is up — answer SIP with 200 OK.
        try:
            self._sip_call.accept()
        except pj.Error as e:
            log.exception("sip accept failed: %s", e)
            await self._teardown()
            return

        async with self._lock:
            self._state = State.BRIDGED
        log.info("call bridged")

    # ---- TG→SIP direction (inbound Telegram call) ---------------------------

    async def _on_tg_incoming(self, incoming: IncomingTgCall) -> None:
        # Fired from the Pyrogram update handler. Decide quickly and hand the
        # (slow) setup to a task so we don't block signaling update dispatch.
        async with self._lock:
            if self._state is not State.IDLE:
                log.info("rejecting TG call from %s — busy", incoming.caller_id)
                await self._tg_sig.discard_incoming(incoming, busy=True)
                return
            self._state = State.TG_RINGING
        self._incoming_task = asyncio.create_task(self._spawn_sip_call(incoming))

    async def _pick_sip_dest(self, caller_id: int) -> Optional[str]:
        """Map an inbound TG caller to a SIP destination via inbound_routes. Keys
        may be a numeric TG user id, "@username", or "+<phone>" (resolved to a
        user id and cached). None = caller not in the table (declined)."""
        routes = self._cfg.telegram.inbound_routes
        if not routes:
            return None
        # 1. direct numeric user id
        if str(caller_id) in routes:
            return routes[str(caller_id)]
        # 2. @username
        uname = await self._tg_sig.username_of(caller_id)
        if uname and uname in routes:
            return routes[uname]
        # 3. +phone — we can't read the caller's number, so resolve each phone
        # *key* to its user id (cached; imports it as a contact) and compare.
        for key, dest in routes.items():
            if re.fullmatch(r"\+\d{5,15}", key):
                try:
                    uid, _ = await self._tg_sig.resolve_target(key)
                except Exception as e:  # noqa: BLE001
                    log.debug("inbound route phone %s unresolved: %s", key, e)
                    continue
                if uid == caller_id:
                    return dest
        return None

    def _sip_dest_uri(self, dest: str) -> str:
        """Turn a route value into a SIP URI: a bare extension → sip:<ext>@<domain>;
        a value already containing '@' or a sip: scheme is used as-is."""
        d = dest.strip()
        if d.startswith("sip:"):
            return d
        if "@" in d:
            return f"sip:{d}"
        return f"sip:{d}@{self._cfg.sip.domain}"

    async def _spawn_sip_call(self, incoming: IncomingTgCall) -> None:
        dest = await self._pick_sip_dest(incoming.caller_id)
        if not dest:
            log.info("rejecting TG call from %s — not in inbound_routes",
                     incoming.caller_id)
            await self._tg_sig.discard_incoming(incoming)
            async with self._lock:
                self._state = State.IDLE
            return
        dest_uri = self._sip_dest_uri(dest)
        log.info("routing TG call from %s → SIP %s", incoming.caller_id, dest_uri)

        # Commit: adopt the call so caller-cancel / signaling are tracked.
        self._tg_sig.bind_incoming(incoming)
        self._active_uid = incoming.caller_id
        try:
            await self._tg_sig.received_call()  # caller's UI shows "ringing"

            # Media objects. SIP audio bridges both ways; if a video source is
            # configured (e.g. a doorbell camera) we also send it to the TG
            # caller (the SIP phone has no camera, so this leg is one-way video).
            self._playback = JitterBuffer(_playback_cap_bytes(self._cfg.bridge))
            self._tg_media = TelegramMedia(
                self._playback, self._loop,
                sample_rate=self._cfg.bridge.tg_sample_rate, video=self._cfg.video,
            )
            self._tg_media.set_state_callback(self._on_tg_conn_state_threadsafe)
            self._tg_media.set_signaling_sender(self._tg_sig.send_signaling_out)
            await self._tg_media.create_call(incoming.caller_id)

            # Ring the SIP side and wait for it to answer.
            await self._dial_sip(dest_uri)

            # Phone answered → accept the TG call and finish the key exchange
            # as the callee (init_exchange with the caller's g_a_hash → g_b).
            g, p, rnd = await self._tg_sig.get_dh_config()
            g_b = await self._tg_media.init_exchange(
                incoming.caller_id, g, p, rnd, g_a_hash=incoming.g_a_hash)
            protocol = self._cfg.telegram.call_protocol
            await self._tg_sig.accept_call(g_b, protocol)
            est = await self._tg_sig.wait_established(ANSWER_TIMEOUT_S)

            await self._tg_media.exchange_keys(
                incoming.caller_id, est.g_a_or_b, est.key_fingerprint)
            await self._tg_media.connect(
                incoming.caller_id, est.connections,
                est.protocol.library_versions, est.p2p_allowed)
            self._tg_media.start_tx_pump()
            self._tg_media.start_video_feeder()  # doorbell camera → TG caller
        except CallDiscardedError as e:
            log.info("tg→sip call ended before bridge: %s", e)
            await self._teardown()
            return
        except Exception as e:  # noqa: BLE001
            log.exception("tg→sip setup failed; tearing down: %s", e)
            await self._teardown()
            return

        async with self._lock:
            self._state = State.BRIDGED
        log.info("call bridged (tg→sip)")

    async def _dial_sip(self, dest_uri: str) -> None:
        """Place the outbound SIP call and block until answered (CONFIRMED).
        Raises CallDiscardedError if it fails / times out."""
        answered: asyncio.Future = self._loop.create_future()

        def on_state(state_text: str) -> None:  # PJSIP thread
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._on_outbound_sip_state(state_text, answered), self._loop)

        self._sip_call = self._sip.make_call(
            dest_uri, on_state=on_state, on_media=self._on_sip_media_threadsafe)
        try:
            await asyncio.wait_for(answered, timeout=SIP_ANSWER_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise CallDiscardedError("sip answer timeout")

    async def _on_outbound_sip_state(self, state_text: str,
                                     answered: asyncio.Future) -> None:
        # On the loop, so it's safe to touch the future.
        if state_text == "CONFIRMED":
            if not answered.done():
                answered.set_result(True)
        elif state_text == "DISCONNECTED":
            if not answered.done():
                answered.set_exception(CallDiscardedError("sip side did not answer"))
            else:
                await self._teardown()  # far end hung up a live call

    def _on_sip_media_threadsafe(self, am: pj.AudioMedia) -> None:
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._wire_sip_media(am), self._loop)

    async def _wire_sip_media(self, am: pj.AudioMedia) -> None:
        if self._sip_port is not None or self._tg_media is None or self._playback is None:
            return
        self._sip_port = self._sip.make_bridge_port(
            sample_rate=self._cfg.bridge.tg_sample_rate,
            frame_ms=self._cfg.bridge.frame_ms,
            on_capture=self._tg_media.push_capture,
            pull_playback=self._playback.pull,
        )
        am.startTransmit(self._sip_port)
        self._sip_port.startTransmit(am)
        log.info("sip audio wired to ntgcalls bridge @%dHz", self._cfg.bridge.tg_sample_rate)

    def _on_sip_state(self, state_text: str) -> None:
        log.debug("sip state: %s", state_text)
        if state_text == "DISCONNECTED":
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._teardown(), self._loop)

    def _on_tg_conn_state_threadsafe(self, state_name: str) -> None:
        # Fired on an ntgcalls thread. Only act on terminal failure states.
        if not self._loop:
            return
        if any(tok in state_name.upper() for tok in _FAILED_STATES):
            log.warning("ntgcalls media leg failed (state=%s); tearing down", state_name)
            asyncio.run_coroutine_threadsafe(self._teardown(), self._loop)

    async def _on_tg_signaling_in(self, data: bytes) -> None:
        if self._tg_media is not None and self._active_uid is not None:
            try:
                await self._tg_media.feed_signaling(self._active_uid, data)
            except Exception as e:  # noqa: BLE001
                log.debug("feed_signaling failed: %s", e)

    async def _on_tg_remote_hangup(self) -> None:
        log.info("tg remote hung up")
        await self._teardown()

    async def _teardown(self) -> None:
        async with self._lock:
            if self._state in (State.IDLE, State.TEARDOWN):
                return
            self._state = State.TEARDOWN

        log.info("teardown starting")
        if self._tg_media:
            await self._tg_media.stop()
            self._tg_media = None
        await self._tg_sig.discard_call()
        if self._playback:
            self._playback.clear()
            self._playback = None
        if self._sip_call:
            try:
                self._sip_call.end()
            except Exception:  # noqa: BLE001
                pass
            self._sip_call = None
        self._sip_port = None
        self._active_uid = None

        async with self._lock:
            self._state = State.IDLE
        log.info("idle")


def _playback_cap_bytes(bridge_cfg) -> int:
    """Cap the TG→SIP jitter buffer to bound latency (s16le mono)."""
    cap_ms = max(bridge_cfg.jitter_ms * 4, 200)
    return bridge_cfg.tg_sample_rate * 2 * cap_ms // 1000
