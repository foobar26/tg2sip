"""Thread-safe PCM bridge between ntgcalls' media thread and PJSUA2's media
thread.

Both legs run at 48 kHz mono s16le:

  * PJSUA2's conference bridge is configured at 48 kHz (``medConfig.clockRate``)
    and transparently resamples the SIP codec (PCMA/PCMU 8 kHz) to/from our
    custom :class:`~src.sip_agent.BridgeAudioPort`.
  * ntgcalls is told the external audio is 48 kHz mono, so no manual resampling
    happens here — only buffering across threads.

Direction handling:

  * SIP → Telegram is push-through: ``BridgeAudioPort.onFrameReceived`` hands the
    caller's PCM straight to ``TelegramMedia.push_capture`` (no buffer needed).
  * Telegram → SIP is decoupled by :class:`JitterBuffer`: ntgcalls delivers
    decoded playback frames on its own worker thread (``on_frames``) while
    PJSUA2 pulls fixed-size frames on its media thread (``onFrameRequested``).
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class JitterBuffer:
    """Bounded FIFO of raw PCM bytes, safe for one producer + one consumer on
    different threads. Caps total latency by dropping the oldest audio, and
    pads with silence on underrun so PJSUA2 always gets a full frame."""

    def __init__(self, max_bytes: int):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._max = max(max_bytes, 0)
        # diagnostics (read without locking; approximate is fine)
        self.pushed = 0
        self.pulled = 0
        self.underruns = 0
        self.dropped = 0

    def push(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._buf.extend(data)
            self.pushed += len(data)
            overflow = len(self._buf) - self._max
            if self._max and overflow > 0:
                del self._buf[:overflow]
                self.dropped += overflow

    def pull(self, n: int) -> bytes:
        with self._lock:
            have = len(self._buf)
            if have >= n:
                out = bytes(self._buf[:n])
                del self._buf[:n]
                self.pulled += n
                return out
            out = bytes(self._buf) + b"\x00" * (n - have)
            self._buf.clear()
            self.underruns += 1
            self.pulled += have
            return out

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
