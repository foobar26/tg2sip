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
# pre-AVX2 CPUs, and 2.x removed the runtime toggle. We strip that one line and
# rebuild. All heavy deps (WebRTC, Clang, Boost, ffmpeg, GLib, X11, Mesa) are
# downloaded prebuilt by cmake — only the small wrapper compiles here.
FROM python:3.11-slim-bookworm AS ntgcalls-build
ARG NTGCALLS_VERSION=v2.1.0
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential python3-dev \
    libasound2-dev libpulse-dev flex libelf-dev texinfo \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN git clone --depth 1 --branch ${NTGCALLS_VERSION} --recurse-submodules --shallow-submodules \
    https://github.com/pytgcalls/ntgcalls.git
WORKDIR /build/ntgcalls
# Remove ONLY the openh264 software encoder (decoder kept); forces VP8/VP9.
RUN sed -i '/openh264::addEncoders/d' wrtc/src/video_factory/video_factory_config.cpp \
    && ! grep -q 'openh264::addEncoders' wrtc/src/video_factory/video_factory_config.cpp \
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
