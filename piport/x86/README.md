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
between variants. Re-copy them from `../` whenever the main tree changes
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
- The exact pre-conversion `yolov5s_relu.onnx` (the same model
  `rknn_convert/convert.py` quantizes to `.rknn` for the RK3588 variant) is
  sitting right here in `models/`, no re-sourcing or re-export needed.
- Its real I/O contract, checked directly via `onnx.load()`: input
  `images` is NCHW float32 `[1,3,640,640]` (RKNN's NHWC uint8 input and
  its `mean_values=[0,0,0]/std_values=[255,255,255]` normalization are
  RKNN-toolkit conveniences, not part of the graph -- this variant's
  `detector.py` does the transpose and `/255.0` explicitly instead). All
  three output heads already end in `Sigmoid` ops in the graph itself, so
  the shared box-decode math (copied verbatim from the RK3588
  `detector.py`, not reimplemented) is a straight drop-in with no extra
  activation step needed.
- Ran the new `detector.py` against `../../rknn_convert/bus.jpg` (the same
  image the RKNN conversion pipeline uses as its own sanity check): found
  3 `person` + 1 `vehicle` (the bus), scores 0.70-0.88, no spurious classes
  leaking through the COCO-to-AI-Port-vocabulary mapping. This confirms
  the whole model+math path is correct end to end, not just plausible from
  reading the graph.
- Single-threaded-caller throughput on a 12th-gen Intel Core i7-12800H
  (laptop-class, NOT a server chip): **~137ms/frame, ~7.3 fps**, via
  onnxruntime's own default (multi-threaded internally) CPU execution
  provider. This project's current cadence is a 2fps detection tick **per
  camera**, so 4 concurrently-paired cameras need ~8 inferences/sec total
  against the single shared `Detector` instance (same
  one-instance-behind-a-lock design as the RK3588 variant) -- meaning this
  laptop CPU is right at the edge for 4 cameras, not comfortably above it.
  A real "powerful x86 server" (more cores, server-class Xeon/EPYC, no
  laptop thermal throttling) should clear this with real headroom, but
  that's an expectation, not something measured here -- worth re-running
  this same benchmark on the actual target box before trusting it.

**NOT yet confirmed -- do these before trusting this in production:**
1. **`docker build` itself.** `docker` isn't runnable from the environment
   this was written in (WSL without Docker Desktop's WSL integration
   enabled) -- the `Dockerfile` was written carefully against the real,
   confirmed runtime requirements above, but has never actually been
   built. Treat your first real build as the first test of that file, not
   a formality. (You mentioned testing this on a remote server -- that's
   exactly the right next step.)
2. **Real camera / real RTSP frames.** The bus.jpg test proves the model
   and math are correct; it says nothing about real-world detection
   accuracy or sustained framerate against this project's actual capture
   pipeline (ffmpeg software decode + live motion-tick cadence in
   `avclient.py`).
3. **Networking mode.** See below -- this needs macvlan or host networking,
   not Docker's default bridge+NAT. Untested either way.
4. **Whether onnxruntime's default thread pool fights with ffmpeg's own
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

`docker-compose.yml` in this directory sketches the macvlan approach --
**explicitly marked illustrative/untested** in its own comments. Replace
the placeholder NIC name, subnet, and MAC/IP values with real ones for your
network before trying it.

## Building and running

```bash
docker build -t piport-x86 .
docker run --rm --cap-add=NET_ADMIN --network host piport-x86 --iface eth0
```

(`--network host` above is the simpler single-instance path to try first;
switch to the macvlan compose file once that's confirmed working, if you
want multiple concurrent instances the way the Pi's `instance_manager.py`
supports.)

## If you want multiple instances on one x86 box

`instance_manager.py` is copied here unchanged and should work as-is for a
bare-metal (non-Docker) x86 deployment -- it only manages
processes/systemd-units/macvlan-interfaces, nothing Rockchip-specific. For
a Docker deployment specifically, the more natural equivalent is one
container per instance (each with its own macvlan network attachment,
`--mac`, `--device-id`) rather than running `instance_manager.py` inside a
single container -- `docker-compose.yml`'s `aiport-main` service is meant
to be copy-pasted as a template for additional instances, not run as the
only one forever.
