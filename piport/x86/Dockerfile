# Pi Port -- x86/no-NPU AI-Port emulator (onnxruntime on CPU).
# Build context is this directory. The ONNX model is gitignored; fetch it first:
#   python3 ../../scripts/fetch_models.py && docker build -t piport-x86 .
# See README.md for networking and testing.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ffmpeg: software H.264 decode (no Rockchip rkmpp binary here). iproute2:
# config.resolve_ip shells out to `ip`. ca-certificates: TLS to the console.
# dhclient is not installed: host networking uses the host's own lease.
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

# :10001/udp L2 discovery beacon, :443/tcp adopt/control HTTPS API. Needs
# --network host or macvlan; the default bridge+NAT breaks L2 discovery.
EXPOSE 10001/udp 443/tcp

# run_all.py binds :443/:10001, so the container must stay root (no USER).
# SIGINT (not the default SIGTERM) so run_all.py's KeyboardInterrupt handler
# stops its four children instead of orphaning them.
STOPSIGNAL SIGINT

ENTRYPOINT ["python3", "run_all.py"]
