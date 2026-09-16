# Pi Port -- a DIY UniFi Protect "AI Port" on an Orange Pi (RK3588)

Pi Port is a reverse-engineering / interoperability proof of concept that makes
a stock **Orange Pi 5 Plus (RK3588)** running Linux present itself to a real
UniFi Protect console as a genuine **UniFi Protect AI Port** (catalog model
`UVC AI Port`, sysid `0xa5f1`): L2 discovery, adoption, camera pairing, video
relay, real NPU object detection, and dashboard detection events with
thumbnails.

It talks the vendor wire protocol only. It does not modify firmware, does not
write to the controller's database, and ships no Ubiquiti code or binaries --
you supply your own console, cameras, and (for reverse engineering) firmware.

## What it is

- A working emulator of the AI Port device role, confirmed live against a real
  UniFi Protect console: discovery, adoption, camera pairing, video relay, NPU
  motion detection, dashboard events with thumbnails, and automatic un-adopt
  detection, with real cameras paired and streamed concurrently.
- A reference for the AI Port protocol, reverse-engineered from firmware on
  hardware the author owns and documented in `piport/README.md`.
- A hobbyist/PoC project with an x86/no-NPU Docker variant for anyone without
  Rockchip hardware.

## What it is not

- **Not a product, and not production software.** It is a proof of concept.
- **Not affiliated with, endorsed by, or connected to Ubiquiti Inc.** "UniFi",
  "UniFi Protect" and "AI Port" are trademarks of Ubiquiti Inc.
- **Not the original AI Port.** It does not run Ubiquiti firmware, does not use
  Ubiquiti's own detection model, and does not provide the real device's
  feature set -- see *How this differs from the real AI Port* below.
- **Not a way to target other people's systems.** It is intended for
  interoperability and personal use on hardware and consoles you own.

## How this differs from the real AI Port

| | Real AI Port | Pi Port |
|---|---|---|
| Hardware | Ubiquiti AI Port (Ambarella) | Orange Pi 5 Plus (RK3588), or x86 in Docker |
| Firmware | Ubiquiti, signed | none; this repo's Python |
| Detection model | Ubiquiti's own classifier | stock YOLOv5 (person/vehicle/animal) |
| Output | full Ubiquiti product behaviour | the subset this project implements |

It is a protocol-compatible stand-in built on the shared camera/chime
middleware, not a clone of the device.

## AI-assisted development

Large parts of this project were produced with LLMs (Claude Code and DeepSeek)
under human supervision, review, and real-hardware testing. Verify anything you
rely on. See *Disclaimer*.

## Repository layout

```
piport/            the emulator (RK3588 / Orange Pi)
  discovery.py       UDP/10001 UBNT discovery responder
  http_api.py        HTTPS :443 adopt/control API
  ucp4_client.py     generic ucp4 device-management websocket
  avclient.py        pairing/streaming/smart-detect websocket + detection
  detector.py        YOLOv5 inference wrapper (RKNN / NPU)
  sysinfo_server.py  dev-box CPU/mem/GPU/NPU status page (+ webui.html)
  models/            anchors + COCO labels (model binaries fetched, not stored)
  instance_manager.py / run_all.py  multi-instance orchestration
  x86/               x86/no-NPU Docker variant (onnxruntime on CPU)
scripts/           install.sh (one-shot installer), fetch_models.py, helpers
rknn_convert/      YOLOv5s -> RKNN conversion recipe (x86 build host)
```

Model binaries (`.onnx`/`.rknn`) and all Ubiquiti firmware are **not** stored
here; see *Model assets* below. `piport/README.md` has the protocol detail and
per-component notes; `piport/x86/README.md` covers the Docker variant.

## Requirements

- Orange Pi 5 Plus (or another RK3588 board) running Linux, with root -- the
  emulator binds UDP/10001 and TCP/443.
- Python 3, and `rknn-toolkit-lite2` on the board for NPU inference.
- A UniFi Protect console you own and control on the same LAN.

The x86 variant needs Docker and runs `onnxruntime` on CPU instead of the NPU;
no Rockchip hardware required.

## Install (RK3588 / Orange Pi)

On the board itself, `scripts/install.sh` does the whole stand-up: system
packages, a venv + dependencies, model assets, the required `librknnrt.so`
upgrade, a reboot-surviving systemd instance (via `instance_manager.py`), and
the web UI:

```bash
sudo scripts/install.sh --console <UNVR-IP> [--parent-iface <iface>]
```

- Run from the checkout root, as root. `--parent-iface` defaults to the
  auto-detected default-route NIC.
