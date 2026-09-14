"""Object detection on generic x86 CPU (no NPU).

Runs an ONNX model directly via onnxruntime. Two output layouts are supported
and auto-detected, so either model works:

  * Ultralytics YOLOv5 ONNX export (default: models/yolov5n.onnx) -- a single
    ``[1, N, 5+num_classes]`` output with box decode (xywh, pixels) and
    sigmoid already baked into the graph.
  * Rockchip yolov5s_relu.onnx -- three raw ``[1,255,H,W]`` heads that need the
    anchor/grid decode (same math as the RK3588 variant).

Override the model with AIPORT_MODEL_PATH. Labels/anchors, the COCO->AI-Port
type mapping and the box/NMS math are kept identical to the RK3588 variant so
avclient.py needs no changes.
"""
import logging
import os
import threading

import numpy as np

log = logging.getLogger("aiport-detector-x86")

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(HERE, "models")
DEFAULT_MODEL = os.path.join(MODELS_DIR, "yolov5n.onnx")
MODEL_PATH = os.environ.get("AIPORT_MODEL_PATH") or DEFAULT_MODEL
LABELS_PATH = os.path.join(MODELS_DIR, "coco_80_labels_list.txt")
ANCHORS_PATH = os.path.join(MODELS_DIR, "anchors_yolov5.txt")

IMG_SIZE = (640, 640)  # (width, height), model's fixed input
OBJ_THRESH = 0.35
NMS_THRESH = 0.45

