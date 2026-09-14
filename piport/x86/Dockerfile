# Pi Port -- x86/no-NPU AI-Port emulator (onnxruntime on CPU).
#
# Build context is this directory (piport/x86). The ONNX model is gitignored, so
# fetch it first:
#   python3 ../../scripts/fetch_models.py
#   docker build -t piport-x86 .
#
# See README.md for networking (host vs macvlan) and testing.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ffmpeg: software H.264 decode (avclient.py falls back to plain ffmpeg when the
# Rockchip rkmpp build is absent). iproute2: config.resolve_ip shells out to
# `ip`. ca-certificates: TLS to the Protect console.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        iproute2 \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run_all.py instance_manager.py config.py \
     discovery.py http_api.py ucp4_client.py avclient.py detector.py ./
COPY aiport.cfg ./
COPY models/ ./models/

# Fail fast with a clear message if the model wasn't fetched before the build.
RUN test -f models/yolov5n.onnx \
 || { echo >&2 "models/yolov5n.onnx missing: run scripts/fetch_models.py before docker build"; exit 1; }

# :10001/udp L2 discovery beacon, :443/tcp adopt/control HTTPS API. Use
# --network host (single instance) or a macvlan network; the default bridge+NAT
# breaks L2 discovery. run_all.py auto-detects the route-carrying interface, so
# no --iface is needed in the common case.
EXPOSE 10001/udp 443/tcp

# run_all.py binds :443/:10001, so the container must stay root (no USER).
# Deliver SIGINT (not the default SIGTERM) so run_all.py's KeyboardInterrupt
# handler stops its four children instead of orphaning them.
STOPSIGNAL SIGINT

ENTRYPOINT ["python3", "run_all.py"]
