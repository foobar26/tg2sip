# tg2sip — Telegram ↔ SIP gateway

Receives inbound SIP calls and bridges them as **native Telegram P2P voice calls** to a configured Telegram user. Runs as a Docker service.

## How it works

```
    Caller ──SIP──► [PJSUA2 UA] ──PCM 48k──► [NTgCalls P2P media] ──UDP──► Telegram user
                                                       ▲
                                  Pyrogram signaling (DH, request/confirm/discard)
```

The gateway logs in as a Telegram user account (its own phone number — *not* a bot). When a SIP INVITE arrives, the gateway:
1. Answers the SIP call (PJSUA2), sending `180 Ringing` while it sets up the TG leg
2. Initiates a Telegram P2P call to the configured user via Pyrogram raw API (`phone.requestCall`)
3. Lets **NTgCalls** run the Diffie-Hellman exchange — the gateway only shuttles the MTProto messages (`phone.requestCall` → `phoneCallAccepted` → `phone.confirmCall`) and relays the encrypted ICE/handshake blobs both ways (`phone.sendSignalingData` ↔ `updatePhoneCallSignalingData`)
4. NTgCalls establishes the encrypted WebRTC/UDP media to the user and reaches `CONNECTED`
5. Answers SIP with `200 OK` and bridges PCM both ways: PJSUA2 ↔ a custom `AudioMediaPort` ↔ NTgCalls external frames (48 kHz mono)

Either side hanging up tears down both legs.

## Status

Working end-to-end: inbound SIP calls bridge to a Telegram P2P call with **two-way audio** and **optional one-way outgoing video** (e.g. a camera's RTSP/MJPEG feed shown as the caller's camera).

| Component | Maturity |
|---|---|
| SIP UA (PJSUA2) | ✅ Mature, well-tested library |
| Telegram MTProto signaling (Pyrogram) | ✅ Mature |
| NTgCalls P2P media (audio) | ✅ Working |
| NTgCalls P2P media (outgoing video) | ✅ Working (VP8; see the CPU note below) |
| Audio bridge plumbing | ✅ Working; jitter buffer may want tuning for your network |

> **NTgCalls is built from source, not pip-installed.** The P2P call code in `src/telegram_media.py` targets the `ntgcalls` 2.x API (`create_p2p_call` + `set_stream_sources(CAPTURE)`, `init_exchange`/`exchange_keys`/`connect_p2p`, the signaling relay, and the Playback-on-`microphone` quirk). The Dockerfile's `ntgcalls-build` stage compiles ntgcalls **with the openh264 (H264) software encoder removed** — that encoder uses AVX2 and **crashes (SIGILL) on pre-AVX2 CPUs** (e.g. Ivy Bridge / older), and ntgcalls 2.x no longer lets you disable it at runtime. Stripping it forces VP8/VP9. On a CPU **with** AVX2 you could instead just `pip install ntgcalls` and drop the build stage. If you bump the ntgcalls version, revisit `telegram_media.py` and the patch line — the API and internals change across versions.

### Tested clients

| Telegram client | Negotiated `library_version` | Status |
|---|---|---|
| Telegram Android / iOS / Desktop | 9.0.0 (V2 signaling, external relay) | ✅ Two-way audio + outgoing video |
| Telegram WebK (`web.telegram.org/k/`) | 12.0.0 / 13.0.0 (V3 signaling, SCTP + gzip) | ✅ Two-way audio + outgoing video — requires the `dev`-branch ntgcalls pinned in the Dockerfile |
| Telegram WebA (`web.telegram.org/a/`) | — | ⚠️ Untested — WebA's outbound-call path appears broken in the client itself (even WebA → mobile direct calls fail), independent of this gateway |

