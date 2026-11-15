# 固定基础发行摘要；目标平台由构建命令指定为 linux/amd64。
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS camera-tunnel-build
USER root
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends gcc libc6-dev
COPY Friday/native/PortableVision/camera_udp_tunnel.c /tmp/camera_udp_tunnel.c
RUN gcc -shared -fPIC -O2 -Wall -Wextra -Werror -o /tmp/camera-udp-tunnel.so /tmp/camera_udp_tunnel.c -ldl

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked --mount=type=cache,target=/var/lib/apt/lists,sharing=locked sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && rm -f /etc/apt/apt.conf.d/docker-clean && apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends ffmpeg espeak-ng libgl1 libegl1 libgles2 libglib2.0-0 libxcb1 libportaudio2 libsndfile1 socat ca-certificates
RUN pip install --no-cache-dir uv==0.13.7
COPY Friday/pyproject.toml Friday/uv.lock /opt/friday/
WORKDIR /opt/friday
RUN --mount=type=cache,target=/root/.cache/uv UV_PROJECT_ENVIRONMENT=/opt/runtime uv sync --locked --no-dev --extra vision --extra local-runtime --extra airplay --extra nas-connection --extra privacy-playback --extra ktv
COPY Friday/native/PortableVision/ /opt/friday/native/PortableVision/
RUN --mount=type=cache,target=/root/.cache/uv UV_PROJECT_ENVIRONMENT=/opt/vision uv sync --locked --no-dev --extra sound --project native/PortableVision
COPY Friday/native/PortableSeparation/ /opt/friday/native/PortableSeparation/
RUN --mount=type=cache,target=/root/.cache/uv UV_PROJECT_ENVIRONMENT=/opt/separation uv sync --locked --no-dev --project native/PortableSeparation
COPY ARL/ /opt/arl-source/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --python /opt/runtime/bin/python --no-deps '/opt/arl-source[linux]'
COPY Friday/ /opt/friday/
COPY --from=camera-tunnel-build /tmp/camera-udp-tunnel.so /opt/friday/native/PortableVision/camera-udp-tunnel.so
COPY container/ /opt/container/
COPY build-inputs.json /opt/container/build-inputs.json
RUN useradd --create-home --uid 1000 friday && mkdir -p /opt/friday/data /opt/friday/.artifacts && chmod -R a+rX /opt/friday /opt/container && chown friday:friday /opt/friday/data /opt/friday/.artifacts && chmod 700 /opt/friday/data /opt/friday/.artifacts
ENV PATH=/opt/runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin XDG_STATE_HOME=/opt/friday/data/state FRIDAY_EMBODIED_DATA=/opt/friday/data/embodied OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
USER friday
EXPOSE 8740 18745
HEALTHCHECK --interval=15s --timeout=12s --start-period=90s --retries=3 CMD ["/opt/runtime/bin/python", "/opt/container/entrypoint.py", "health"]
ENTRYPOINT ["/opt/runtime/bin/python", "/opt/container/entrypoint.py"]
