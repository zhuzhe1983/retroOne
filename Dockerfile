# LinuxServer Debian trixie / Selkies 2.0, amd64 manifest verified 2026-10-19.
ARG SELKIES_BASE=ghcr.io/linuxserver/baseimage-selkies:debiantrixie@sha256:a684b614e142b4d1e942b98a2c5232fdaff94c33f89f4a751d97303866afb645

# Eden's official AppImage uses URuntime + DwarFS, not SquashFS. Run its
# SHA-pinned extractor through Debian QEMU in this build-only stage so that
# Apple Silicon buildx also works (Rosetta cannot run its static PIE loader).
FROM ${SELKIES_BASE} AS eden-extract
RUN apt-get update && apt-get install -y --no-install-recommends qemu-user \
    && rm -rf /var/lib/apt/lists/*
COPY vendor/eden.AppImage /tmp/eden.AppImage
COPY container/runtime-lock.json /tmp/runtime-lock.json
RUN python3 -c 'import hashlib,json; from pathlib import Path; p=Path("/tmp/eden.AppImage"); expected=json.loads(Path("/tmp/runtime-lock.json").read_text())["Eden-0.2.1.AppImage"]["sha256"]; assert hashlib.sha256(p.read_bytes()).hexdigest()==expected, "Eden checksum differs from reviewed lock"' \
    && chmod 0755 /tmp/eden.AppImage \
    && mkdir -p /tmp/eden-extract \
    && cd /tmp/eden-extract \
    && qemu-x86_64 /tmp/eden.AppImage --appimage-extract >/dev/null \
    && test -x AppDir/AppRun && test -x AppDir/bin/eden \
    && mv AppDir /opt/eden \
    && rm /tmp/eden.AppImage

FROM ${SELKIES_BASE}

ENV ROM_ROOT=/roms CONFIG_ROOT=/config/retroone RETROONE_GPU_BACKEND=intel \
    RETROONE_ROM_ROOT=/roms RETROONE_CONFIG_ROOT=/config/retroone \
    RETROONE_CORE_ROOT=/opt/retroone/libretro \
    EMULATORJS_ROOT=/opt/retroone/vendor/emulatorjs EMULATORJS_DATA=/opt/retroone/vendor/emulatorjs/data \
    SUBFOLDER=/stream/ TITLE=RetroOne START_DOCKER=false \
    PIXELFLUX_WAYLAND=false HARDEN_DESKTOP=true \
    NO_STEAM=true NO_WEBCAM=true SELKIES_COMMAND_ENABLED=false \
    SELKIES_FILE_TRANSFERS=none SELKIES_PRINTING_ENABLED=false \
    SELKIES_VIDEO_STREAMING_MODE=true SELKIES_FRAMERATE=60-60 \
    SELKIES_MANUAL_WIDTH=0 SELKIES_MANUAL_HEIGHT=0 \
    SELKIES_MANUAL_RESOLUTION="false|locked" SELKIES_ENABLE_RESIZE="false|locked" \
    RETROONE_ARTWORK_ROOT=/artwork \
    SELKIES_ENCODER=h264enc SELKIES_VIDEO_CRF=18 \
    SELKIES_GAMEPAD_ENABLED=true SELKIES_GAMEPAD_ON_START=true \
    SELKIES_AUDIO_ENABLED=true SELKIES_AUDIO_BITRATE=128000 \
    LC_ALL=zh_CN.UTF-8 PYTHONUNBUFFERED=1

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      retroarch retroarch-assets dosbox dolphin-emu mame 7zip unzip wmctrl \
      mesa-utils vainfo ffmpeg libqt6svg6 libqt6waylandclient6 \
      libqt6waylandcompositor6 libqt6opengl6 libqt6widgets6 libqt6network6 \
      libqt6concurrent6 libqt6core6t64 libqt6dbus6 libqt6gui6 \
      libaio1t64 libpcap0.8t64 libxcb-cursor0 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Wine 10 from Debian stable, including native 32-bit DirectDraw/DirectSound.
RUN dpkg --add-architecture i386 && apt-get -o Acquire::Retries=3 update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
      wine wine32:i386 wine64 fonts-wine fonts-wqy-microhei locales \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libasound2-plugins:i386 libgl1:i386 libglx-mesa0:i386 libgl1-mesa-dri:i386 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/retroone
COPY requirements.txt ./
RUN /lsiopy/bin/python -m pip install --no-cache-dir -r requirements.txt
COPY vendor/libretro/ /opt/retroone/libretro/
COPY vendor/pcsx2.AppImage /tmp/pcsx2.AppImage
RUN 7z x -y -o/opt/pcsx2 /tmp/pcsx2.AppImage >/dev/null && rm /tmp/pcsx2.AppImage
COPY container/pcsx2 /usr/local/bin/pcsx2
COPY vendor/sdlpal.AppImage /tmp/sdlpal.AppImage
RUN 7z x -y -o/opt/sdlpal /tmp/sdlpal.AppImage >/dev/null && rm /tmp/sdlpal.AppImage
COPY container/sdlpal /usr/local/bin/sdlpal
COPY --from=eden-extract /opt/eden/ /opt/eden/
COPY container/eden /usr/local/bin/retroone-eden
COPY container/wine-game /usr/local/bin/retroone-wine
COPY vendor/system/ /opt/retroone/system/
COPY vendor/emulatorjs/ /opt/retroone/vendor/emulatorjs/
COPY retroone/ /opt/retroone/retroone/
COPY scripts/library_audit.py /opt/retroone/scripts/library_audit.py
COPY web/ /opt/retroone/web/
COPY container/runtime-lock.json /opt/retroone/runtime-lock.json
COPY container/nginx.conf /opt/retroone/nginx.template.conf
COPY container/configure-nginx.py /opt/retroone/configure-nginx.py
COPY container/provisioner.py /opt/retroone/provisioner.py
RUN sed -i '/cp \/defaults\/default.conf/i python3 /opt/retroone/configure-nginx.py' /etc/s6-overlay/s6-rc.d/init-nginx/run
COPY container/stream-input.js /opt/retroone/stream-input.js
COPY container/azahar-core-options.cfg /opt/retroone/azahar-core-options.cfg
COPY container/prepare-cores.py /opt/retroone/prepare-cores.py
COPY container/root/ /
RUN chmod +x /usr/local/bin/pcsx2 /usr/local/bin/sdlpal /usr/local/bin/retroone-eden /usr/local/bin/retroone-wine /custom-cont-init.d/99-retroone /custom-services.d/retroone-api

# Some upstream assembly lacks .note.GNU-stack. glibc 2.41 refuses that
# executable-stack metadata on dlopen; apply the one reviewed SHA-pinned fix.
RUN apt-get update && apt-get install -y --no-install-recommends patchelf \
    && python3 /opt/retroone/prepare-cores.py \
    && rm -rf /var/lib/apt/lists/*

RUN dpkg-query -W -f='${binary:Package}\t${Version}\n' > /opt/retroone/runtime-packages.tsv

ENV SUBFOLDER=/p/player1/stream/ RETROONE_PROFILE_ID=player1 RETROONE_BASE_PATH=/p/player1 RETROONE_GATEWAY=true

EXPOSE 3000 3001
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8080/api/health >/dev/null || exit 1
