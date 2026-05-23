from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class SipConfig:
    username: str
    password: str
    domain: str
    registrar: str
    transport: str
    local_port: int
    rtp_port_range: tuple[int, int]
    codec_priorities: dict[str, int]


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    forward_user_id: int
    session_name: str
    session_dir: Path
    call_protocol: dict


@dataclass(frozen=True)
class BridgeConfig:
    sip_sample_rate: int
    tg_sample_rate: int
    frame_ms: int
    jitter_ms: int


@dataclass(frozen=True)
class BehaviourConfig:
    reject_if_busy: bool
    hangup_on_tg_decline: bool
    caller_id_in_tg_message: bool


@dataclass(frozen=True)
class VideoConfig:
    enabled: bool       # true when a source URL or command is configured
    source_url: str     # ffmpeg input (MJPEG/RTSP/http stream) for the TG camera
    source_cmd: str     # full shell command override; must write raw yuv420p to stdout
    source_user: str    # optional HTTP/RTSP basic-auth username
    source_pass: str    # optional HTTP/RTSP basic-auth password
    input_args: str     # extra ffmpeg input options before -i (e.g. "-f mjpeg")
    width: int
    height: int
    fps: int
    h264: bool          # offer the H264 encoder (set false to dodge a SIMD crash)


@dataclass(frozen=True)
class Config:
    sip: SipConfig
    telegram: TelegramConfig
    bridge: BridgeConfig
    behaviour: BehaviourConfig
    video: VideoConfig
    log_level: str


def load() -> Config:
    load_dotenv()
    cfg_path = Path(os.environ.get("CONFIG_PATH", "config/config.yaml"))
    with cfg_path.open() as f:
        raw = yaml.safe_load(f)

    session_dir = Path(os.environ.get("SESSION_DIR", "sessions"))
    session_dir.mkdir(parents=True, exist_ok=True)

    sip_raw = raw["sip"]
    sip = SipConfig(
        username=_req_env("SIP_USERNAME"),
        password=_req_env("SIP_PASSWORD"),
        domain=_req_env("SIP_DOMAIN"),
        registrar=os.environ.get("SIP_REGISTRAR") or _req_env("SIP_DOMAIN"),
        transport=sip_raw["transport"],
        local_port=int(sip_raw["local_port"]),
        rtp_port_range=tuple(sip_raw["rtp_port_range"]),  # type: ignore[arg-type]
        codec_priorities=dict(sip_raw["codec_priorities"]),
    )

    tg_raw = raw["telegram"]
    tg = TelegramConfig(
        api_id=int(_req_env("TG_API_ID")),
        api_hash=_req_env("TG_API_HASH"),
        # Optional now: it's the fallback target when the SIP destination doesn't
        # carry a routable number/username. 0 = no fallback (reject such calls).
        forward_user_id=int(os.environ.get("TG_FORWARD_USER_ID") or 0),
        session_name=tg_raw["session_name"],
        session_dir=session_dir,
        call_protocol=tg_raw["call_protocol"],
    )

    br_raw = raw["bridge"]
    bridge = BridgeConfig(
        sip_sample_rate=int(br_raw["sip_sample_rate"]),
        tg_sample_rate=int(br_raw["tg_sample_rate"]),
        frame_ms=int(br_raw["frame_ms"]),
        jitter_ms=int(br_raw["jitter_ms"]),
    )

    bh_raw = raw["behaviour"]
    behaviour = BehaviourConfig(
        reject_if_busy=bool(bh_raw["reject_if_busy"]),
        hangup_on_tg_decline=bool(bh_raw["hangup_on_tg_decline"]),
        caller_id_in_tg_message=bool(bh_raw["caller_id_in_tg_message"]),
    )

    vid_raw = raw.get("video") or {}
    source_url = os.environ.get("VIDEO_SOURCE_URL", "").strip()
    source_cmd = os.environ.get("VIDEO_SOURCE_CMD", "").strip()
    video = VideoConfig(
        enabled=bool(source_url or source_cmd),
        source_url=source_url,
        source_cmd=source_cmd,
        source_user=os.environ.get("VIDEO_SOURCE_USER", ""),
        source_pass=os.environ.get("VIDEO_SOURCE_PASS", ""),
        input_args=os.environ.get("VIDEO_INPUT_ARGS", ""),
        width=int(vid_raw.get("width", 640)),
        height=int(vid_raw.get("height", 480)),
        fps=int(vid_raw.get("fps", 15)),
        h264=_env_bool("VIDEO_H264", True),
    )

    return Config(
        sip=sip,
        telegram=tg,
        bridge=bridge,
        behaviour=behaviour,
        video=video,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


def _req_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"required env var {name} is not set")
    return val
