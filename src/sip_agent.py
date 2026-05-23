"""PJSUA2 wrapper.

Registers as a SIP UA. On incoming INVITE, calls back into the gateway via
the `on_incoming_call` callback. The gateway decides whether to accept; if so,
it calls `answer()` and attaches a media tap.

The media tap exposes the call's audio as a bidirectional PCM stream at
`sip_sample_rate` (typically 8 kHz). PJSUA2 internally resamples between the
negotiated codec (e.g. PCMA 8 k) and our tap port.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

import pjsua2 as pj  # type: ignore[import-not-found]

from .config import SipConfig

log = logging.getLogger(__name__)


def _frame_to_bytes(frame: pj.MediaFrame) -> bytes:
    """Read raw PCM out of a PJSUA2 MediaFrame's ByteVector (vector<unsigned
    char> — elements are ints 0..255)."""
    buf = frame.buf
    try:
        return bytes(buf)
    except (TypeError, ValueError):
        return bytes(bytearray(int(b) & 0xFF for b in buf))


def _bytes_to_frame(frame: pj.MediaFrame, data: bytes) -> None:
    """Fill a PJSUA2 MediaFrame's ByteVector (vector<unsigned char>) with PCM.

    get_frame() copies min(buf.size(), requested) bytes from buf.data(), so the
    vector must actually contain the bytes and frame.type must be AUDIO.
    """
    try:
        frame.buf = pj.ByteVector(data)
    except (TypeError, ValueError):
        try:
            frame.buf = pj.ByteVector(list(data))
        except (TypeError, ValueError):
            vec = pj.ByteVector()
            for b in data:
                vec.append(int(b))  # 0..255 for unsigned char
            frame.buf = vec
    frame.size = len(data)


class BridgeAudioPort(pj.AudioMediaPort):
    """In-process audio tap connected to a SIP call's AudioMedia.

    ``onFrameReceived`` — PCM arriving from the SIP caller (→ Telegram).
    ``onFrameRequested`` — PCM to play back to the caller (← Telegram).
    Both fire on PJSIP's media thread; the supplied callbacks must be cheap and
    thread-safe (see :class:`~src.audio_bridge.JitterBuffer`).
    """

    def __init__(
        self,
        on_capture: Callable[[bytes], None],
        pull_playback: Callable[[int], bytes],
        bytes_per_frame: int,
    ):
        super().__init__()
        self._on_capture = on_capture
        self._pull_playback = pull_playback
        self._bpf = bytes_per_frame
        self.rx_frames = 0
        self.tx_frames = 0

    def onFrameReceived(self, frame: pj.MediaFrame) -> None:  # noqa: N802 - pjsua2 API
        self.rx_frames += 1
        try:
            data = _frame_to_bytes(frame)
        except Exception as e:  # noqa: BLE001
            if self.rx_frames == 1:
                log.warning("bridge: onFrameReceived decode error: %s", e)
            return
        if self.rx_frames == 1:
            log.debug("bridge: first SIP→port frame received (%d bytes)", len(data))
        elif self.rx_frames % 500 == 0:
            log.debug("bridge sip→tg frames=%d", self.rx_frames)
        if data:
            self._on_capture(data)

    def onFrameRequested(self, frame: pj.MediaFrame) -> None:  # noqa: N802 - pjsua2 API
        self.tx_frames += 1
        try:
            n = frame.size or self._bpf  # honor the bridge's requested size
            data = self._pull_playback(n)
            frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
            _bytes_to_frame(frame, data)
        except Exception as e:  # noqa: BLE001
            if self.tx_frames <= 2:
                log.warning("bridge: onFrameRequested error: %s", e)
            return
        if self.tx_frames == 1:
            log.debug("bridge: first port→SIP frame requested (size=%d, gave=%d)",
                      frame.size, len(data))
        elif self.tx_frames % 500 == 0:
            log.debug("bridge tg→sip frames=%d", self.tx_frames)


@dataclass
class IncomingCall:
    call_id: int
    remote_uri: str
    caller_id: str


class _Endpoint(pj.Endpoint):
    pass


class _Account(pj.Account):
    def __init__(self, on_incoming: Callable[[IncomingCall, "SipCall"], None]):
        super().__init__()
        self._on_incoming = on_incoming

    def onRegState(self, prm: pj.OnRegStateParam) -> None:  # noqa: N802 - pjsua2 API
        log.info("sip registration status=%s code=%s", prm.code, prm.reason)

    def onIncomingCall(self, prm: pj.OnIncomingCallParam) -> None:  # noqa: N802
        call = SipCall(self, prm.callId)
        info = call.getInfo()
        ic = IncomingCall(
            call_id=prm.callId,
            remote_uri=info.remoteUri,
            caller_id=_extract_user(info.remoteUri),
        )
        log.info("incoming sip call from %s", ic.caller_id)
        self._on_incoming(ic, call)


class SipCall(pj.Call):
    """A single SIP call. Exposes media tap once connected."""

    def __init__(self, acc: pj.Account, call_id: int = pj.PJSUA_INVALID_ID):
        super().__init__(acc, call_id)
        self._on_state: Optional[Callable[[str], None]] = None
        self._on_media: Optional[Callable[[pj.AudioMedia], None]] = None

    def set_callbacks(
        self,
        on_state: Callable[[str], None],
        on_media: Callable[[pj.AudioMedia], None],
    ) -> None:
        self._on_state = on_state
        self._on_media = on_media

    def onCallState(self, prm: pj.OnCallStateParam) -> None:  # noqa: N802
        info = self.getInfo()
        state = info.stateText
        log.debug("sip call state %s", state)
        if self._on_state:
            self._on_state(state)

    def onCallMediaState(self, prm: pj.OnCallMediaStateParam) -> None:  # noqa: N802
        info = self.getInfo()
        for i in range(info.media.size()):
            mi = info.media[i]
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                am = self.getAudioMedia(i)
                log.info("sip media active on slot=%d", i)
                if self._on_media:
                    self._on_media(am)

    def accept(self) -> None:
        prm = pj.CallOpParam(True)
        prm.statusCode = 200
        self.answer(prm)

    def reject(self, code: int = 486) -> None:
        prm = pj.CallOpParam(True)
        prm.statusCode = code
        self.hangup(prm)

    def end(self) -> None:
        if self.isActive():
            prm = pj.CallOpParam(True)
            prm.statusCode = 200
            self.hangup(prm)


class SipAgent:
    """Owns the PJSUA2 endpoint. Single instance per process."""

    def __init__(self, cfg: SipConfig, on_incoming_call: Callable[[IncomingCall, SipCall], None]):
        self._cfg = cfg
        self._on_incoming = on_incoming_call
        self._ep = _Endpoint()
        self._acc: Optional[_Account] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._ep.libCreate()

        ep_cfg = pj.EpConfig()
        ep_cfg.uaConfig.userAgent = "tg2sip/0.1"
        ep_cfg.logConfig.level = 3
        ep_cfg.medConfig.clockRate = 48000
        ep_cfg.medConfig.sndClockRate = 0  # no sound device
        ep_cfg.medConfig.noVad = True
        ep_cfg.medConfig.ecTailLen = 0
        self._ep.libInit(ep_cfg)

        tp_cfg = pj.TransportConfig()
        tp_cfg.port = self._cfg.local_port
        transport_type = (
            pj.PJSIP_TRANSPORT_TCP if self._cfg.transport.lower() == "tcp"
            else pj.PJSIP_TRANSPORT_UDP
        )
        self._ep.transportCreate(transport_type, tp_cfg)

        self._ep.libStart()
        # Headless: no sound card. A null device gives the conference bridge a
        # master clock so custom ports get polled (onFrameRequested/Received);
        # without it startTransmit fails with PJMEDIA_EAUD_NODEFDEV and no audio
        # is ever pulled toward the caller.
        self._ep.audDevManager().setNullDev()
        log.info("pjsua2 started on port %d/%s (null audio device)",
                 self._cfg.local_port, self._cfg.transport)

        for codec, prio in self._cfg.codec_priorities.items():
            try:
                self._ep.codecSetPriority(codec, prio)
            except pj.Error as e:
                log.warning("codec %s not available: %s", codec, e)

        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = f"sip:{self._cfg.username}@{self._cfg.domain}"
        acc_cfg.regConfig.registrarUri = f"sip:{self._cfg.registrar}"
        cred = pj.AuthCredInfo("digest", "*", self._cfg.username, 0, self._cfg.password)
        acc_cfg.sipConfig.authCreds.append(cred)

        self._acc = _Account(self._on_incoming)
        self._acc.create(acc_cfg)
        log.info("registering as %s", acc_cfg.idUri)

    def stop(self) -> None:
        with self._lock:
            if self._acc:
                self._acc.shutdown()
                self._acc = None
            try:
                self._ep.libDestroy()
            except Exception:  # noqa: BLE001
                pass

    def make_bridge_port(
        self,
        sample_rate: int,
        frame_ms: int,
        on_capture: Callable[[bytes], None],
        pull_playback: Callable[[int], bytes],
        channels: int = 1,
    ) -> BridgeAudioPort:
        """Create the in-process audio port that bridges a SIP call to NTgCalls.

        Connect it to the call's AudioMedia with `am.startTransmit(port)` and
        `port.startTransmit(am)`. The conference bridge resamples between the
        negotiated codec and `sample_rate`, so pick the rate NTgCalls expects
        (48 kHz) to avoid a second resampling step.
        """
        fmt = pj.MediaFormatAudio()
        fmt.type = pj.PJMEDIA_TYPE_AUDIO
        fmt.clockRate = sample_rate
        fmt.channelCount = channels
        fmt.bitsPerSample = 16
        fmt.frameTimeUsec = frame_ms * 1000

        bytes_per_frame = sample_rate * frame_ms // 1000 * 2 * channels
        port = BridgeAudioPort(on_capture, pull_playback, bytes_per_frame)
        port.createPort("tg2sip_bridge", fmt)
        return port


def _extract_user(sip_uri: str) -> str:
    # sip:1234@host or "Name" <sip:1234@host>
    try:
        body = sip_uri.split("<", 1)[-1].rstrip(">")
        userpart = body.split("sip:", 1)[-1].split("@", 1)[0]
        return userpart or sip_uri
    except Exception:  # noqa: BLE001
        return sip_uri
