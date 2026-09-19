# Credits

Third-party projects this repository references, derives from, or was informed
by. Nothing here is vendored into the repo; see `LICENSE` for this project's own
license.

## Detection model & conversion

- **[Ultralytics YOLOv5](https://github.com/ultralytics/yolov5)** (AGPL-3.0) —
  the detection architecture. The converted `.rknn` is an AGPL-3.0 derivative of
  YOLOv5s, so redistributed model binaries carry AGPL-3.0, the same license as
  this project.
- **[airockchip/rknn_model_zoo](https://github.com/airockchip/rknn_model_zoo)**
  (Apache-2.0, tag `v2.3.2`) — the ONNX export, anchors, COCO labels, calibration
  subset and the conversion recipe used by `rknn_convert/`.

## Protocol research

- **[rjmotion/pyunifiwire](https://github.com/rjmotion/pyunifiwire)** — UBNT L2
  discovery field/TLV notes.
- **[danielwoz/ubiquiti-protect-onvif-event-listener](https://github.com/danielwoz/ubiquiti-protect-onvif-event-listener)**
  — event/schema reference. Its direct database-write approach is deliberately
  **not** used here; this project is protocol-only.

## Inspiration

- **[dciancu/unifi-protect-unvr-docker-arm64](https://github.com/dciancu/unifi-protect-unvr-docker-arm64)**
  — the project that got this author into UniFi tinkering, and whose methods
  helped in understanding how parts of the ecosystem work.
