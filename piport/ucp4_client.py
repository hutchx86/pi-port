#!/usr/bin/env python3
"""Outbound mTLS WebSocket ("ucp4") client for the AI Port emulator.

8-byte header + JSON envelope; AI Port uses the real camera ubnt_avclient
codebase, so AI-Key-specific handlers don't apply. Unknown actions are acked
with `{}` and logged so the controller's errors.log can reveal real shapes.
"""
import argparse
import json
import logging
import os
import ssl
import struct
import sys
import threading
import time
import uuid

from websockets.sync.client import connect

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

log = logging.getLogger("aiport-ucp4-client")

_PROCESS_START_MS = int(time.time() * 1000)

HERE = os.path.dirname(os.path.abspath(__file__))
ADOPT_STATE_FILE = os.path.join(HERE, "adopt_state.json")
CERT_PATH = os.path.join(HERE, "server.crt")
KEY_PATH = os.path.join(HERE, "server.key")


def pack_record(record_type: int, payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    header = struct.pack(">BB4sH", record_type, 1, b"\x00\x00\x00\x00", len(body))
    return header + body


def pack_message(head: dict, body: dict) -> bytes:
    return pack_record(1, head) + pack_record(2, body)


def unpack_all_records(data: bytes):
    records = []
    off = 0
    while off < len(data):
        if len(data) - off < 8:
            log.warning("trailing %d bytes, not enough for a record header", len(data) - off)
            break
        rtype, version, reserved, length = struct.unpack(">BB4sH", data[off:off + 8])
        off += 8
        payload = data[off:off + length]
        off += length
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            obj = {"_raw": payload.decode(errors="replace")}
        records.append((rtype, version, obj))
    return records


def wait_for_adopt_state(fallback_host_port, poll_interval=1.0, fallback_after=10.0):
    log.info("waiting for %s to appear (written by http_api.py on a successful adopt POST)...",
              ADOPT_STATE_FILE)
    waited = 0.0
    while True:
        if os.path.exists(ADOPT_STATE_FILE):
            try:
                with open(ADOPT_STATE_FILE) as f:
                    state = json.load(f)
                if state.get("hosts"):
                    return state
            except (OSError, json.JSONDecodeError):
                pass
        if fallback_host_port and waited >= fallback_after:
            log.info("no adopt POST seen after %.0fs -- falling back to tokenless reconnect "
                      "to %s (device may already be adopted server-side)",
                      waited, fallback_host_port)
            return {"hosts": [fallback_host_port], "token": None}
        time.sleep(poll_interval)
        waited += poll_interval


def build_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    return ctx


def handle_action(head, body, device_info):
    action = head.get("action")
    log.info("<<< controller action=%r head=%s body=%s", action, head, body)

    if action == "getInfo":
        # Controller getInfo schema (service.js); an empty fwVersion disables
        # firmware-gated features, and a non-null hwrev beats an empty column.
        now_ms = int(time.time() * 1000)
        return {
            "mac": device_info["mac_nosep"],
            "type": device_info["type"],
            "fwVersion": device_info["version"],
            "version": device_info["version"],
            "hwrev": 1,
            # Read live so ResetToDefaults can't leave this claiming still-adopted.
            "adopted": _read_current_token() is not None,
            "guid": device_info["guid"],
            "deviceId": device_info["device_id"],
            "ip": device_info["ip"],
            "connectionHost": device_info["ip"],
            "connectionSecurePort": 443,
            "uptime": (now_ms - _PROCESS_START_MS) // 1000,
            "upSince": _PROCESS_START_MS,
            "protocolVersion": 1,
            "poeType": "unknown",
        }
    if action == "networkStatus":
        return {"ip": device_info["ip"], "connected": True}
    if action == "changeUserPassword":
        log.info("changeUserPassword requested (not persisted in this emulator)")
        return {}

    # Ack `{}` so the controller's RPC doesn't hang; log it for shape discovery.
    return {}


def _read_current_token():
    if not os.path.exists(ADOPT_STATE_FILE):
        return None
    try:
        with open(ADOPT_STATE_FILE) as f:
            return json.load(f).get("token")
    except (OSError, json.JSONDecodeError):
        return None


def _watch_for_reconnect(ws, this_connection_token, stop_event, poll_interval=2.0):
    while not stop_event.is_set():
        time.sleep(poll_interval)
        current = _read_current_token()
        # Any token change (including clearing) makes this connection's
        # X-Adopted stale, so tear it down.
        if current != this_connection_token:
            log.info("adopt token changed (%s -> %s) -- closing this connection "
                      "to force a fresh one", this_connection_token, current)
            try:
                ws.close()
            except Exception:
                pass
            return


def run(host: str, port: int, token: str, send_token_header: bool, device_info: dict,
        adopted: bool = True):
    device_info = {**device_info, "console_host": host}
    url = f"wss://{host}:{port}/"
    headers = {
        "X-Ident": device_info["mac_nosep"],
        "X-Mode": "0",
        "X-Type": device_info["type"],
        "X-Adopted": "true" if adopted else "false",
        "X-Ip": device_info["ip"],
        "X-Device-Id": device_info["device_id"],
        "X-Version": device_info["version"],
        "X-Guid": device_info["guid"],
    }
    if send_token_header:
        headers["X-Token"] = token

    log.info("connecting to %s with headers=%s", url, headers)
    ctx = build_ssl_context()

    with connect(url, subprotocols=["ucp4"], additional_headers=headers,
                 ssl=ctx, open_timeout=15) as ws:
        log.info("WSS CONNECTED (subprotocol=%s)", ws.subprotocol)

        stop_watch = threading.Event()
        watcher = threading.Thread(target=_watch_for_reconnect, args=(ws, token, stop_watch),
                                    daemon=True)
        watcher.start()

        req_id = str(uuid.uuid4())
        msg = pack_message(
            {"timestamp": int(time.time() * 1000), "type": "request",
             "action": "getConsoleInfo", "id": req_id},
            {},
        )
        ws.send(msg)
        log.info(">>> sent getConsoleInfo request id=%s", req_id)

        try:
            _consume(ws, device_info)
        finally:
            stop_watch.set()


def _consume(ws, device_info):
    for raw in ws:
        if isinstance(raw, str):
            log.warning("unexpected TEXT frame (protocol says binary-only): %r", raw[:200])
            continue
        records = unpack_all_records(raw)
        if len(records) < 2:
            log.warning("frame with <2 records: %s", records)
            continue
        head = records[0][2]
        body = records[1][2]

        if head.get("type") == "response":
            log.info("<<< response to our request id=%s errorCode=%s body=%s",
                      head.get("id"), head.get("errorCode"), body)
            continue

        reply_body = handle_action(head, body, device_info)
        if head.get("id"):
            reply = pack_message(
                {"timestamp": int(time.time() * 1000), "type": "response",
                 "action": head.get("action"), "id": head["id"], "errorCode": 0},
                reply_body,
            )
            ws.send(reply)
            log.info(">>> replied to action=%s id=%s body=%s",
                      head.get("action"), head.get("id"), reply_body)


def main():
    cfg = config.load_config_and_logging(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__)
    config.add_common_flags(ap)
    ap.add_argument("--iface", default=None,
                     help="interface to derive identity from (never bound); defaults to cfg "
                          "[network] iface, else the default-route interface (auto-detected)")
    ap.add_argument("--bind-ip", default="0.0.0.0",
                     help="local IP to report as our own (X-Ip header etc); does not "
                          "actually bind a socket to it since this is an outbound client")
    ap.add_argument("--mac", default=None)
    ap.add_argument("--type", default=cfg["platform"],
                     help="catalog model string (see discovery.py's --platform help)")
    ap.add_argument("--version", default=cfg["fw_version"])
    ap.add_argument("--device-id", default=cfg["device_id"])
    ap.add_argument("--guid", default=cfg["guid"])
    ap.add_argument("--fallback-host-port", default=None,
                     help="host:port to reconnect to (tokenless) if no fresh adopt POST "
                          "shows up within --fallback-after seconds")
    ap.add_argument("--fallback-after", type=float, default=10.0)
    ap.add_argument("--state-dir", default=None,
                     help="where adopt_state.json/server.crt/server.key live -- defaults "
                          "to the shared piport/ path (single-instance, unchanged "
                          "behavior); set this to isolate a second/third AI Port instance "
                          "running on the same box (see instance_manager.py)")
    args = ap.parse_args()
    args.iface = config.resolve_iface(args.iface, cfg)

    if args.state_dir:
        global ADOPT_STATE_FILE, CERT_PATH, KEY_PATH
        os.makedirs(args.state_dir, exist_ok=True)
        ADOPT_STATE_FILE = os.path.join(args.state_dir, "adopt_state.json")
        CERT_PATH = os.path.join(args.state_dir, "server.crt")
        KEY_PATH = os.path.join(args.state_dir, "server.key")

    mac_nosep = config.resolve_mac(args.iface, args.mac, cfg).lower()
    ip = config.resolve_ip(args.iface, cfg, args.bind_ip)

    device_info = {
        "mac_nosep": mac_nosep.replace(":", ""),
        "type": args.type,
        "version": args.version,
        "device_id": args.device_id,
        "guid": args.guid,
        "ip": ip,
    }

    # Tokens are single-use: send X-Token only the first time a value is seen.
    used_tokens = set()

    while True:
        state = wait_for_adopt_state(args.fallback_host_port, fallback_after=args.fallback_after)
        hosts = state.get("hosts") or []
        token = state.get("token")
        # The tokenless fallback means never adopted as this identity: report
        # X-Adopted: false.
        adopted = token is not None
        if not hosts:
            log.warning("adopt state had no hosts, retrying in 5s")
            time.sleep(5)
            continue
        host_port = hosts[0]
        if ":" in host_port:
            host, port_s = host_port.rsplit(":", 1)
            port = int(port_s)
        else:
            host, port = host_port, 7442

        send_token = token is not None and token not in used_tokens
        if token is not None:
            used_tokens.add(token)
        if token is not None and not send_token:
            log.info("token %s already used on a prior connection -- reconnecting "
                      "tokenless (fingerprint should already be pinned)", token)
        try:
            run(host, port, token, send_token, device_info, adopted=adopted)
        except Exception as e:
            log.warning("connection ended: %r -- reconnecting in 3s", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
