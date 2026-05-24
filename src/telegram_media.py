"""Telegram P2P call media layer (NTgCalls 1.3.4).

NTgCalls speaks Telegram's encrypted UDP voice protocol. Crucially it *owns*
the Diffie-Hellman key exchange — it generates the secret, holds the call key
for SRTP, and only hands us the public material to shuttle over MTProto:

    await media.create_call(user_id)                  # register call + media
    g_a_hash = await media.init_exchange(...)          # → phone.requestCall
    ... wait for phoneCallAccepted (g_b) ...
    auth = await media.exchange_keys(user_id, g_b, 0)  # → phone.confirmCall
    ... confirmCall returns the relay connections ...
    await media.connect(user_id, connections, versions, p2p_allowed)

Every NTgCalls *action* method returns an asyncio Future and must be awaited
(only ``get_protocol``, the constructors and the ``on_*`` registrations are
sync). Audio uses NTgCalls' EXTERNAL source/sink at 48 kHz mono s16le: caller
PCM is pushed with ``send_external_frame`` and remote PCM arrives via the
``on_frames`` callback. No files/FIFOs are involved.

``send_external_frame`` is async but capture frames originate on PJSIP's media
thread, so they're funnelled through an asyncio.Queue drained by a task on the
event loop. The flow mirrors pytgcalls' own ``ConnectCall`` against this API
version (ntgcalls v1.3.4 ``pythonapi.cpp``).
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
import threading
import time
from typing import Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

try:
    import ntgcalls  # type: ignore[import-not-found]
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "ntgcalls is not installed in the runtime image. "
        "Verify Dockerfile installs the pinned `ntgcalls` wheel."
    ) from e

from .audio_bridge import JitterBuffer

log = logging.getLogger(__name__)


class TelegramMedia:
    def __init__(
        self,
        playback: JitterBuffer,
        loop: asyncio.AbstractEventLoop,
        sample_rate: int = 48000,
        channels: int = 1,
        video=None,
    ):
        self._ntg = ntgcalls.NTgCalls()
        self._playback = playback
        self._loop = loop
        self._sr = sample_rate
        self._ch = channels
        self._video = video  # config.VideoConfig or None
        if video is not None and video.enabled and not video.h264:
            # Drop the H264 software encoder (process-wide, before any connection
            # is built) to dodge a SIMD/illegal-instruction crash in the prebuilt
            # codecs on older CPUs (no AVX2). NOTE: removed in ntgcalls 2.x —
            # there H264 is always offered and can't be disabled via the API.
            if hasattr(ntgcalls.NTgCalls, "enable_h264_encoder"):
                ntgcalls.NTgCalls.enable_h264_encoder(False)
                log.info("ntgcalls H264 encoder disabled (VIDEO_H264=0)")
            else:
                log.warning("VIDEO_H264=0 ignored: this ntgcalls (%s) has no H264 "
                            "toggle — H264 is always offered",
                            getattr(ntgcalls, "__version__", "?"))
        self._user_id: Optional[int] = None
        # ntgcalls EXTERNAL audio is fed in 10 ms frames.
        self._frame_bytes = sample_rate // 100 * 2 * channels
        self._capture_buf = bytearray()
        self._rx_frames = 0
        self._on_state: Optional[Callable[[str], None]] = None
        self._sig_sender = None
        self._tx_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._tx_task: Optional[asyncio.Task] = None
        # Outgoing video: we run ffmpeg ourselves and push EXTERNAL frames (the
        # SHELL source deadlocks against EXTERNAL audio in ntgcalls' A/V sync).
        self._video_proc: Optional[subprocess.Popen] = None
        self._video_thread: Optional[threading.Thread] = None
        self._video_queue: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._video_task: Optional[asyncio.Task] = None
        self._video_send_fails = 0
        # Signaling is queued (ordered) in both directions. Incoming blobs that
        # arrive before connect_p2p creates the signaling object are held here
        # and replayed once the pumps start (avoids losing the peer's ICE setup).
        self._sig_out_queue: asyncio.Queue = asyncio.Queue()
        self._sig_in_queue: asyncio.Queue = asyncio.Queue()
        self._sig_out_task: Optional[asyncio.Task] = None
        self._sig_in_task: Optional[asyncio.Task] = None

        self._ntg.on_frames(self._on_frames)
        self._ntg.on_connection_change(self._on_connection_change)
        self._ntg.on_signaling(self._on_signaling)

    def set_state_callback(self, cb: Callable[[str], None]) -> None:
        """cb(state_name) — fired (on an ntgcalls thread) on connection change."""
        self._on_state = cb

    def set_signaling_sender(self, cb) -> None:
        """cb(user_id, data) — async coroutine; forwards ntgcalls' outgoing
        signaling blobs to Telegram (phone.sendSignalingData)."""
        self._sig_sender = cb

    async def feed_signaling(self, user_id: int, data: bytes) -> None:
        """Queue an incoming updatePhoneCallSignalingData blob for ntgcalls.

        Queued (not sent directly) so blobs arriving before connect_p2p has
        created the signaling object aren't dropped — the pump replays them in
        order once it starts.
        """
        self._sig_in_queue.put_nowait(bytes(data))

    # ---- call setup (async ntgcalls calls; run from the orchestrator) -------

    def get_protocol(self) -> "ntgcalls.Protocol":
        """The library's own protocol descriptor (layers + versions). Sync."""
        return self._ntg.get_protocol()

    async def create_call(self, user_id: int) -> None:
        self._user_id = user_id
        # ntgcalls 2.x: create_p2p_call takes no media; capture sources are set
        # separately (1.3.4 took the media directly in create_p2p_call).
        await self._ntg.create_p2p_call(user_id)
        await self._ntg.set_stream_sources(
            user_id, ntgcalls.StreamMode.CAPTURE, self._capture_media()
        )

    async def init_exchange(self, user_id: int, g: int, p: bytes, random: bytes,
                            g_a_hash: Optional[bytes] = None) -> bytes:
        """DH start. Outgoing (g_a_hash=None): returns our g_a_hash for
        phone.requestCall. Incoming (g_a_hash=caller's): returns our g_b for
        phone.acceptCall."""
        dh = ntgcalls.DhConfig(g, p, random)
        return await self._ntg.init_exchange(user_id, dh, g_a_hash)

    async def exchange_keys(self, user_id: int, g_b: bytes, fingerprint: int) -> "ntgcalls.AuthParams":
        """Derive the call key from the peer's g_b. Returns AuthParams whose
        ``g_a_or_b`` (our g_a) and ``key_fingerprint`` go into phone.confirmCall."""
        return await self._ntg.exchange_keys(user_id, g_b, fingerprint)

    async def connect(self, user_id: int, connections, versions, p2p_allowed: bool) -> None:
        servers = _build_servers(connections)
        await self._ntg.connect_p2p(user_id, servers, list(versions), p2p_allowed)
        # create_p2p_call only wired the capture (microphone) side; register the
        # PLAYBACK source so remote audio is delivered as EXTERNAL frames via
        # the on_frames callback (otherwise the SIP side only ever hears silence).
        await self._ntg.set_stream_sources(
            user_id, ntgcalls.StreamMode.PLAYBACK, self._playback_media()
        )
        # Signaling object now exists; start ordered relay of both directions.
        # Any incoming blobs received earlier are still queued and get replayed.
        self._sig_out_task = asyncio.create_task(self._sig_out_pump())
        self._sig_in_task = asyncio.create_task(self._sig_in_pump())
        log.info("ntgcalls connect_p2p done (%d servers); playback source set", len(servers))

    # ---- audio --------------------------------------------------------------

    def start_tx_pump(self) -> None:
        """Begin draining captured frames to ntgcalls. Call once connected."""
        if self._tx_task is None:
            self._tx_task = asyncio.create_task(self._tx_pump())

    def push_capture(self, pcm: bytes) -> None:
        """Feed caller PCM (48 kHz mono s16le) toward Telegram, in 10 ms frames.

        Runs on PJSIP's media thread, so it only hands work to the loop.
        """
        if self._user_id is None:
            return
        buf = self._capture_buf
        buf.extend(pcm)
        n = self._frame_bytes
        while len(buf) >= n:
            chunk = bytes(buf[:n])
            del buf[:n]
            self._loop.call_soon_threadsafe(self._enqueue_tx, chunk)

    def _enqueue_tx(self, chunk: bytes) -> None:
        # On the loop thread. Drop the oldest frame rather than block on overrun.
        try:
            self._tx_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                self._tx_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._tx_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

    async def _tx_pump(self) -> None:
        frame_data = ntgcalls.FrameData(0, 0, 0, 0)
        sent = 0
        while True:
            chunk = await self._tx_queue.get()
            if chunk is None or self._user_id is None:
                if chunk is None:
                    break
                continue
            try:
                await self._ntg.send_external_frame(
                    self._user_id,
                    ntgcalls.StreamDevice.MICROPHONE,
                    chunk,
                    frame_data,
                )
                sent += 1
                if sent == 1:
                    log.debug("bridge: first frame sent to ntgcalls (%d bytes)", len(chunk))
                elif sent % 500 == 0:
                    log.debug("bridge sip→tg frames sent=%d", sent)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("send_external_frame failed: %s", e)

    def _on_frames(self, chat_id, mode, device, frames) -> None:
        # Fires on an ntgcalls worker thread. Only playback (remote audio) is
        # routed to the SIP side; capture echoes are ignored.
        if mode != ntgcalls.StreamMode.PLAYBACK:
            return
        for f in frames:
            data = f.data
            if data:
                self._rx_frames += 1
                if self._rx_frames == 1:
                    log.debug("bridge: first frame received from ntgcalls (%d bytes)", len(data))
                elif self._rx_frames % 500 == 0:
                    log.debug("bridge tg→sip frames recv=%d (%d bytes)",
                              self._rx_frames, len(data))
                self._playback.push(bytes(data))

    def _on_signaling(self, chat_id, data) -> None:
        # Fires on an ntgcalls thread. Enqueue (ordered) for the out pump.
        self._loop.call_soon_threadsafe(self._sig_out_queue.put_nowait, bytes(data))

    async def _sig_out_pump(self) -> None:
        n = 0
        while True:
            data = await self._sig_out_queue.get()
            if data is None:
                break
            n += 1
            if n == 1:
                log.debug("signaling: first outgoing blob → Telegram (%d bytes)", len(data))
            if self._sig_sender is not None and self._user_id is not None:
                try:
                    await self._sig_sender(self._user_id, data)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.debug("signaling out failed: %s", e)

    async def _sig_in_pump(self) -> None:
        n = 0
        while True:
            data = await self._sig_in_queue.get()
            if data is None:
                break
            n += 1
            if n == 1:
                log.debug("signaling: first incoming blob → ntgcalls (%d bytes)", len(data))
            if self._user_id is not None:
                try:
                    await self._ntg.send_signaling(self._user_id, data)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.debug("signaling in failed: %s", e)

    def _on_connection_change(self, chat_id, net_info) -> None:
        state = getattr(net_info, "state", net_info)
        name = getattr(state, "name", str(state))
        log.info("ntgcalls connection state=%s", name)
        if self._on_state:
            self._on_state(name)

    def _audio_external(self) -> "ntgcalls.AudioDescription":
        # NOTE the 1.3.4 arg order: (media_source, sample_rate, channel_count, input).
        return ntgcalls.AudioDescription(
            media_source=ntgcalls.MediaSource.EXTERNAL,
            sample_rate=self._sr,
            channel_count=self._ch,
            input="",
        )

    def _capture_media(self) -> "ntgcalls.MediaDescription":
        # microphone = audio we send (send_external_frame). camera = outgoing
        # video, also EXTERNAL: we run ffmpeg ourselves and push frames. (A SHELL
        # video source deadlocks against the EXTERNAL audio in ntgcalls' capture
        # A/V sync, so both legs must be EXTERNAL.)
        if self.video_enabled:
            video = ntgcalls.VideoDescription(
                media_source=ntgcalls.MediaSource.EXTERNAL,
                width=self._video.width,
                height=self._video.height,
                fps=self._video.fps,
                input="",
            )
            return ntgcalls.MediaDescription(
                microphone=self._audio_external(), camera=video,
            )
        return ntgcalls.MediaDescription(microphone=self._audio_external())

    @property
    def video_enabled(self) -> bool:
        return bool(self._video and self._video.enabled)

    def _ffmpeg_args(self) -> list:
        """Argv for the ffmpeg we run ourselves (subprocess, no shell) to decode
        the source into raw yuv420p frames on stdout."""
        w, h, fps = self._video.width, self._video.height, self._video.fps
        if self._video.source_cmd:
            log.info("video source: custom command (%dx%d@%dfps)", w, h, fps)
            return ["sh", "-c", self._video.source_cmd]
        url = _with_basic_auth(
            self._video.source_url, self._video.source_user, self._video.source_pass
        )
        ff_level = "info" if log.isEnabledFor(logging.DEBUG) else "error"
        args = ["ffmpeg", "-nostdin", "-v", ff_level]
        if url.startswith(("http://", "https://")):
            args += ["-reconnect", "1", "-reconnect_at_eof", "1",
                     "-reconnect_streamed", "1", "-reconnect_delay_max", "2"]
        if self._video.input_args:
            args += shlex.split(self._video.input_args)
        args += ["-i", url, "-f", "rawvideo", "-r", str(fps),
                 "-pix_fmt", "yuv420p", "-vf", f"scale={w}:{h}", "pipe:1"]
        log.info("video source: ffmpeg %dx%d@%dfps from %s", w, h, fps, _redact_url(url))
        return args

    def start_video_feeder(self) -> None:
        """Spawn ffmpeg and push its raw frames to ntgcalls as EXTERNAL camera
        frames. Call once connected."""
        if not self.video_enabled or self._video_proc is not None:
            return
        try:
            self._video_proc = subprocess.Popen(
                self._ffmpeg_args(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not start video ffmpeg: %s", e)
            return
        self._video_thread = threading.Thread(target=self._video_reader, daemon=True)
        self._video_thread.start()
        self._video_task = asyncio.create_task(self._video_pump())

    def _video_reader(self) -> None:
        # Thread: read whole yuv420p frames from ffmpeg (blocking read paces us
        # to ffmpeg's -r fps) and hand each to the loop. A pipe read() can return
        # a partial frame, so accumulate until a full frame is in hand.
        frame_size = self._video.width * self._video.height * 3 // 2
        stdout = self._video_proc.stdout if self._video_proc else None
        if stdout is None:
            return
        read = 0
        try:
            while True:
                buf = bytearray()
                while len(buf) < frame_size:
                    chunk = stdout.read(frame_size - len(buf))
                    if not chunk:
                        rc = self._video_proc.poll() if self._video_proc else None
                        log.warning("video: ffmpeg output ended after %d frames (exit=%s)",
                                    read, rc)
                        return  # ffmpeg exited / EOF
                    buf.extend(chunk)
                read += 1
                self._loop.call_soon_threadsafe(self._enqueue_video, bytes(buf))
        except Exception as e:  # noqa: BLE001
            log.warning("video reader error after %d frames: %s", read, e)

    def _enqueue_video(self, frame: bytes) -> None:
        try:
            self._video_queue.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                self._video_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._video_queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    async def _video_pump(self) -> None:
        w, h = self._video.width, self._video.height
        fps = max(self._video.fps, 1)
        interval = 1.0 / fps
        next_send = time.monotonic()
        last_ms = 0
        sent = 0
        while True:
            frame = await self._video_queue.get()
            if frame is None or self._user_id is None:
                if frame is None:
                    break
                continue
            # Pace to ~fps so a startup burst doesn't bunch frames together.
            now = time.monotonic()
            if now < next_send:
                await asyncio.sleep(next_send - now)
            next_send = max(next_send + interval, time.monotonic())
            # Strictly-increasing capture timestamp (WebRTC drops dup/old NTP).
            ms = int(time.monotonic() * 1000)
            if ms <= last_ms:
                ms = last_ms + 1
            last_ms = ms
            try:
                await self._ntg.send_external_frame(
                    self._user_id, ntgcalls.StreamDevice.CAMERA,
                    frame, ntgcalls.FrameData(ms, 0, w, h),
                )
                sent += 1
                if sent == 1:
                    log.debug("video: first frame pushed to ntgcalls (%d bytes)", len(frame))
                elif sent % 300 == 0:
                    log.debug("video frames pushed=%d", sent)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._video_send_fails += 1
                if self._video_send_fails <= 3:
                    log.warning("send_external_frame(camera) failed (#%d): %s",
                                self._video_send_fails, e)

    def _playback_media(self) -> "ntgcalls.MediaDescription":
        # Incoming audio is routed to the Playback *Microphone* device (the call
        # adds addTrack(Playback, Microphone) and optimizeSources only enables
        # incoming audio when Microphone is an external writer), so the playback
        # sink must be set on `microphone`, not `speaker`, to receive on_frames.
        return ntgcalls.MediaDescription(microphone=self._audio_external())

    async def stop(self) -> None:
        proc = self._video_proc
        self._video_proc = None  # reader thread (daemon) exits on EOF
        if proc is not None:
            # Force ffmpeg down and reap it off-thread (don't block the loop). A
            # stuck ffmpeg that ignores SIGTERM would keep the camera's RTSP
            # session open, so the next call's ffmpeg can't connect (exit 187).
            threading.Thread(target=_kill_proc, args=(proc,), daemon=True).start()
        for task_attr in ("_tx_task", "_sig_out_task", "_sig_in_task", "_video_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, task_attr, None)
        if self._user_id is None:
            return
        try:
            await self._ntg.stop(self._user_id)
        except Exception as e:  # noqa: BLE001
            log.warning("ntgcalls stop failed: %s", e)
        finally:
            self._user_id = None


def _kill_proc(proc) -> None:
    """Terminate then (if needed) kill an ffmpeg subprocess and reap it. Run in a
    daemon thread so the blocking waits don't stall the event loop."""
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _with_basic_auth(url: str, user: str, password: str) -> str:
    """Embed URL-encoded credentials in the URL's userinfo (ffmpeg uses these
    for HTTP/RTSP Basic/Digest auth). No-op when no user is set or the scheme
    has no host (e.g. a local file path)."""
    if not user:
        return url
    parts = urlsplit(url)
    if not parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[-1]  # drop any existing userinfo
    userinfo = quote(user, safe="")
    if password:
        userinfo += ":" + quote(password, safe="")
    return urlunsplit((parts.scheme, f"{userinfo}@{host}",
                       parts.path, parts.query, parts.fragment))


def _redact_url(url: str) -> str:
    """Hide any userinfo (credentials) before logging a URL."""
    parts = urlsplit(url)
    if "@" in parts.netloc:
        host = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parts.scheme, f"***@{host}",
                           parts.path, parts.query, parts.fragment))
    return url


def _build_servers(connections) -> list:
    """Convert Telegram TL phoneConnection(s) to ntgcalls RTCServer list.

    RTCServer(id, ipv4, ipv6, port, username, password, turn, stun, tcp, peer_tag)
    — mirrors pytgcalls' BridgedClient.parse_servers for this API version.
    """
    servers = []
    for c in connections:
        if type(c).__name__ == "PhoneConnectionWebrtc":
            servers.append(
                ntgcalls.RTCServer(
                    c.id, c.ip, c.ipv6, c.port,
                    c.username, c.password,
                    c.turn, c.stun, False, None,
                )
            )
        else:  # PhoneConnection — legacy UDP reflector
            servers.append(
                ntgcalls.RTCServer(
                    c.id, c.ip, c.ipv6, c.port,
                    None, None,
                    True, False, c.tcp, c.peer_tag,
                )
            )
    return servers