# Same three-category mapping as the RK3588 variant.
_VEHICLE_CLASSES = {"bicycle", "car", "motorbike", "aeroplane", "bus", "train", "truck", "boat"}
_ANIMAL_CLASSES = {"bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}


def _map_object_type(coco_label):
    label = coco_label.strip()
    if label == "person":
        return "person"
    if label in _VEHICLE_CLASSES:
        return "vehicle"
    if label in _ANIMAL_CLASSES:
        return "animal"
    return None


def _load_anchors():
    with open(ANCHORS_PATH) as f:
        values = [float(x.strip()) for x in f if x.strip()]
    pairs = list(zip(values[0::2], values[1::2]))
    return [pairs[0:3], pairs[3:6], pairs[6:9]]


def _load_labels():
    with open(LABELS_PATH) as f:
        return [line.strip() for line in f if line.strip()]


# --- Rockchip raw-head layout ([1,255,H,W] x3) -----------------------------
# _box_process through _post_process are copied verbatim from the RK3588
# detector.py; keep them unchanged for easy diffing.

def _box_process(position, anchors):
    grid_h, grid_w = position.shape[2:4]
    col, row = np.meshgrid(np.arange(0, grid_w), np.arange(0, grid_h))
    col = col.reshape(1, 1, grid_h, grid_w)
    row = row.reshape(1, 1, grid_h, grid_w)
    grid = np.concatenate((col, row), axis=1)
    stride = np.array([IMG_SIZE[1] // grid_h, IMG_SIZE[0] // grid_w]).reshape(1, 2, 1, 1)

    col = col.repeat(len(anchors), axis=0)
    row = row.repeat(len(anchors), axis=0)
    anchors = np.array(anchors).reshape(len(anchors), 2, 1, 1)

    box_xy = position[:, :2, :, :] * 2 - 0.5
    box_wh = pow(position[:, 2:4, :, :] * 2, 2) * anchors

    box_xy += grid
    box_xy *= stride
    box = np.concatenate((box_xy, box_wh), axis=1)

    xyxy = np.copy(box)
    xyxy[:, 0, :, :] = box[:, 0, :, :] - box[:, 2, :, :] / 2
    xyxy[:, 1, :, :] = box[:, 1, :, :] - box[:, 3, :, :] / 2
    xyxy[:, 2, :, :] = box[:, 0, :, :] + box[:, 2, :, :] / 2
    xyxy[:, 3, :, :] = box[:, 1, :, :] + box[:, 3, :, :] / 2
    return xyxy


def _filter_boxes(boxes, box_confidences, box_class_probs):
    box_confidences = box_confidences.reshape(-1)
    class_max_score = np.max(box_class_probs, axis=-1)
    classes = np.argmax(box_class_probs, axis=-1)
    pos = np.where(class_max_score * box_confidences >= OBJ_THRESH)
    scores = (class_max_score * box_confidences)[pos]
    return boxes[pos], classes[pos], scores


def _nms_boxes(boxes, scores):
    x = boxes[:, 0]
    y = boxes[:, 1]
    w = boxes[:, 2] - boxes[:, 0]
    h = boxes[:, 3] - boxes[:, 1]
    areas = w * h
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x[i], x[order[1:]])
        yy1 = np.maximum(y[i], y[order[1:]])
        xx2 = np.minimum(x[i] + w[i], x[order[1:]] + w[order[1:]])
        yy2 = np.minimum(y[i] + h[i], y[order[1:]] + h[order[1:]])
        w1 = np.maximum(0.0, xx2 - xx1 + 0.00001)
        h1 = np.maximum(0.0, yy2 - yy1 + 0.00001)
        inter = w1 * h1
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(ovr <= NMS_THRESH)[0]
        order = order[inds + 1]
    return np.array(keep)


def _nms_per_class(boxes, classes, scores):
    nboxes, nclasses, nscores = [], [], []
    for c in set(classes.tolist()):
        idx = np.where(classes == c)
        b, s = boxes[idx], scores[idx]
        keep = _nms_boxes(b, s)
        if len(keep):
            nboxes.append(b[keep])
            nclasses.append(np.full(len(keep), c))
            nscores.append(s[keep])
    if not nboxes:
        return None, None, None
    return np.concatenate(nboxes), np.concatenate(nclasses), np.concatenate(nscores)


def _post_process(outputs, anchors):
    boxes, scores, classes_conf = [], [], []
    reshaped = [o.reshape([len(anchors[0]), -1] + list(o.shape[-2:])) for o in outputs]
    for i, data in enumerate(reshaped):
        boxes.append(_box_process(data[:, :4, :, :], anchors[i]))
        scores.append(data[:, 4:5, :, :])
        classes_conf.append(data[:, 5:, :, :])

    def flatten(x):
        ch = x.shape[1]
        return x.transpose(0, 2, 3, 1).reshape(-1, ch)

    boxes = np.concatenate([flatten(b) for b in boxes])
    classes_conf = np.concatenate([flatten(c) for c in classes_conf])
    scores = np.concatenate([flatten(s) for s in scores])

    boxes, classes, scores = _filter_boxes(boxes, scores, classes_conf)
    if boxes.shape[0] == 0:
        return None, None, None
    return _nms_per_class(boxes, classes, scores)


# --- Ultralytics layout ([1,N,5+num_classes], decode baked in) -------------

def _xywh2xyxy(xywh):
    xyxy = np.copy(xywh)
    xyxy[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
    xyxy[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
    xyxy[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
    xyxy[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
    return xyxy


def _post_process_ultralytics(output):
    pred = output[0]  # (N, 5+num_classes): xywh(px), obj, class scores
    obj = pred[:, 4]
    class_conf = pred[:, 5:]
    classes = np.argmax(class_conf, axis=1)
    scores = obj * class_conf[np.arange(class_conf.shape[0]), classes]
    keep = scores >= OBJ_THRESH
    if not np.any(keep):
        return None, None, None
    boxes = _xywh2xyxy(pred[keep, :4])
    return _nms_per_class(boxes, classes[keep], scores[keep])


class Detector:
    """One shared onnxruntime InferenceSession behind a lock.

    Keeps detect(frame_rgb) -> result shape identical to the RK3588
    variant so avclient.py needs no changes.
    """

    def __init__(self):
        try:
            import onnxruntime as ort  # imported here so tests don't need it
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime is not installed -- pip install onnxruntime, or run "
                "the x86 Docker image (see piport/x86/README.md)") from exc
        self._lock = threading.Lock()
        self._labels = _load_labels()
        self._anchors = _load_anchors()
        # CPU-only by design; use ["CUDAExecutionProvider",
        # "CPUExecutionProvider"] if onnxruntime-gpu is swapped in.
        self._session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        # Ultralytics' release ONNX is float16; honour whatever the graph wants.
        self._in_dtype = np.float16 if "float16" in inp.type else np.float32
        outs = self._session.get_outputs()
        shapes = [o.shape for o in outs]
        # One [1,N,5+classes] output => Ultralytics (decode baked in);
        # otherwise the three Rockchip raw heads.
        if len(outs) == 1 and len(shapes[0]) == 3 and shapes[0][-1] == 5 + len(self._labels):
            self._mode = "ultralytics"
        else:
            self._mode = "raw_heads"
        log.info("Detector initialized (%s, mode=%s, outputs=%s, providers=%s)",
                  MODEL_PATH, self._mode, shapes, self._session.get_providers())

    def detect(self, frame_rgb):
        """frame_rgb: HxWx3 uint8 RGB, already resized to IMG_SIZE (plain
        stretch, no letterboxing). Returns {"objectType", "score", "box":
        (x1,y1,x2,y2)} in IMG_SIZE pixel space, sorted by score descending.
        """
        # NHWC uint8 -> NCHW float [1,3,H,W], normalized to [0,1].
        chw = frame_rgb.transpose(2, 0, 1).astype(np.float32) / 255.0
        batched = chw[np.newaxis, ...].astype(self._in_dtype)
        with self._lock:
            outputs = self._session.run(None, {self._input_name: batched})
        outputs = [np.asarray(o, dtype=np.float32) for o in outputs]
        if self._mode == "ultralytics":
            boxes, classes, scores = _post_process_ultralytics(outputs[0])
        else:
            boxes, classes, scores = _post_process(outputs, self._anchors)
        if boxes is None:
            return []
        results = []
        for box, cls, score in zip(boxes, classes, scores):
            coco_label = self._labels[int(cls)] if int(cls) < len(self._labels) else None
            mapped = _map_object_type(coco_label) if coco_label else None
            if not mapped:
                continue
            x1, y1, x2, y2 = [float(v) for v in box]
            results.append({"objectType": mapped, "score": float(score), "box": (x1, y1, x2, y2)})
        results.sort(key=lambda r: -r["score"])
        return results
