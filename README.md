# Pi Port — a DIY UniFi Protect "AI Port" on an Orange Pi (RK3588)

Recreate Ubiquiti's **UniFi Protect AI Port** (catalog model `UVC AI Port`,
sysid `0xa5f1`) on a stock **Orange Pi 5 Plus (RK3588)** running Linux. The
device presents itself to a real UniFi Protect console as a genuine AI Port:
L2 discovery, adoption, camera pairing, video relay, **real NPU object
detection**, and dashboard detection events with thumbnails.

This is a reverse-engineering / interoperability **proof of concept**, not a
product. It speaks the vendor protocol only — no direct controller-database
writes, no modified firmware. The protocol was reverse-engineered from
firmware running on hardware the author owns.

> **Not affiliated with Ubiquiti Inc.** "UniFi", "UniFi Protect" and "AI Port"
> are trademarks of Ubiquiti Inc. This repository contains **no Ubiquiti
> binaries or firmware**; you must supply those yourself.

> **AI-assisted development.** Large parts of this project were produced with
> LLMs (Claude Code and DeepSeek) under human supervision, review, and
> real-hardware testing. Always verify anything you rely on.

## Status

Working and confirmed live against a real Protect console: discovery,
adoption, camera pairing, video relay, NPU motion detection, dashboard events
with thumbnails, and automatic un-adopt detection — with three real cameras
paired and streamed concurrently. See `piport/README.md` for detail.

Open items: live verification of multi-object tracking with a genuinely
simultaneous scene, and Line Crossing (implemented, awaiting a controller
test). The classifier is stock YOLOv5s (person/vehicle/animal), not Ubiquiti's
own model.

## Repository layout

```
piport/            the emulator (RK3588 / Orange Pi)
  discovery.py       UDP/10001 UBNT discovery responder
  http_api.py        HTTPS :443 adopt/control API
  ucp4_client.py     generic ucp4 device-management websocket
  avclient.py        pairing/streaming/smart-detect websocket + detection
  detector.py        YOLOv5s inference wrapper (RKNN / NPU)
  sysinfo_server.py  dev-box CPU/mem/GPU/NPU status page
  models/            anchors + COCO labels (model binaries fetched, not stored)
  instance_manager.py / run_all.py  multi-instance orchestration
  x86/               x86/no-NPU Docker variant (onnxruntime on CPU)
scripts/           install.sh (one-shot installer) + deployment helpers +
                   fetch_models.py (model assets)
rknn_convert/      YOLOv5s -> RKNN conversion recipe (x86 build host)
```

Model binaries (`.onnx`/`.rknn`) and all Ubiquiti firmware are **not** stored
here; see *Model assets* below.

## Requirements

- Orange Pi 5 Plus (or another RK3588 board) running Linux, with root — the
  emulator binds UDP/10001 and TCP/443.
- Python 3, and `rknn-toolkit-lite2` on the board for NPU inference.
- A UniFi Protect console you own and control on the same LAN.

The x86 variant needs Docker and runs `onnxruntime` on CPU instead of the
NPU; no Rockchip hardware required.

## One-shot install (RK3588 / Orange Pi)

On the board itself, one script does the whole stand-up — system packages, a
venv + dependencies, model assets, the required `librknnrt.so` upgrade, a
reboot-surviving systemd instance (via `instance_manager.py`), and the web UI:

```bash
sudo scripts/install.sh --console <UNVR-IP> [--parent-iface enP4p65s0]
```

Prerequisites/details:

- Run from the checkout root, as root. `--parent-iface` defaults to the
  auto-detected default-route NIC.
- The NPU model `yolov5s_relu.rknn` cannot be built on the Pi (the RKNN
  converter is x86_64-only). Build it on an x86 host with
  `scripts/fetch_models.py --convert`, or let the installer fetch it from
  `--rknn-url` / `PIPORT_RKNN_URL` / its `DEFAULT_RKNN_URL`. `--rknn <path>`
  takes a local build, and a local `models-rknn/` submodule is also detected.
  Use `--no-npu` for a CPU-only protocol-stack install.
- Opens the web UI on `:8090` (`--webui-port` to change) for board stats and
  instance management.

The manual steps below are what the installer automates, if you prefer them
by hand.

## Quick start (RK3588)

```bash
# 1. Fetch the model assets into models/ (downloads ONNX + labels;
#    --convert also builds the .rknn on an x86 box with rknn-toolkit2).
python3 scripts/fetch_models.py
python3 scripts/fetch_models.py --convert     # optional, x86 build host

# 2. Install runtime dependencies.
pip install -r piport/requirements.txt
pip install rknn-toolkit-lite2

# 3. Set device identity and your console IP.
$EDITOR piport/aiport.cfg

# 4. Run (needs root).
sudo python3 piport/run_all.py --iface <your-iface>
```

Then adopt "AI Port" from the Protect console UI like any camera, and pair a
real camera to it. `piport/README.md` covers configuration, the x86
variant, and the model/conversion details.

## Deployment

`run_all.py` needs root (binds UDP/10001 and TCP/443) and must be able to send
L2 broadcast/multicast on the LAN, so run it on the host, not in a NAT'd
container. It resolves its own interface's IP by default, so multiple
instances on one box don't collide on `:443`.

### Single instance (RK3588)

Run it directly (`sudo python3 piport/run_all.py --iface <iface>`), or
under systemd for reboot survival and crash recovery. A minimal unit:

```ini
[Unit]
Description=UniFi AI Port emulator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/pi-port/piport
ExecStart=/usr/bin/python3 /opt/pi-port/piport/run_all.py --iface <iface>
Restart=on-failure
KillMode=control-group
[Install]
WantedBy=multi-user.target
```