- The NPU model cannot be built on the Pi (the RKNN converter is x86_64-only).
  Build it on an x86 host with `scripts/fetch_models.py --convert`, or let the
  installer fetch it from `--rknn-url` / `PIPORT_RKNN_URL` / its default URL.
  `--rknn <path>` takes a local build, and a local `models-rknn/` submodule is
  also detected. Use `--no-npu` for a CPU-only protocol-stack install.
- Opens the web UI on `:8090` (`--webui-port` to change) for board stats and
  instance management.

### Manual install

The installer automates the steps below; run them by hand if you prefer:

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

## Use

1. Adopt "AI Port" from the Protect console UI like any camera.
2. Pair a real camera to it from the camera's own settings page.
3. Detections then appear as dashboard events with thumbnails, and the
   console's own person/vehicle/animal filters work.

`run_all.py` needs root (binds UDP/10001 and TCP/443) and must be able to send
L2 broadcast/multicast on the LAN, so run it on the host, not in a NAT'd
container. It resolves its own interface's IP by default, so multiple instances
on one box don't collide on `:443`.

### Single instance (RK3588)

Run it directly (`sudo python3 piport/run_all.py --iface <iface>`), or under
systemd for reboot survival. A minimal unit:

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
macvlan sub-interface, MAC, and DHCP lease -- a distinct L2 device, so the
console sees independent AI Ports:

```bash
sudo python3 piport/instance_manager.py create lab2 --parent-iface <iface>
python3 piport/instance_manager.py list
sudo python3 piport/instance_manager.py destroy lab2
```

Identity (MAC/device-id) is generated once at creation and baked into the unit,
so it is stable across reboots. `create --ip X.Y.Z.W` uses a static address
instead of DHCP. The web UI (`sysinfo_server.py`) can also create/destroy
instances.

### x86 / Docker variant

```bash
cd piport/x86
python3 ../../scripts/fetch_models.py       # -> x86/models/yolov5n.onnx
./run-smoke-test.sh                          # fetch + build + start + verify

# manual equivalent:
docker build -t piport-x86 .
docker run --rm --cap-add=NET_ADMIN --network host piport-x86
```

Only `detector.py` (onnxruntime on CPU) and `requirements.txt` differ from the
RK3588 tree. `docker-compose.yml` is the single-instance host-network test;
`docker-compose.macvlan.yml` is a multi-instance sketch (macvlan, not the
default bridge, which breaks L2 discovery). See `piport/x86/README.md`.

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
| `yolov5s_relu.onnx` (27.6 MiB) | RKNN conversion (`.rknn`) | Rockchip model zoo delivery: <https://ftrg.zbox.filez.com/v2/delivery/data/95f00b0fc900458ba134f8b180b3f7a1/examples/yolov5/yolov5s_relu.onnx> |
| anchors, COCO labels, bus.jpg, calibration subset | both | <https://github.com/airockchip/rknn_model_zoo> (tag `v2.3.2`) |

The x86 variant defaults to `yolov5n.onnx`; it still accepts the Rockchip
`yolov5s_relu.onnx` via `AIPORT_MODEL_PATH` (both output layouts are
auto-detected). The Ultralytics model is AGPL-3.0, same as the weights.

`yolov5s_relu.rknn` is produced on an x86_64 machine with `rknn-toolkit2`
(install from PyPI or the Rockchip GitHub release -- see
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
work, the same license as this project. If you redistribute it, include the
AGPL-3.0 text and attribution; the corresponding source is this repo's
conversion recipe plus the ONNX in the table above. Check those licenses
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
LLMs under human supervision and testing -- review and verify everything
yourself. This software is provided "as is", without warranty of any kind. It
binds privileged ports, manipulates network interfaces (macvlan/DHCP), and
runs detection hardware; misconfiguration can disrupt your network or device.
By using it you accept full responsibility for any damage, data loss, or other
consequences. **The authors and contributors are not responsible or liable
for any loss or damage arising from its use.**

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE). Because this is a network service,
anyone who runs a modified version for others to interact with over a network
must offer them the corresponding source.

## Credits

Protocol work was informed by the open-source UniFi community, including
[rjmotion/pyunifiwire](https://github.com/rjmotion/pyunifiwire) discovery notes
and
[danielwoz/ubiquiti-protect-onvif-event-listener](https://github.com/danielwoz/ubiquiti-protect-onvif-event-listener)
as a schema reference (its database-write approach is deliberately not used
here). The detection model and conversion recipe come from
[airockchip/rknn_model_zoo](https://github.com/airockchip/rknn_model_zoo).
