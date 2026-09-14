# Pi Port — a DIY UniFi Protect "AI Port" on Orange Pi 5 Plus / RK3588

**STATUS: the full pipeline works live against a real UniFi Protect
console, including "Filter by Person"** — discovery, adoption, camera
pairing, video relay, real NPU motion detection, dashboard events with
thumbnails, and automatic un-adopt detection are all confirmed live
(3 real cameras paired and stress-tested concurrently). The "Filter by
Person" fix was populating `descriptors[].zones` with the real zone ID(s) —
not the `RequestAI`/reverification theory, which was ruled out as
AI-Key-only.

This README is the condensed "how do I stand this up / how does it work"
summary for this component. The project root's `README.md` has the overview,
setup, and licensing.

## Real protocol (confirmed live, not guessed)

AI Port turned out to share the exact same firmware middleware as real
UniFi cameras (`ubnt_ctlserver`, `ubnt_avclient`) rather than a bespoke
stack — so its protocol is the already-solved camera/chime protocol,
plus two AI-Port-specific additions.

1. **UDP/10001 discovery** — the classic UBNT L2 discovery protocol. The
   `platform` TLV (`0x0C`) must exactly match the controller's catalog
   string: **`"UVC AI Port"`**, sysid **`0xa5f1`**.
2. **HTTPS control API on the device, port 443** (not 8080 — that was AI
   Key's bespoke server; AI Port runs the real shared camera-firmware
   `ubnt_ctlserver`, whose port is 443):
   - `POST /api/1.2/manage` — the real adopt endpoint (confirmed live;
     `/api/adopt` is NOT used for AI Port). Body is nested:
     `{"username","password","mgmt":{"username","password","hosts","token","protocol","mode","nvr","controller","consoleId","consoleName"}}`.
   - `POST /api/1.2/login` — controller logs in here first (any
     `Set-Cookie` accepted) before calling most other `api/1.2/<action>`
     endpoints.
   - `GET /api/1.2/status` — returns `{fw, board:{hwaddr}, features:{...}}`.
     This is a **camera** capability-report endpoint (used by
     `getFeatureFlags`/`requestFeatureFlags` for un-paired cameras) — kept
     implemented since a real AI Port likely answers it too, but it turned
     out NOT to be the mechanism for AI Port's own capability declaration
     (see step 4 below).
   - `POST /api/1.2/snapshot` — serves a still image.
3. **Outbound mTLS WebSocket #1 — `ucp4` binary envelope**, to
   `<console-ip>:7442` (same 8-byte-header + JSON envelope as chime/AI Key).
   Handles adoption bookkeeping and generic RPCs: `getInfo`,
   `networkStatus`, `sshService`, etc. **Does NOT carry camera-specific
   commands at all** — `registerDeviceWebSocket` (server-side) only adds a
   connection to the camera-command routing pool for connections that do
   **not** negotiate the `ucp4` subprotocol, which is why pairing needs a
   second connection (next point).
