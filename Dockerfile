FROM python:3.11-slim-bookworm AS pjsip-build

ARG PJSIP_VERSION=2.14.1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    ca-certificates \
    pkg-config \
    swig \
    libssl-dev \
    libasound2-dev \
    libopus-dev \
    libsrtp2-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN wget -q https://github.com/pjsip/pjproject/archive/refs/tags/${PJSIP_VERSION}.tar.gz \
    && tar xzf ${PJSIP_VERSION}.tar.gz \
    && mv pjproject-${PJSIP_VERSION} pjproject

WORKDIR /build/pjproject
RUN ./configure --enable-shared --disable-video --disable-sound --with-ssl \
    && make dep -j"$(nproc)" \
    && make -j"$(nproc)" \
    && make install \
    && ldconfig

WORKDIR /build/pjproject/pjsip-apps/src/swig
RUN make python \
    && cd python \
    && pip install --no-cache-dir . \
    && python -c "import pjsua2; print('pjsua2 ok at', pjsua2.__file__)"

# ---- ntgcalls: build from source with the H264 (openh264) encoder stripped ----
# The prebuilt ntgcalls wheel's openh264 ENCODER uses AVX2 and SIGILLs on
# pre-AVX2 CPUs, and 2.x+ removed the runtime toggle. We strip that one line and
# rebuild. All heavy deps (WebRTC, Clang, Boost, ffmpeg, GLib, X11, Mesa) are
# downloaded prebuilt by cmake — only the small wrapper compiles here.
FROM python:3.11-slim-bookworm AS ntgcalls-build
# v3.0.0 is the first release with v12/v13 protocol support (needed by WebK,
# pytgcalls/ntgcalls issue #46). Bump together with config library_versions.
# Accepts a tag, branch, or commit SHA — the init+fetch pattern below works for
# any of them (a plain `git clone --branch` does not accept SHAs).
ARG NTGCALLS_VERSION=v3.0.0
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential python3-dev \
    libasound2-dev libpulse-dev flex libelf-dev texinfo \
    libx11-dev libxext-dev libxrandr-dev libxcomposite-dev \
    libxcursor-dev libxdamage-dev libxfixes-dev libxi-dev libxtst-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN git init ntgcalls \
    && cd ntgcalls \
    && git remote add origin https://github.com/pytgcalls/ntgcalls.git \
    && git fetch --depth 1 origin ${NTGCALLS_VERSION} \
    && git checkout FETCH_HEAD \
    && git submodule update --init --recursive --depth 1
WORKDIR /build/ntgcalls
# Remove ONLY the openh264 software encoder (decoder kept); forces VP8/VP9.
# The call is `openh264::add_encoders` in 3.x (`addEncoders` in 2.x). Fail the
# build if it isn't found, so an upstream rename can't silently skip the patch
# (the result would SIGILL on pre-AVX2 CPUs as soon as video starts).
RUN f=wrtc/src/video_factory/video_factory_config.cpp \
    && grep -Eq 'openh264::(add_encoders|addEncoders)' "$f" \
    && sed -Ei '/openh264::(add_encoders|addEncoders)/d' "$f" \
    && ! grep -Eq 'openh264::(add_encoders|addEncoders)' "$f" \
    && grep -Eq 'openh264::(add_decoders|addDecoders)' "$f" \
    && echo "openh264 encoder stripped"
# Build the wheel (downloads prebuilt clang/webrtc/boost/ffmpeg/... then compiles).
RUN pip wheel . --no-deps -w /wheels && ls -la /wheels

FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    libopus0 \
    libsrtp2-1 \
    libasound2 \
    libpulse0 \
    curl \
    xz-utils \
    ca-certificates \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Debian's ffmpeg lacks the RTSP demuxer; use a full static build (incl. rtsp).
# ffmpeg decoders use runtime SIMD dispatch, so this is safe on pre-AVX2 CPUs.
RUN curl -fsSL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o /tmp/ffmpeg.tar.xz \
    && mkdir -p /tmp/ffx && tar xf /tmp/ffmpeg.tar.xz -C /tmp/ffx --strip-components=1 \
    && install -m 0755 /tmp/ffx/ffmpeg /tmp/ffx/ffprobe /usr/local/bin/ \
    && rm -rf /tmp/ffmpeg.tar.xz /tmp/ffx \
    && /usr/local/bin/ffmpeg -hide_banner -demuxers 2>/dev/null | grep -qi rtsp \
    && echo "static ffmpeg installed with rtsp support"

COPY --from=pjsip-build /usr/local/lib/ /usr/local/lib/
RUN ldconfig \
    && python -c "import pjsua2; print('runtime pjsua2 ok at', pjsua2.__file__)"

WORKDIR /app
COPY --from=ntgcalls-build /wheels/ /tmp/wheels/
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && python -c "import ntgcalls; ntgcalls.NTgCalls(); print('custom ntgcalls ok', getattr(ntgcalls,'__version__','?'))"

COPY src/ ./src/

RUN useradd -m -u 1000 gw && mkdir -p /app/sessions /app/config && chown -R gw:gw /app
USER gw

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "src"]
