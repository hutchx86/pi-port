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
scripts/           deployment helpers + fetch_models.py (model assets)
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
python3 ../scripts/fetch_models.py          # x86/models/yolov5s_relu.onnx
docker build -t piport-x86 .
docker run --rm --cap-add=NET_ADMIN --network host piport-x86 --iface eth0
```

For multiple instances use `docker-compose.yml` (macvlan) rather than the
default bridge, which breaks L2 discovery. **The Dockerfile has not been built
by the author (no Docker daemon available at time of writing) — treat the
first build as its first test.** See `piport/x86/README.md`.

### Operational notes

- Never `pkill -f 'run_all.py'`: the pattern matches the killing command
  itself. Use `pkill -9 -f '[r]un_all.py'`, and separately kill orphaned
  ffmpeg children (`pkill -9 -f '[f]fmpeg.*rtsp://<console-ip>:7447'`).
- To start detached over SSH, use `setsid -f` (a backgrounded `nohup` can hang
  the SSH channel).

## Model assets (not tracked)

The detector needs an open YOLOv5s model. Because it is a large binary and is
reproducible, it is not committed; `scripts/fetch_models.py` downloads it:

| Asset | Source |
|---|---|
| `yolov5s_relu.onnx` | Rockchip model zoo delivery: <https://ftrg.zbox.filez.com/v2/delivery/data/95f00b0fc900458ba134f8b180b3f7a1/examples/yolov5/yolov5s_relu.onnx> |
| anchors, COCO labels, bus.jpg, calibration subset | <https://github.com/airockchip/rknn_model_zoo> (tag `v2.3.2`) |

`yolov5s_relu.rknn` is produced on an x86_64 machine with `rknn-toolkit2`
(install from PyPI or the Rockchip GitHub release — see
`rknn_convert/convert.py` and `piport/README.md`):

```bash
python3 scripts/fetch_models.py --convert
```

The model derives from Ultralytics YOLOv5 (AGPL-3.0) via Rockchip's model zoo;
check those licenses before redistributing it.

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
