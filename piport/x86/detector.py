"""Object detection on generic x86 CPU (no NPU).

Runs the pre-conversion yolov5s_relu.onnx directly via onnxruntime instead
of the RKNN path. Labels, anchors, mapping and box-decode/NMS math are
copied verbatim from the RK3588 variant; only the input layout (NCHW
float32, explicit /255) and loading glue differ.
"""
import logging
import os
import threading

import numpy as np
import onnxruntime as ort

log = logging.getLogger("aiport-detector-x86")

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(HERE, "models")
MODEL_PATH = os.path.join(MODELS_DIR, "yolov5s_relu.onnx")
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

    nboxes, nclasses, nscores = [], [], []
    for c in set(classes):
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


class Detector:
    """One shared onnxruntime InferenceSession behind a lock.

    Keeps detect(frame_rgb) -> result shape identical to the RK3588
    variant so avclient.py needs no changes.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._labels = _load_labels()
        self._anchors = _load_anchors()
        # CPU-only by design; use ["CUDAExecutionProvider",
        # "CPUExecutionProvider"] if onnxruntime-gpu is swapped in.
        self._session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        log.info("Detector initialized (%s, providers=%s)",
                  MODEL_PATH, self._session.get_providers())

    def detect(self, frame_rgb):
        """frame_rgb: HxWx3 uint8 RGB, already resized to IMG_SIZE (plain
        stretch, no letterboxing). Returns {"objectType", "score", "box":
        (x1,y1,x2,y2)} in IMG_SIZE pixel space, sorted by score descending.
        """
        # NHWC uint8 -> NCHW float32 [1,3,H,W], normalized to [0,1].
        chw = frame_rgb.transpose(2, 0, 1).astype(np.float32) / 255.0
        batched = chw[np.newaxis, ...]
        with self._lock:
            outputs = self._session.run(None, {self._input_name: batched})
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
