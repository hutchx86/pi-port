# Pi Port -- x86/no-NPU Docker variant

An x86-CPU Docker variant of the RK3588/Orange Pi AI Port emulator in `../`.
It stands up the same emulated UniFi Protect AI Port on a general x86 server
with no NPU. This is an **addition**, not a replacement: `../` is still the
real, live-deployed, Orange-Pi-hosted emulator, and nothing here affects it.

This is an interoperability reimplementation, not the original Ubiquiti
product; it is not affiliated with or endorsed by Ubiquiti. Licensed
AGPL-3.0-or-later (see the repository root).

## What differs from the RK3588 variant

Only the detection backend:

- `detector.py` runs the same YOLOv5 model through `onnxruntime` on the CPU
  (`CPUExecutionProvider`) instead of `rknnlite` on the RK3588 NPU.
- `requirements.txt` uses `onnxruntime` instead of `rknn-toolkit-lite2`.

Everything else (`config.py`, `discovery.py`, `http_api.py`, `ucp4_client.py`,
`avclient.py`, `instance_manager.py`, `run_all.py`, `aiport.cfg`) is a verbatim
mirror of `../`; re-sync those files from the parent tree when it changes.
Video decode needs no changes: `avclient.py` already falls back to software
`ffmpeg` when the Rockchip `ffmpeg-rkmpp` binary is absent, as it is here.

The x86 detector supports both output layouts and auto-detects which is in
use: the default Ultralytics `models/yolov5n.onnx` (single `[1,25200,85]`
output, fp16 input, box decode and sigmoid baked into the graph) and the
Rockchip `yolov5s_relu.onnx` (three raw `[1,255,H,W]` heads needing the
anchor/grid decode). Point `AIPORT_MODEL_PATH` at either one.

## Confirmed vs. untested

Confirmed:
- `docker build` succeeds and the image is hadolint-clean.
- With host networking, all four components come up: discovery listens on
  `eth0`, the control API binds `:443`, and the detector initializes.
- Both model layouts are supported and detect correctly on a still image.

Untested:
- Real cameras / real RTSP frames through the live capture pipeline.
- macvlan networking.
- CPU contention between onnxruntime and ffmpeg software decode under a
  4-camera load. The RK3588 variant does not have this problem, because decode
  and inference run on separate hardware blocks (RKVDEC + NPU).

## Build and run

The ONNX model is gitignored, so fetch it first. `run-smoke-test.sh` does
fetch + build + host-network start + stay-up check in one go:

```bash
python3 ../../scripts/fetch_models.py     # -> models/yolov5n.onnx
./run-smoke-test.sh

# manual equivalent:
docker build -t piport-x86 .
docker run --rm --cap-add=NET_ADMIN --network host piport-x86
```

`docker-compose.yml` is the single-instance host-network test.
`docker-compose.macvlan.yml` sketches the macvlan approach; replace its
placeholder NIC name, subnet, MAC, device-id, and IP before use.

## Networking

The L2 discovery beacon (`discovery.py`, UDP/10001) must be broadcast-visible
on the console's LAN, and the console must reach this container's `:443` at a
real, stable LAN IP. Docker's default bridge network breaks both: it
source-NATs traffic (masquerade) and serves DNS through an embedded stub at
`127.0.0.11`, so the container cannot present a genuine LAN identity. Use:

- `--network host` (`docker-compose.yml`) -- simplest, single instance. The
  container shares the host's own IP and DNS, so it is not a distinct LAN
  device.
- a macvlan network (`docker-compose.macvlan.yml`) -- a distinct MAC/IP per
  container, matching what `instance_manager.py` does on the Pi. The host
  itself cannot reach macvlan container IPs without an extra macvlan shim
  interface on the host; this file is illustrative and untested.

`run_all.py` auto-detects the route-carrying interface, so no `--iface` is
usually needed. For several concurrent instances on one x86 box, prefer one
container per instance (each with its own macvlan attachment, `--mac`, and
`--device-id`) rather than running `instance_manager.py` inside one container.
