# AI Port emulator, x86/no-NPU variant. See README.md for status.
FROM python:3.11-slim

# Plain Debian ffmpeg, software H.264 decode only; avclient.py
# already falls back to plain "ffmpeg" when the RKMPP build is absent.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run_all.py instance_manager.py config.py \
     discovery.py http_api.py ucp4_client.py avclient.py detector.py ./
COPY models/ ./models/

# :10001/udp L2 discovery beacon, :443/tcp adopt/control HTTPS API. Use a
# macvlan network (or --network host) so both are reachable on the real LAN.
EXPOSE 10001/udp 443/tcp

# run_all.py enforces root (needs to bind :443), so no non-root USER.
# --iface defaults to "eth0"; override at run/compose time, not here.
ENTRYPOINT ["python3", "run_all.py"]