4. **Outbound mTLS WebSocket #2 — the classic pre-`ucp4` avclient
   protocol**, same URL/port, headers `camera-mac`/`camera-model`
   (`camera-model` must be the literal sysid string `"0xa5f1"`, not
   `"UVC AI Port"`), subprotocol `secure_transfer`. **Wire format is plain
   JSON sent as BINARY websocket frames** (not text — a real gotcha, see
   `avclient.py`'s docstring). This connection carries:
   - `UiStreamControl` — the pairing/streaming control message. Reply
     `{"status":"started"|"stopped","usedPoints":N}` — `usedPoints` MUST
     be included on every reply (start AND stop) or the controller's
     camera-capacity bookkeeping breaks after 2+ pair/unpair cycles.
   - `EventSmartDetect` — device→controller push for smart detection. Real
     confirmed shape (byte-for-byte from a live controller-side capture):
     top-level `descriptors` array (not `tracks`), each with `objectType`,
     `coord: [x,y,w,h]` (flat array, not nested), `trackerID` (capital ID),
     `confidenceLevel`. Top-level `edgeType` is a real per-track state
     machine: `"enter"` opens a real dashboard event, `"moving"` updates it
     while the track stays open, `"leave"` closes it and triggers a
     thumbnail upload. **`"none"` alone opens nothing** — this took two
     rounds of live iteration against the controller's own Rust
     (`ds`/"Device Service") debug log to pin down; see the summary doc's
     "FRONTIER REACHED" / milestone entries for the full story of how
     `enter`/`leave` were confirmed via `ds`'s own log lines
     (`SmartDetectHandler::handle_object_enter/leave()`).
   - `EventFeatureFlagsUpdated` — device→controller push for hardware
     capability declaration (**the real mechanism** for
     `featureFlags.smartDetectTypes`/`hasMotionDetection` — see "Real
     object detection" below for how this connects to the NPU classifier).
   - `GetRequest` (`what: "snapshot"` or `"smartDetectZoneSnapshot"`) —
     asks for a real JPEG, uploaded as `multipart/form-data` to whatever
     `uri` the request gives (two different ports are used for the two
     variants — both already handled the same way in code).

## Components

- **`config.py` + `aiport.cfg`** — shared configuration + device identity
  loader (see "Configuration" below). All four protocol scripts read the same
  file; CLI flags override cfg values.
- **`discovery.py`** — UDP/10001 discovery responder, `platform="UVC AI Port"`,
  `sysid=0xa5f1`.
- **`http_api.py`** — HTTPS `:443` server: `/api/1.2/manage` (adopt),
  `/api/1.2/login`, `/api/1.2/status`, `/api/1.2/snapshot`.
- **`ucp4_client.py`** — the generic `ucp4` device-management WebSocket client
  (adoption, `getInfo`, etc).
- **`avclient.py`** — the classic per-camera avclient WebSocket client:
  pairing (`UiStreamControl`), real motion detection + classification,
  `EventSmartDetect`/`EventFeatureFlagsUpdated` pushes, snapshot upload. This
  is where almost all of the real behavior lives.
- **`detector.py`** — wraps `RKNNLite` running a real YOLOv5s model on the
  RK3588 NPU. See "Real object detection" below.
- **`run_all.py`** — launches all of the above together.

## Configuration (`aiport.cfg`)

Device identity, networking, and the console endpoint are no longer
hardcoded in the scripts — they live in `aiport.cfg` (INI) next to
`run_all.py`, loaded through the shared `config.py` module. Every stage
script reads the same file, and any CLI flag overrides the cfg value (so
`--hostname lab2` etc. still work ad-hoc). Each script also accepts
`--config <path>` (point at a different file) and `--debug` (overrides
`[logging] debug`); `run_all.py` passes both through to its children.

```ini
[identity]
mac =
hostname = piport
platform = UVC AI Port
sysid = 0xa5f1
fw_version = 5.1.12.67
discovery_fw_version = aiport4G.mt8390.v5.1.12.67.emu.260904.0000
device_id = fe6488e7-7042-5bcb-ab86-6f0ad1a5baed
guid = 5c2659d4-48f3-16c4-ed83-2fefbad37066

[network]
iface =
parent_iface =
mode = dhcp
ip =
netmask = 255.255.255.0
gateway =
dns =

[console]
host = <UNVR-IP>
port = 7442

[logging]
debug = false
```

Key meanings (the checked-in `aiport.cfg` is fully annotated):
- `[identity]` — `mac` empty derives `FC:EC:DA:<iface's last 3 octets>`;
  `platform` must match the controller's catalog string; `device_id` must
  be unique per emulated instance. `fw_version` is the short version sent
  over ucp4/avclient/http, `discovery_fw_version` the longer L2 discovery
  string.
- `[network] mode` — `dhcp` reads the interface's live lease
  (`dhclient`/OS), `static` uses `ip`/`netmask`/`gateway`. For
  `instance_manager.py`, `create --ip X.Y.Z.W` (or `mode=static`) builds the
  macvlan with a static address via `ip addr add` instead of `dhclient`.
- `[console]` — the UNVR the avclient dials pre-adoption.
- `[logging] debug` — set true for debug-level logs (any script's `--debug`
  flag overrides it).

Only full-line `#` comments are supported — `configparser` does not strip
inline comments, so keep comments on their own line.

## Quick start on the Orange Pi

One-shot installer (system packages, venv, deps, model assets, `librknnrt.so`
upgrade, a reboot-surviving systemd instance and the web UI):

```bash
sudo ../scripts/install.sh --console <UNVR-IP> [--parent-iface <iface>]
```

The manual equivalent:

```bash
pip install -r requirements.txt
pip install rknn-toolkit-lite2   # see "Real object detection" below first

sudo python3 run_all.py --iface <your-iface> --fallback-host-port <console-ip>:7442
```

Device identity and the console endpoint default from `aiport.cfg` — edit
it (or pass `--config`) before the first run; see "Configuration" above.

Then adopt "AI Port" from the Protect console UI like a normal camera,
and pair a real camera to it from the camera's own settings page. Watch
`run_all.log` — discovery probes, the `/api/1.2/manage` POST, both
WebSocket connections, then `UiStreamControl` once you pair a camera.

**Operational gotchas** (all confirmed live this session):
- `pkill -f 'run_all.py'` can **kill itself** — the pattern string is
  present in the very command line doing the killing. Use
  `pkill -9 -f '[r]un_all.py'` (bracket around one letter defeats the
  self-match) or kill by literal PID.
- Killing the stack with `SIGKILL` doesn't let it clean up its own ffmpeg
  child processes (the per-camera RTSP pull/decode) — they become orphaned
  and keep running, holding an RTSP session, invisible to the next run's own
  tracking. Always also run
  `pkill -9 -f '[f]fmpeg.*rtsp://<console-ip>:7447'` before relaunching.
- The Orange Pi's OS doesn't wire this stack into boot startup — if the
  Pi reboots (power loss, manual reboot), `run_all.py` needs to be
  relaunched manually.

## Real object detection (RK3588 NPU)

The motion/smart-detect pipeline runs a real, quantized YOLOv5s model on
the Orange Pi's NPU — not a placeholder. Getting this working required a
few real, non-obvious steps; **read this before touching `detector.py` or
`models/`**.

The model binaries are not committed to this repo. Fetch them (ONNX, anchors,
COCO labels, calibration subset) and optionally build the `.rknn` with:

```bash
python3 ../scripts/fetch_models.py            # download assets
python3 ../scripts/fetch_models.py --convert  # build .rknn (x86 + rknn-toolkit2)
```

### Why the model has to be converted on a separate x86 machine

`rknn-toolkit-lite2` is the on-device, inference-only Python API
(`pip install rknn-toolkit-lite2`; its PyPI wheels are aarch64-only for
Python <= 3.12). It can only **run** an already-converted `.rknn` file; it
cannot convert a plain ONNX model. Conversion needs the full
**`rknn-toolkit2`** package, which runs on an x86_64 build host (also on
PyPI, or as a wheel from `https://github.com/airockchip/rknn-toolkit2`).
Use the **same version** as the `rknn-toolkit-lite2` you run on the Pi —
the two must be format-compatible.

```bash
# On any x86_64 Linux box (a throwaway venv is fine — this tooling is
# never deployed anywhere, only its OUTPUT, the .rknn file, is):
pip install "setuptools<81" "onnx==1.16.1"  # see gotchas below
pip install rknn-toolkit2==2.3.2
pip install onnx==1.16.1  # rknn-toolkit2's own install may upgrade onnx; repin after

# Real UBNT model files (TensorRT .engine / Ambarella "cavalry" bundles,
# found elsewhere in this project) are NOT usable here -- they're
# compiled for completely different NPU/GPU architectures. Use an open
# model instead. Rockchip's own model zoo ships a ready ONNX + working
# conversion recipe:
curl -L -o yolov5s_relu.onnx \
  "https://ftrg.zbox.filez.com/v2/delivery/data/95f00b0fc900458ba134f8b180b3f7a1/examples/yolov5/yolov5s_relu.onnx"
# plus the anchors file, COCO labels, and a 20-image calibration subset --
# all from https://github.com/airockchip/rknn_model_zoo (v2.3.2),
# examples/yolov5/model/ and datasets/COCO/.

python3 convert.py yolov5s_relu.onnx rk3588 yolov5s_relu.rknn
# (convert.py: rknn.config(target_platform="rk3588") -> load_onnx ->
#  build(do_quantization=True, dataset=<calibration list>) -> export_rknn)
```

**Two real environment bugs hit during conversion**, both fixed by
pinning versions:
- `ModuleNotFoundError: No module named 'pkg_resources'` — `setuptools`
  >= 81 dropped it entirely. Fix: `pip install "setuptools<81"`.
- `AttributeError: module 'onnx' has no attribute 'mapping'` — the latest
  `onnx` (1.22.0) removed a module `rknn-toolkit2`'s internals still use.
  Fix: `pip install onnx==1.16.1` (satisfies rknn-toolkit2's own
  `>=1.16.1` requirement while still having `mapping`).

`fetch_models.py` places `yolov5s_relu.rknn`, `anchors_yolov5.txt` and
`coco_80_labels_list.txt` in `models/` next to `detector.py` on the Pi.

### The system `librknnrt.so` upgrade (required)

The Orange Pi's stock OS image ships an old `/usr/lib/librknnrt.so`
(runtime 1.4.0, from 2022) that **cannot load models in the newer format**
`rknn-toolkit2==2.3.2` produces (`Invalid RKNN model version 6`).
`rknn-toolkit-lite2` does not bundle its own copy — it uses whatever the
system provides. Fix (back up the original first):

```bash
cp /usr/lib/librknnrt.so /root/librknnrt.so.orig-backup
curl -L -o /root/librknnrt_new.so \
  "https://raw.githubusercontent.com/airockchip/rknn-toolkit2/v2.3.2/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so"
cp /root/librknnrt_new.so /usr/lib/librknnrt.so
ldconfig
```

After this, `RKNNLite().init_runtime()` should log
`librknnrt version: 2.3.2` (matching the toolkit version) instead of the
old `1.4.0`.

### One more real bug (already fixed in `detector.py`)

`RKNNLite.inference()` needs an explicit batch dimension —
`frame[np.newaxis, ...]` (shape `(1,H,W,3)`), not just `(H,W,3)`, or it
fails with `"need 4dims input, but 3dims input buffer feed"`.

### Sanity-check the model works at all

Before wiring it into the live pipeline, test against Rockchip's own
reference image (also in the model zoo, `examples/yolov5/model/bus.jpg`)
— should detect 3 people + 1 bus at 0.70+ confidence. If this doesn't
work, nothing downstream will either.

### How detection feeds into the real dashboard events

`avclient.py` runs **one** ffmpeg process per paired camera that
pulls the RTSP stream and uses `filter_complex split=2` to produce both
outputs from a single decode: a 1/2-fps JPEG still (for snapshots) and
2-fps 640×640 raw RGB frames piped to Python for classification — no
JPEG/PNG/pixel-decode library needed on the Python side. A background loop
classifies the latest frame every 1.5s (`MOTION_POLL_INTERVAL_S`): a
detection opens (`"enter"`) or refreshes (`"moving"`) a track, and a track
closes (`"leave"`) only after `MOTION_MISS_TOLERANCE_TICKS` (20, ~30s) of
**consecutive** empty ticks. Multiple simultaneous objects are tracked
independently, matched frame-to-frame by bounding-box IOU within each
object type.

**Two real bugs worth knowing about if you touch this**:
- The first version gated ticks on an ffmpeg `scene` (frame-diff) filter
  instead of classifying continuously — confirmed live that this is
  unreliable (a real walk-by produced zero scene-diff ticks while the
  camera's own onboard motion detection fired normally in the same window).
  The classifier now runs on a plain timer; the NPU inference is fast/cheap
  enough that a pre-filter wasn't buying anything.
- The leave-debounce must only count *consecutive* misses (reset on any real
  detection), never fire unconditionally on every tick — otherwise a track
  can never close once ticking is time-driven rather than event-driven.

### Capability declaration — the real, non-obvious final piece

A detection creating a real dashboard event with a real thumbnail (this
all works) is **not enough** for the event to be filterable by object
type in the UI. That additionally requires the camera's own
`featureFlags.smartDetectTypes`/`hasMotionDetection` to be populated —
and confirmed, live, that this does NOT come from `/api/1.2/status`
(that endpoint is for un-paired cameras' own capability refresh, an
unrelated code path). The real mechanism, found by reading the
controller's `service.js` directly: send **`EventFeatureFlagsUpdated`**
over the classic avclient connection (device→controller push, not a
reply to anything):

```json
{"deviceID": "<paired camera mac>",
 "smartDetect": ["person", "vehicle", "animal"],
 "motionDetect": ["stable"],
 "mic": true, "speaker": true, "ledStatus": true}
```

The controller resolves the paired camera the same way it resolves
`EventSmartDetect`'s target, writes these into the AI Port's own
`featureFlagsMap[cameraMac]`, and copies the overridable subset onto the
camera's own `featureFlags` on the next reconnect pass — confirmed live
by querying the controller's own Postgres directly (read-only) and seeing
`smartDetectTypes`/`hasMotionDetection` update on the real camera row.

## Known open questions / next steps

- Live verification of multi-object tracking with a genuinely simultaneous
  different-type scene (only the single-object path has been re-confirmed
  live).
- Line Crossing support is implemented but its event/capability shapes have
  not been tested against a live controller.
- The classifier is generic motion/object detection (person/vehicle/animal
  via stock YOLOv5s), not the real UBNT second-pass classifier
  (`second_verifier_mlabel`) the real hardware runs — reasonable, since real
  UBNT model weights are not portable to this NPU.

## Safety / scope note

This only talks to hardware and a console **you own**, using vendor
protocol details reverse-engineered from **your own** firmware, purely to
interoperate with your own UniFi Protect install. Nothing here targets
third-party systems.
