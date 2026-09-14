# Pi Port -- x86/no-NPU variant

An x86-CPU-only variant of `../` (the RK3588/Orange Pi AI Port emulator),
added 2026-09-06 to explore whether this project is worth containerizing
for a general x86 server instead of staying tied to Rockchip hardware. This
is an **addition**, not a replacement: `../` (discovery.py, http_api.py,
ucp4_client.py, avclient.py, detector.py, instance_manager.py, run_all.py at
the project root) is completely untouched and remains the real, live-deployed,
Orange-Pi-hosted emulator. Nothing here changes how that one runs.

## What's different from the RK3588 variant

Only **one** real functional difference: `detector.py` here
runs the same YOLOv5s model via `onnxruntime` on CPU instead of via
`rknnlite` on the RK3588's NPU. Everything else
(`config.py`, `discovery.py`, `http_api.py`, `ucp4_client.py`, `avclient.py`,
`instance_manager.py`, `run_all.py`) is a **verbatim mirror** of `../` --
deliberately, to avoid any risk of the wire protocol behaving differently
between variants. `aiport.cfg` is copied from `../aiport.cfg` too (the image
ships it as a template). Re-copy them from `../` whenever the main tree changes
(they had drifted badly once before: the mirror predated the `config.py`
refactor and all the zone-filtering / coordinate-space / snapshot / Line
Crossing fixes -- re-synced 2026-09-13). Video decode also degrades
gracefully with zero code changes: `avclient.py`'s own
`FFMPEG_BIN`/`_HWDEC_ARGS` constants already fall back to plain
software-decode `ffmpeg` whenever the Rockchip `ffmpeg-rkmpp` binary isn't
present, which is exactly the case in this image (see `Dockerfile`), which
also now ships the mirrored `config.py`.

## What's CONFIRMED vs. still untested

**Confirmed, live, this session:**
- Default model is `models/yolov5n.onnx` -- the official Ultralytics YOLOv5n
  ONNX export (release `v7.0`), **3.8 MiB**. Its single `output0` is
  `[1,25200,85]` (xywh in pixels + objectness + 80 class scores, with box
  decode and sigmoid baked into the graph) and its input is fp16.
  `detector.py` auto-detects this layout and honours the fp16 input dtype.
- The older Rockchip `yolov5s_relu.onnx` (28.9 MiB, three raw `[1,255,H,W]`
  heads needing the anchor/grid decode) is still supported -- point
  `AIPORT_MODEL_PATH` at it and `detector.py` picks the matching path.
- Ran both against `../../rknn_convert/bus.jpg` (the RKNN pipeline's own sanity
  image): each finds 3 `person` + 1 `vehicle` (the bus), no spurious classes.
  yolov5n scores 0.51-0.83, yolov5s_relu 0.70-0.88.
- Throughput on a 12th-gen Intel Core i7-12800H (laptop-class, NOT a server
  chip), single shared `Detector`, onnxruntime CPU provider: **yolov5n
  ~40 ms/frame (~25 fps)**, yolov5s_relu ~65 ms/frame (~15 fps). The project's
  2 fps-per-camera tick needs ~8 inferences/sec for 4 cameras, so yolov5n
  leaves real headroom even on a laptop. The earlier ~7.3 fps figure was
  depressed by the duplicate-detection bug below.
- **NMS bug found and fixed.** The x86 `_nms_boxes` had dropped the
  `+ w[other]`/`+ h[other]` terms from the intersection, so IoU was always 0
  and *every* duplicate survived -- 29 raw boxes vs 4 real ones on bus.jpg,
  and needless per-frame cost. Fixed to match `../detector.py`, with a
  regression test in `../tests/test_detector.py` that runs against both the
  RK3588 and x86 detectors.
- **`docker build` works** (2026-09-14): `docker build -t piport-x86 .`
  succeeds (hadolint-clean), and the container brings up all four components
  on bridge networking -- discovery listening on `eth0`, control API bound,
  detector initialized.

**NOT yet confirmed -- do these before trusting this in production:**
1. **Real camera / real RTSP frames.** The bus.jpg test proves the model
   and math are correct; it says nothing about real-world detection
   accuracy or sustained framerate against this project's actual capture
   pipeline (ffmpeg software decode + live motion-tick cadence in
   `avclient.py`).
2. **Networking mode.** See below -- this needs macvlan or host networking,
   not Docker's default bridge+NAT. Untested either way.
3. **Whether onnxruntime's default thread pool fights with ffmpeg's own
   decode threads** for CPU time when 4 cameras' software decode + shared
   detector inference all run concurrently on the same box -- the RK3588
   variant never has this problem because decode and inference use
   physically separate hardware blocks (RKVDEC + NPU). Worth watching
   `top -H` under real 4-camera load.

## Networking

The L2 discovery beacon (`discovery.py`, UDP/10001) needs genuine broadcast
visibility on the real UniFi console's LAN, and the console needs to reach
this container's `:443` at a real, stable LAN IP -- Docker's default
bridge+NAT network breaks both of those. This needs **macvlan** (own
MAC+IP per instance, matching what `instance_manager.py` already does with
real macvlan sub-interfaces on the Pi) or `--network host` if you only ever
need one instance and the host's own network identity is acceptable to
give the container.

`docker-compose.yml` is the single-instance host-network smoke test.
`docker-compose.macvlan.yml` sketches the macvlan approach -- **explicitly
marked illustrative/untested** in its own comments. Replace the placeholder NIC
name, subnet, and MAC/device-id/IP values with real ones for your network
before trying it.

## Building and running

The ONNX is gitignored, so fetch it first. `run-smoke-test.sh` does fetch +
build + a host-network start + a stay-up check in one go:

```bash
python3 ../../scripts/fetch_models.py     # -> models/yolov5n.onnx
./run-smoke-test.sh

# manual equivalent:
docker build -t piport-x86 .
docker run --rm --cap-add=NET_ADMIN --network host piport-x86
```

(`--network host` is the simpler single-instance path to try first and is what
`docker-compose.yml` uses; no `--iface` is needed because `run_all.py`
auto-detects the route-carrying interface. Switch to
`docker-compose.macvlan.yml` once host mode works, if you want multiple
concurrent instances the way the Pi's `instance_manager.py` supports.)

## If you want multiple instances on one x86 box

`instance_manager.py` is copied here unchanged and should work as-is for a
bare-metal (non-Docker) x86 deployment -- it only manages
processes/systemd-units/macvlan-interfaces, nothing Rockchip-specific. For
a Docker deployment specifically, the more natural equivalent is one
container per instance (each with its own macvlan network attachment,
`--mac`, `--device-id`) rather than running `instance_manager.py` inside a
single container -- `docker-compose.macvlan.yml`'s `aiport-main` service is meant
to be copy-pasted as a template for additional instances, not run as the
only one forever.