`KillMode=control-group` matters: `run_all.py` spawns four child processes and
only reaps them on `KeyboardInterrupt`, so a plain SIGTERM would orphan them.

### Multiple instances (RK3588)

`instance_manager.py` creates one systemd unit per instance, each with its own
macvlan sub-interface, MAC, and DHCP lease — a distinct L2 device, so the
console sees independent AI Ports:

```bash
sudo python3 piport/instance_manager.py create lab2 --parent-iface <iface>
python3 piport/instance_manager.py list
sudo python3 piport/instance_manager.py destroy lab2
```

Identity (MAC/device-id) is generated once at creation and baked into the
unit, so it is stable across reboots. `create --ip X.Y.Z.W` uses a static
address instead of DHCP.

### x86 / Docker variant

```bash
cd piport/x86
python3 ../../scripts/fetch_models.py       # -> x86/models/yolov5n.onnx
./run-smoke-test.sh                          # fetch + build + start + verify

# manual equivalent:
docker build -t piport-x86 .
docker run --rm --cap-add=NET_ADMIN --network host piport-x86
```

`docker-compose.yml` is the single-instance host-network test;
`docker-compose.macvlan.yml` is the multi-instance sketch (macvlan, not the
default bridge, which breaks L2 discovery). **The Dockerfile has still not been
built by the author (no Docker daemon in the authoring environment) —
`run-smoke-test.sh` on an x86 host is the first real test.** See
`piport/x86/README.md`.

### Operational notes

- Never `pkill -f 'run_all.py'`: the pattern matches the killing command
  itself. Use `pkill -9 -f '[r]un_all.py'`, and separately kill orphaned
  ffmpeg children (`pkill -9 -f '[f]fmpeg.*rtsp://<console-ip>:7447'`).
- To start detached over SSH, use `setsid -f` (a backgrounded `nohup` can hang
  the SSH channel).

## Model assets (not tracked)

The detector needs an open YOLOv5 model. Because the binaries are large and
reproducible, they are not committed; `scripts/fetch_models.py` downloads them:

| Asset | Used by | Source |
|---|---|---|
| `yolov5n.onnx` (3.8 MiB) | x86/CPU variant | Ultralytics release: <https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.onnx> |
| `yolov5s_relu.onnx` (28.9 MiB) | RKNN conversion (`.rknn`) | Rockchip model zoo delivery: <https://ftrg.zbox.filez.com/v2/delivery/data/95f00b0fc900458ba134f8b180b3f7a1/examples/yolov5/yolov5s_relu.onnx> |
| anchors, COCO labels, bus.jpg, calibration subset | both | <https://github.com/airockchip/rknn_model_zoo> (tag `v2.3.2`) |

The x86 variant defaults to `yolov5n.onnx`; it still accepts the Rockchip
`yolov5s_relu.onnx` via `AIPORT_MODEL_PATH` (both output layouts are
auto-detected). The Ultralytics model is AGPL-3.0, same as the weights.

`yolov5s_relu.rknn` is produced on an x86_64 machine with `rknn-toolkit2`
(install from PyPI or the Rockchip GitHub release — see
`rknn_convert/convert.py` and `piport/README.md`):

```bash
python3 scripts/fetch_models.py --convert
```

`scripts/install.sh` resolves the prebuilt `.rknn` in this order: `--rknn
<path>`, a local copy at `models-rknn/` (e.g. a git submodule of a separate
model repo) or `rknn_convert/`, then a download from `--rknn-url` /
`PIPORT_RKNN_URL` / its `DEFAULT_RKNN_URL`. It verifies the pinned sha256 where
one applies (always for the default URL). The default source is the separate
AGPL-3.0 model repo
[`hutchx86/pi-port-models`](https://github.com/hutchx86/pi-port-models) (tag
`v1`); the model is never committed to this repo.

**Model license.** The model derives from Ultralytics YOLOv5 (AGPL-3.0) via
Rockchip's model zoo (Apache-2.0), so any redistributed `.rknn` is an AGPL-3.0
work, separate from this repository's GPL-3.0-only source. If you redistribute
it, include the AGPL-3.0 text and attribution; the corresponding source is this
repo's conversion recipe plus the ONNX in the table above. Check those licenses
yourself before redistributing.

## Verification

Unit suite (no hardware required):

```bash
cd piport && python3 -m unittest discover -s tests
```

Live verification is done on the Orange Pi (see `piport/README.md` and
`scripts/live_watch.py`).

## Legal

- **Not affiliated with or endorsed by Ubiquiti Inc.**
- **No Ubiquiti binaries or firmware are distributed here.** You supply your
  own firmware images; reverse-engineering may be restricted in your
  jurisdiction and you are responsible for how you use this.
- Intended for interoperability and personal use on hardware and consoles you
  own. Nothing here targets third-party systems.

## Disclaimer

**Proof of concept, not production software.** Large parts were produced with
LLMs under human supervision and testing — review and verify everything
yourself. This software is provided "as is", without warranty of any kind. It
binds privileged ports, manipulates network interfaces (macvlan/DHCP), and
runs detection hardware; misconfiguration can disrupt your network or device.
By using it you accept full responsibility for any damage, data loss, or other
consequences. **The authors and contributors are not responsible or liable
for any loss or damage arising from its use.**

## License

GPL-3.0-only. See [LICENSE](LICENSE).

## Credits

Protocol work was informed by the open-source UniFi community, including
[rjmotion/pyunifiwire](https://github.com/rjmotion/pyunifiwire) discovery notes and
[danielwoz/ubiquiti-protect-onvif-event-listener](https://github.com/danielwoz/ubiquiti-protect-onvif-event-listener)
as a schema reference (its database-write approach is deliberately not used
here). The detection model and conversion recipe come from
[airockchip/rknn_model_zoo](https://github.com/airockchip/rknn_model_zoo).
