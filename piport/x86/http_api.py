#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""HTTPS control API on :443, standing in for AI Port's local ubnt_ctlserver:
GET/POST /api/info, POST /api/1.2/manage (adopt), plus login/status/snapshot.
The controller does not validate the server cert.
"""
import argparse
import http.server
import json
import logging
import os
import ssl
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

log = logging.getLogger("aiport-adopt-http")

HERE = os.path.dirname(os.path.abspath(__file__))
CERT_PATH = os.path.join(HERE, "server.crt")
KEY_PATH = os.path.join(HERE, "server.key")
ADOPT_STATE_FILE = os.path.join(HERE, "adopt_state.json")
# RAM-only dir shared with avclient.py (writes snapshots); served from /api/1.2/snapshot.
STREAM_DIR = config.stream_dir()


def ensure_cert():
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        return
    log.info("generating self-signed EC P-256 device cert (controller does not validate it)")
    subprocess.run([
        "openssl", "ecparam", "-out", KEY_PATH, "-name", "prime256v1", "-genkey", "-noout",
    ], check=True)
    subprocess.run([
        "openssl", "req", "-new", "-x509", "-sha256", "-key", KEY_PATH, "-out", CERT_PATH,
        "-days", "36500", "-subj", "/O=piport/CN=piport",
    ], check=True)


def _is_adopted() -> bool:
    # Read live (avclient.py clears it on ResetToDefaults) so isAdopted follows a real un-adopt.
    try:
        with open(ADOPT_STATE_FILE) as f:
            state = json.load(f)
        return bool(state.get("token")) or bool(state.get("hosts"))
    except (OSError, json.JSONDecodeError):
        return False


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "piport-http/0.1"

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return b""
        return self.rfile.read(length)

    def _info_payload(self):
        cfg = self.server.aiport_config
        adopted = _is_adopted()
        return {
            "mac": cfg["mac"],
            "type": cfg["platform"],
            "sysid": f"0x{cfg['sysid']:04x}",
            "name": cfg["hostname"],
            "model": cfg["platform"],
            "firmwareVersion": cfg["fw_version"],
            "adopted": adopted,
            "isAdopted": adopted,
            "guid": cfg["guid"],
            "deviceId": cfg["device_id"],
            "anonymousDeviceId": cfg["device_id"],
            "ip": cfg["ip"],
            "uptime": 0,
            "connectionHost": cfg["ip"],
            # Real hwrev unconfirmed; placeholder matching ucp4 getInfo's hwrev.
            "hardwareRevision": 1,
        }

    def do_GET(self):
        log.info("GET %s from %s", self.path, self.client_address)
        if self.path.rstrip("/") in ("/api/info", "/info"):
            self._json(200, self._info_payload())
        elif self.path.rstrip("/") in ("/api/support", "/support"):
            self._json(200, {"statusCode": 200})
        elif self.path.rstrip("/") in ("/api/1.2/status",):
            self._serve_status()
        elif self.path.rstrip("/") in ("/api/1.2/snapshot",):
            # REST snapshot pull (GET) is separate from POST /api/1.2/snapshot; same handler.
            self._serve_snapshot()
        else:
            log.warning("unhandled GET path %s -- replying 200 anyway", self.path)
            self._json(200, {"statusCode": 200})

    def _serve_status(self):
        # Camera capability endpoint (getFeatureFlags/requestFeatureFlags); kept
        # honest to the detector (person/vehicle/animal + motion).
        cfg = self.server.aiport_config
        mac_colons = ":".join(cfg["mac"][i:i + 2] for i in range(0, 12, 2))
        self._json(200, {
            "fw": cfg["fw_version"],
            "board": {"hwaddr": mac_colons},
            "features": {
                "smartDetect": ["person", "vehicle", "animal"],
                "motionDetect": ["stable"],
                "mic": True,
                "speaker": True,
                "ledStatus": True,
            },
        })

    def do_POST(self):
        body = self._read_body()
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"_raw": body.decode(errors="replace")}
        log.info("POST %s from %s body=%s", self.path, self.client_address,
                  json.dumps(payload))

        path = self.path.rstrip("/")
        if path in ("/api/adopt", "/adopt"):
            mgmt_payload = payload
            self._persist_adopt(mgmt_payload)
            self._json(200, {"statusCode": 200})
        elif path in ("/api/1.2/manage",):
            # Real adopt endpoint (classic UVC management API; /api/adopt unused);
            # adopt fields nested under "mgmt".
            mgmt_payload = payload.get("mgmt", payload)
            self._persist_adopt(mgmt_payload)
            self._json(200, {"statusCode": 200})
        elif path in ("/api/readopt", "/readopt"):
            self._json(200, {"statusCode": 200})
        elif path in ("/api/1.2/login",):
            # Controller logs in first and replays the Set-Cookie; no real auth.
            self.send_response(200)
            self.send_header("Set-Cookie", "AIROS_SESSIONID=piport-session; Path=/")
            body = json.dumps({"statusCode": 200}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/api/1.2/snapshot",):
            # Serve the latest frame avclient.py's ffmpeg wrote to STREAM_DIR; 404 if none yet.
            self._serve_snapshot()
        else:
            log.warning("unhandled POST path %s -- replying 200 anyway", self.path)
            self._json(200, {"statusCode": 200})

    def _serve_snapshot(self):
        stream_dir = STREAM_DIR
        try:
            files = sorted(os.listdir(stream_dir)) if os.path.isdir(stream_dir) else []
        except OSError:
            files = []
        jpg = next((f for f in files if f.endswith(".jpg")), None)
        if not jpg:
            log.warning("snapshot requested but no captured frame available yet")
            self._json(404, {"statusCode": 404, "error": "no snapshot available"})
            return
        path = os.path.join(stream_dir, jpg)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            log.warning("failed to read snapshot %s: %s", path, e)
            self._json(500, {"statusCode": 500})
            return
        log.info("serving snapshot from %s (%d bytes)", path, len(data))
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _persist_adopt(self, mgmt_payload):
        self.server.aiport_config["last_adopt_payload"] = mgmt_payload
        log.info("=" * 70)
        log.info("ADOPT PAYLOAD RECEIVED")
        log.info("  hosts:      %s", mgmt_payload.get("hosts"))
        log.info("  token:      %s", mgmt_payload.get("token"))
        log.info("  protocol:   %s", mgmt_payload.get("protocol"))
        log.info("  consoleId:  %s", mgmt_payload.get("consoleId"))
        log.info("  consoleName:%s", mgmt_payload.get("consoleName"))
        log.info("  username:   %s", mgmt_payload.get("username"))
        log.info("=" * 70)
        config.atomic_write_json(ADOPT_STATE_FILE, mgmt_payload)

    def log_message(self, fmt, *args):
        pass


class Server(http.server.ThreadingHTTPServer):
    pass


def main():
    cfg = config.load_config_and_logging(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__)
    config.add_common_flags(ap)
    ap.add_argument("--port", type=int, default=443,
                     help="AI Port's local API is on the standard camera HTTPS port 443 "
                          "(matches ubnt_ctlserver), confirmed from the controller's own "
                          "aiport.log ('connect ECONNREFUSED <device-ip>:443')")
    ap.add_argument("--iface", default=None,
                     help="interface to bind/derive identity from; defaults to cfg [network] iface, "
                          "else the default-route interface (auto-detected)")
    ap.add_argument("--bind-ip", default="0.0.0.0")
    ap.add_argument("--mac", default=None)
    ap.add_argument("--hostname", default=cfg["hostname"])
    ap.add_argument("--platform", default=cfg["platform"],
                     help="catalog model string (see discovery.py's --platform help)")
    ap.add_argument("--sysid", type=lambda s: int(s, 0), default=config.sysid_int(cfg))
    ap.add_argument("--fw-version", default=cfg["fw_version"])
    ap.add_argument("--device-id", default=cfg["device_id"])
    ap.add_argument("--guid", default=cfg["guid"])
    ap.add_argument("--state-dir", default=None,
                     help="where adopt_state.json/server.crt/server.key live -- defaults "
                          "to this script's own directory (single-instance, unchanged "
                          "behavior); set this to isolate a second/third AI Port instance "
                          "running on the same box (see instance_manager.py)")
    args = ap.parse_args()
    args.iface = config.resolve_iface(args.iface, cfg)

    if args.state_dir:
        global ADOPT_STATE_FILE, CERT_PATH, KEY_PATH, STREAM_DIR
        os.makedirs(args.state_dir, exist_ok=True)
        ADOPT_STATE_FILE = os.path.join(args.state_dir, "adopt_state.json")
        CERT_PATH = os.path.join(args.state_dir, "server.crt")
        KEY_PATH = os.path.join(args.state_dir, "server.key")
        # Both processes must derive the same RAM-only stream dir from the
        # same --state-dir.
        STREAM_DIR = config.stream_dir(args.state_dir)

    ensure_cert()

    ip = config.resolve_ip(args.iface, cfg, args.bind_ip)
    bind_ip = args.bind_ip
    if bind_ip == "0.0.0.0" and str(cfg["mode"]).lower() == "static" and cfg.get("ip"):
        bind_ip = cfg["ip"]

    server = Server((bind_ip, args.port), Handler)
    server.aiport_config = {
        "mac": config.resolve_mac(args.iface, args.mac, cfg),
        "platform": args.platform,
        "sysid": args.sysid,
        "hostname": args.hostname,
        "fw_version": args.fw_version,
        "device_id": args.device_id,
        "guid": args.guid,
        "ip": ip,
        "last_adopt_payload": None,
    }

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    log.info("HTTPS control API listening on %s:%d (mac=%s platform=%s)",
              bind_ip, args.port, server.aiport_config["mac"], args.platform)
    server.serve_forever()


if __name__ == "__main__":
    main()
