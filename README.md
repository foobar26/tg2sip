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

Working end-to-end: inbound SIP calls bridge to a Telegram P2P voice call with **two-way audio**, pinned to `ntgcalls==1.3.4` (see `requirements.txt`).

| Component | Maturity |
|---|---|
| SIP UA (PJSUA2) | ✅ Mature, well-tested library |
| Telegram MTProto signaling (Pyrogram) | ✅ Mature |
| NTgCalls P2P media | ✅ Working against the pinned `ntgcalls==1.3.4` |
| Audio bridge plumbing | ✅ Working; latency / jitter buffer may want tuning for your network |

> **Version pin matters.** The NTgCalls P2P call setup in `src/telegram_media.py` is written against the exact API of `ntgcalls==1.3.4` (snake_case async methods, `create_p2p_call`/`init_exchange`/`exchange_keys`/`connect_p2p`, plus the signaling relay and the Playback-on-`microphone` quirk). If you bump `ntgcalls`, expect to revisit that file — the API has changed across versions.

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

### Video calls (optional)

Set `VIDEO_SOURCE_URL` in `.env` to turn inbound calls into Telegram **video** calls — the configured user sees that stream as the caller's camera, while audio is still bridged from the SIP side. Any ffmpeg-readable input works (MJPEG/HTTP/RTSP/file):

```
VIDEO_SOURCE_URL=http://camera.local/stream.mjpg
# if the stream needs HTTP/RTSP basic auth:
VIDEO_SOURCE_USER=admin
VIDEO_SOURCE_PASS=secret
```

Credentials are URL-encoded into the stream URL for ffmpeg (Basic/Digest) and redacted from logs. Encode resolution/fps come from the `video:` block in `config/config.yaml` (defaults 640×480 @ 15 fps). ntgcalls runs `ffmpeg … -f rawvideo -pix_fmt yuv420p … pipe:1` (the `SHELL` source) and reads raw frames — `ffmpeg` is already in the image. Leave `VIDEO_SOURCE_URL` blank for audio-only calls. Note: incoming video *from* the Telegram user is ignored (there's no SIP video leg); this is one-way video out.

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

## Operational notes

- **NAT / firewalls.** Telegram voice uses UDP; if running behind NAT, the relay (`phoneConnection`) typically traverses it, but you may need to publish UDP ports in `docker-compose.yml` for direct P2P. SIP equally needs RTP UDP reachability — use a STUN server or your provider's recommended ports.
- **Codec.** SIP leg defaults to PCMA/PCMU (8 kHz). Telegram leg uses Opus 48 kHz. The bridge resamples; expect ~20–40 ms added latency.
- **Concurrency.** The current implementation handles **one call at a time** by design (matches the typical "phone" use case). A second SIP INVITE while a call is active will be rejected with `486 Busy Here`.
- **Auto-reconnect.** The SIP registration is kept alive by PJSUA2; the Telegram session reconnects via Pyrogram. The Docker container's `restart: unless-stopped` handles process-level crashes.
- **Logs.** JSON to stdout (captured by Docker). Set `LOG_LEVEL=DEBUG` in `.env` to see PJSUA2 + Pyrogram internals.

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

Use it however you want. No warranty.