The gateway offers `["8.0.0", "9.0.0", "12.0.0", "13.0.0"]` in `config/config.yaml`; the actually-used version is the highest in the intersection with the peer's offer and is logged per call (`negotiated library_versions=…; ntgcalls will use …`). WebK rejects offers that don't include 12/13 with `[406 CALL_PROTOCOL_COMPAT_LAYER_INVALID]`, so the dev-branch pin is required for WebK compatibility — see [pytgcalls/ntgcalls#46](https://github.com/pytgcalls/ntgcalls/issues/46).

If you'd rather use a different stack, the C++ project [kruglinski/tg2sip](https://github.com/kruglinski/tg2sip) (PJSIP + libtgvoip) is an older but battle-tested alternative built specifically for this.

## Prerequisites

1. **A separate Telegram phone number for the gateway.** It cannot share an account with the destination user. Use a spare SIM, a virtual number (e.g. JMP.chat, TextNow, etc.), or any number that can receive a Telegram login SMS.
2. A SIP account (provider + username + password).
3. Telegram **API ID + API hash** from <https://my.telegram.org/apps>.
4. The numeric **Telegram user ID** of the person to forward calls to. Easiest: send any message to `@userinfobot` in Telegram.
5. Docker + docker-compose.

## Setup

```bash
cp .env.example .env
cp config.example.yaml config/config.yaml
$EDITOR .env config/config.yaml
```

**First-run Telegram login** (interactive — must be done once before running as a daemon):

```bash
docker-compose run --rm gateway python -m src.auth
```

You'll be prompted for the gateway's phone number, the SMS code, and 2FA password if enabled. This produces a `sessions/gateway.session` file. Keep this file secret — it's a fully authenticated Telegram session.

**Run the service:**

```bash
docker-compose up -d
docker-compose logs -f gateway
```

> Examples use the legacy `docker-compose` binary (v1). If you've installed the Compose v2 plugin (`apt install docker-compose-plugin`), replace each call with `docker compose` (space, no hyphen). `--rm` is supported in both.

## Configuration

`.env` holds secrets:

```
TG_API_ID=123456
TG_API_HASH=abcdef...
SIP_USERNAME=1001
SIP_PASSWORD=secret
SIP_DOMAIN=sip.provider.example
TG_FORWARD_USER_ID=987654321
```

`config/config.yaml` holds non-secret settings (ports, codecs, log level, ringing behaviour). See `config.example.yaml`.

### Call routing (fixed or dynamic)

By default calls go to `TG_FORWARD_USER_ID`. You can instead route **per call** by the dialed SIP destination (the user part of the To/Request-URI). Point Asterisk at the gateway with the target as the SIP user, e.g.:

```ini
exten => _X.,1,Dial(PJSIP/+49123456789@tg2sip)   ; calls that phone's Telegram account
```

The gateway interprets the dialed value as:

| Dialed user part | Routes to |
|---|---|
| `+49123456789` (leading `+`) | that **phone number's** Telegram account (looked up; the number is imported as a contact on the gateway account) |
| `@alice` | Telegram **username** `alice` |
| `123456789` (plain digits) | Telegram **numeric user id** |
| anything else / the endpoint name | falls back to `TG_FORWARD_USER_ID` |

Notes: the phone number must be **E.164 with the leading `+`** (plain digits are treated as a user id, not a phone). The destination must be a Telegram user. `TG_FORWARD_USER_ID` is now optional — leave it blank to reject calls that don't name a routable target. Resolved targets are cached for the session. The incoming call's `localUri`/`remoteUri` are logged so you can confirm what your PBX actually sends.

### Calling from Telegram to SIP (inbound)

The gateway also works **the other way**: when a whitelisted Telegram account *calls the gateway's Telegram account*, it dials a SIP extension and bridges the audio. The caller hears Telegram's ringback while the phone rings; once the phone answers, audio bridges both ways.

Configure the routes in `config/config.yaml` under `telegram.inbound_routes` — this map is **also the whitelist**: callers not listed are declined.

```yaml
telegram:
  inbound_routes:
    "123456789":      "100"            # TG user id  → SIP extension 100
    "+4915112345678": "100"            # TG phone    → SIP extension 100
    "@somebody":      "200"            # TG username → SIP extension 200
    "234567890": "sip:door@10.0.0.5"   # full SIP URI also allowed
```

| Route key | Matches |
|---|---|
| `"123456789"` (quoted digits) | the caller's **numeric Telegram user id** (most reliable) |
| `"+4915112345678"` (E.164) | the caller's **phone number** — resolved to a user id (see note) |
| `"@somebody"` | the caller's Telegram **username** |

| Route value | Dials |
|---|---|
| `100` (bare) | `sip:100@SIP_DOMAIN` (your PBX routes the extension) |
| `sip:door@10.0.0.5` | that URI verbatim |

Notes:
- **Phone-number keys** work, but indirectly: Telegram never reveals a *caller's* number to us (we only get their user id + username). So a `+<phone>` key is resolved to *its* user id (via a contact import, like the SIP→TG direction) and the incoming call's id is matched against that — the number must belong to a Telegram user. Numeric user-id keys are the most direct (no lookup). Tip: check the log line `incoming TG call from user <id>` to learn a caller's id.
- The gateway handles **one call at a time** in either direction; a second call (either way) gets a busy decline.
- Audio bridges both ways. If a video source is configured (`VIDEO_SOURCE_URL`/`VIDEO_SOURCE_CMD`, e.g. a doorbell camera) it is also sent **to the Telegram caller** — one-way video, since the SIP phone has no camera. Any video the caller sends is ignored.
- For this to work, the gateway's Telegram account must **accept calls** from these users (Telegram Settings → Privacy → Calls).

### Video calls (optional)

Set `VIDEO_SOURCE_URL` in `.env` to turn inbound calls into Telegram **video** calls — the configured user sees that stream as the caller's camera, while audio is still bridged from the SIP side. Any ffmpeg-readable input works (MJPEG/HTTP/RTSP/file):

```
VIDEO_SOURCE_URL=http://camera.local/stream.mjpg
# if the stream needs HTTP/RTSP basic auth:
VIDEO_SOURCE_USER=admin
VIDEO_SOURCE_PASS=secret
```

Credentials are URL-encoded into the stream URL for ffmpeg (Basic/Digest) and redacted from logs. Encode resolution/fps come from the `video:` block in `config/config.yaml` (defaults 640×480 @ 15 fps). The gateway runs `ffmpeg` itself (a static build with RTSP, baked into the image) to decode the source to raw `yuv420p`, then pushes frames into ntgcalls as **EXTERNAL** camera frames (paced, strictly-increasing timestamps). For RTSP cameras add `VIDEO_INPUT_ARGS=-rtsp_transport tcp`. Leave `VIDEO_SOURCE_URL` blank for audio-only calls.

Notes:
- **One-way out.** Incoming video *from* the Telegram user is ignored (there's no SIP video leg).
- **Codec/CPU.** Video is encoded with **VP8 in software** (H264 is stripped from the build — see Status). That's CPU-heavy; on an old CPU keep the resolution/fps modest (e.g. 320×240–640×480) and bump only as far as stays smooth.
- **Why EXTERNAL, not ntgcalls' SHELL source.** ntgcalls' built-in SHELL video reader deadlocks against the EXTERNAL (SIP) audio in its capture A/V-sync, so both legs must be EXTERNAL and we run ffmpeg ourselves.

## Using with a local Asterisk PBX

Typical deployment on a host that already runs Asterisk:

```
PSTN/SIP trunk ─► Asterisk (UDP 5060) ─dialplan─► tg2sip (UDP 5062) ─► Telegram
```

Asterisk owns port 5060 and the external trunk; tg2sip is just an internal endpoint that Asterisk hands selected calls to. Steps:

**1. Set tg2sip's port to something other than 5060** (already done in `config.example.yaml` → `5062`).

**2. Point tg2sip at Asterisk** via `.env`:

```
SIP_USERNAME=tg2sip
SIP_PASSWORD=<a long random secret>
SIP_DOMAIN=127.0.0.1
SIP_REGISTRAR=127.0.0.1:5060
```

**3. Define tg2sip as an endpoint in Asterisk.** For modern `chan_pjsip` (`/etc/asterisk/pjsip.conf`):

```ini
[tg2sip]
type=endpoint
context=tg2sip-in
disallow=all
allow=ulaw,alaw
auth=tg2sip-auth
aors=tg2sip
direct_media=no

[tg2sip-auth]
type=auth
auth_type=userpass
username=tg2sip
password=<same secret as SIP_PASSWORD>

[tg2sip]
type=aor
max_contacts=1
qualify_frequency=60
```

For legacy `chan_sip` (`/etc/asterisk/sip.conf`):

```ini
[tg2sip]
type=friend
secret=<same secret as SIP_PASSWORD>
host=dynamic
context=tg2sip-in
disallow=all
allow=ulaw,alaw
qualify=yes
```

**4. Route calls to tg2sip** in `/etc/asterisk/extensions.conf`. Easiest pattern — forward every inbound trunk call to Telegram:

```ini
[from-trunk]
exten => _X.,1,NoOp(Inbound call from ${CALLERID(num)} → Telegram)
 same => n,Dial(PJSIP/tg2sip,30)
 same => n,Hangup()
```

Or only forward a specific DID:

```ini
exten => 12345,1,Dial(PJSIP/tg2sip,30)
```

**5. Reload Asterisk:**

```bash
sudo asterisk -rx 'pjsip reload'      # or: sip reload
sudo asterisk -rx 'dialplan reload'
```

**6. Start tg2sip and verify registration:**

```bash
docker compose up -d
docker compose logs -f gateway   # look for "sip registration status=200"
sudo asterisk -rx 'pjsip show endpoint tg2sip'   # should show registered AOR
```

If registration fails, check:
- `sudo asterisk -rx 'pjsip set logger on'` to watch the SIP exchange
- The secret in `.env` matches the one in `pjsip.conf`
- The container is on host networking (already the default in `docker-compose.yml`)

## Running as a systemd service

A unit file is provided at `deploy/tg2sip.service` (runs `docker compose up -d` / `down`; runs as root since the gateway needs Docker). Install it:

```bash
sudo install -m 644 deploy/tg2sip.service /etc/systemd/system/tg2sip.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg2sip        # start now + auto-start on boot
```

Control it:

```bash
sudo systemctl start tg2sip
sudo systemctl stop tg2sip                # docker compose down
sudo systemctl restart tg2sip             # recreate from the current image
sudo systemctl status tg2sip
sudo systemctl disable --now tg2sip       # stop + remove from boot
```

The service starts the **already-built** image (it never rebuilds). After a code/Dockerfile change: `sudo docker compose build` then `sudo systemctl restart tg2sip`.

## Logging to /var/log with rotation

By default logs go to stdout (`docker compose logs`). To also write them to `/var/log` with rotation, the compose file mounts `/var/log/tg2sip` into the container and sets `LOG_FILE=/var/log/tg2sip/gateway.log`; the app writes JSON there via a `WatchedFileHandler` (reopens the file after rotation). One-time host setup:

```bash
# 1. create the log dir owned by the container's user (uid 1000 = "gw")
#    (skip if you use the systemd service — it does this via ExecStartPre)
sudo mkdir -p /var/log/tg2sip && sudo chown 1000:1000 /var/log/tg2sip
# 2. install the logrotate config (daily, 14 kept, compressed, max 50M)
sudo install -m 644 deploy/logrotate-tg2sip /etc/logrotate.d/tg2sip
# 3. rebuild + restart so the app picks up file logging
sudo docker compose up -d --build         # or: build, then systemctl restart tg2sip
```

Then tail it with `tail -f /var/log/tg2sip/gateway.log`. Test rotation with `sudo logrotate -f /etc/logrotate.d/tg2sip`. If `/var/log/tg2sip` isn't writable by uid 1000, the app logs a warning to stderr and falls back to stdout-only (it won't crash).

## Operational notes

- **NAT / firewalls.** Telegram voice uses UDP; if running behind NAT, the relay (`phoneConnection`) typically traverses it, but you may need to publish UDP ports in `docker-compose.yml` for direct P2P. SIP equally needs RTP UDP reachability — use a STUN server or your provider's recommended ports.
- **Codec.** SIP leg defaults to PCMA/PCMU (8 kHz). Telegram leg uses Opus 48 kHz. The bridge resamples; expect ~20–40 ms added latency.
- **Concurrency.** The current implementation handles **one call at a time** by design (matches the typical "phone" use case). A second SIP INVITE while a call is active will be rejected with `486 Busy Here`.
- **Auto-reconnect.** The SIP registration is kept alive by PJSUA2; the Telegram session reconnects via Pyrogram. The Docker container's `restart: unless-stopped` handles process-level crashes.
- **Logs.** JSON to stdout (captured by Docker) and, when `LOG_FILE` is set, to a file — see [Logging to /var/log with rotation](#logging-to-varlog-with-rotation). Set `LOG_LEVEL=DEBUG` in `.env` to see PJSUA2 + Pyrogram (and ntgcalls/WebRTC) internals.

## Project layout

```
tg2sip/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── config.example.yaml
├── .env.example
└── src/
    ├── __main__.py          # entry point
    ├── gateway.py           # orchestrator (SIP <-> TG state machine)
    ├── config.py            # config loader
    ├── sip_agent.py         # PJSUA2 wrapper
    ├── telegram_signaling.py# Pyrogram raw API for call setup
    ├── telegram_media.py    # NTgCalls media layer (P2P setup + external-frame audio)
    ├── audio_bridge.py      # thread-safe PCM jitter buffer (TG→SIP)
    ├── auth.py              # one-shot interactive login
    └── log.py
```

## Troubleshooting

- **`pjsua2` import fails in container** — the Dockerfile builds PJSIP from source with `--enable-shared --with-python`. If you change the base image or Python version, you must rebuild PJSIP for that Python. See the Dockerfile.
- **Telegram login loops or `AUTH_KEY_UNREGISTERED`** — delete `sessions/gateway.session` and re-run `auth.py`.
- **No audio one direction** — run with `LOG_LEVEL=DEBUG` and watch the one-shot bridge counters: `bridge: first SIP→port frame received` / `first frame sent to ntgcalls` (caller→TG) and `first frame received from ntgcalls` / `first port→SIP frame requested` (TG→caller). Whichever line is missing tells you which leg isn't flowing.
- **Call connects then drops after ~10–30 s (`ntgcalls connection state=TIMEOUT`/`FAILED`)** — the WebRTC media path never established. It's ICE-over-signaling, so check the signaling relay is intact: with `DEBUG` you should see both `signaling: first outgoing blob → Telegram` and `recv updatePhoneCallSignalingData`. Missing the incoming one means the peer's ICE setup isn't reaching ntgcalls.
- **No incoming (TG→SIP) audio despite a stable `CONNECTED`** — incoming P2P audio is delivered to the Playback **Microphone** device, so the playback `MediaDescription` must set `microphone=` (not `speaker=`). See `TelegramMedia._playback_media`.
- **`PJMEDIA_EAUD_NODEFDEV` / no audio toward the caller** — the headless container has no sound card; the conference bridge needs `audDevManager().setNullDev()` (done in `SipAgent.start`) to provide a master clock so the custom audio port is polled.

## License

This repository's own source code is licensed under the **[Apache License 2.0](LICENSE)**.

It builds on third-party components under their own licenses — notably **PJSIP** (GPLv2-or-commercial), **ntgcalls** (GPLv3), **Pyrogram**/**TgCrypto** (LGPLv3), **WebRTC** (BSD), and **FFmpeg** (GPL, run as a separate process). None are bundled here; the Dockerfile pulls them at build time. The Apache 2.0 license covers this repo's source only — a **built** image combines those components and is therefore subject to their (copyleft) terms. See [NOTICE](NOTICE). Not legal advice.
