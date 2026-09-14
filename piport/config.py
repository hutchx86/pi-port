#!/usr/bin/env python3
"""Shared configuration + device identity for the AI Port emulator.

Single source of truth read from ``aiport.cfg`` (INI); CLI flags still win
over cfg values. Defaults to ``<project root>/aiport.cfg``.
"""
import argparse
import configparser
import hashlib
import json
import logging
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(HERE, "aiport.cfg")

# Real Ubiquiti OUI; a non-Ubiquiti OUI can make the controller report "Unknown".
UBNT_OUI = "FCECDA"

# Built-in defaults for any key the cfg file doesn't set.
DEFAULTS = {
    # [identity]
    "mac": "",
    "hostname": "piport",
    "platform": "UVC AI Port",  # discovery TLV 0x0C / advertised type
    "sysid": "0xa5f1",
    "fw_version": "5.1.12.67",
    # Discovery advertises a longer fw string than ucp4/avclient's "version".
    "discovery_fw_version": "aiport4G.mt8390.v5.1.12.67.emu.260904.0000",
    "device_id": "fe6488e7-7042-5bcb-ab86-6f0ad1a5baed",
    "guid": "5c2659d4-48f3-16c4-ed83-2fefbad37066",
    # [network]
    "iface": "",                # blank = auto-detect (see resolve_iface)
    "parent_iface": "",         # macvlan parent; blank = [network] iface / auto-detect
    "mode": "dhcp",             # "dhcp" | "static"
    "ip": "",
    "netmask": "255.255.255.0",
    "gateway": "",
    "dns": "",
    # [console]
    "host": "",  # set to your controller's IP
    "port": "7442",
    # [logging]
    "debug": "false",
}


def load_config(path=None):
    """Parse aiport.cfg into merged settings; cfg wins over DEFAULTS, and a
    missing file silently falls back to DEFAULTS."""
    cfg = dict(DEFAULTS)
    path = path or DEFAULT_CONFIG_PATH
    parser = configparser.ConfigParser(interpolation=None)
    if os.path.exists(path):
        parser.read(path)
        for section in parser.sections():
            for key, value in parser.items(section):
                cfg[key] = value.strip()
    return cfg


def get_bool(cfg, key):
    return str(cfg.get(key, "")).strip().lower() in ("1", "true", "yes", "on")


def apply_logging(debug=False):
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")


def sysid_int(cfg):
    return int(str(cfg.get("sysid", "0xa5f1")), 0)


def add_common_flags(ap):
    ap.add_argument("--config", default=None,
                    help="path to aiport.cfg (default: <project root>/aiport.cfg)")
    ap.add_argument("--debug", action="store_true", default=None,
                    help="enable debug logging (overrides [logging] debug=true)")


def load_config_and_logging(argv=None):
    """Pre-parse --config/--debug (position-independent), load the cfg and
    apply the debug log level; callers must also declare both via
    add_common_flags()."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    pre.add_argument("--debug", action="store_true", default=None)
    known, _ = pre.parse_known_args(argv if argv is not None else None)
    cfg = load_config(known.config)
    debug = known.debug if known.debug is not None else get_bool(cfg, "debug")
    apply_logging(debug)
    return cfg


def _iface_mac(iface):
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip().upper().replace(":", "")
    except OSError:
        return ""


def _default_iface():
    """Best-effort auto-detect: the interface carrying the default route,
    else the first non-loopback interface that is up. Returns "" if unknown."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                # dest 0.0.0.0 with RTF_UP set (0x1) -- the default-route device
                if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 0x1:
                    return fields[0]
    except (OSError, ValueError):
        pass
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo":
                continue
            try:
                with open(f"/sys/class/net/{name}/operstate") as fh:
                    if fh.read().strip() in ("up", "unknown"):
                        return name
            except OSError:
                continue
    except OSError:
        pass
    return ""


def resolve_iface(iface_override=None, cfg=None):
    """Interface to bind/derive identity from. Precedence: explicit --iface,
    then cfg [network] iface, then auto-detect."""
    cfg = cfg or {}
    explicit = iface_override or cfg.get("iface") or ""
    return explicit or _default_iface() or "eth0"


def resolve_parent_iface(cfg=None):
    """Physical interface a macvlan instance is created on top of. Precedence:
    cfg [network] parent_iface, then [network] iface, then auto-detect."""
    cfg = cfg or {}
    return cfg.get("parent_iface") or resolve_iface(None, cfg)


def resolve_mac(iface, mac_override=None, cfg=None):
    """12-hex-char MAC (uppercase, no separators). Precedence: --mac, cfg
    [identity] mac, then real iface MAC with the Ubiquiti OUI prepended."""
    cfg = cfg or {}
    raw = mac_override or cfg.get("mac") or ""
    if raw:
        return raw.replace(":", "").replace("-", "").upper()
    real = _iface_mac(iface)
    if real:
        return UBNT_OUI + real[6:]
    return "000000000000"


def _iface_ip(iface):
    """The interface's currently-assigned IPv4, or None."""
    try:
        out = subprocess.run(["ip", "-4", "-json", "addr", "show", iface],
                             capture_output=True, text=True, timeout=5)
        data = json.loads(out.stdout or "[]")
        for link in data:
            for addr in link.get("addr_info", []):
                if addr.get("family") == "inet":
                    return addr["local"]
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def resolve_ip(iface, cfg=None, bind_ip=None):
    """IP to report/bind to. Precedence: --bind-ip, then static cfg ip (when
    mode == static), then the interface's live IP; "0.0.0.0" as last resort."""
    if bind_ip and bind_ip != "0.0.0.0":
        return bind_ip
    cfg = cfg or {}
    if str(cfg.get("mode", "")).lower() == "static" and cfg.get("ip"):
        return cfg["ip"]
    return _iface_ip(iface) or "0.0.0.0"


def netmask_to_prefix(netmask):
    """Dotted-quad netmask -> CIDR prefix length (255.255.255.0 -> 24)."""
    if not netmask:
        return None
    try:
        octets = [int(x) for x in str(netmask).split(".")]
        if len(octets) != 4:
            return None
        return sum(bin(o).count("1") for o in octets)
    except ValueError:
        return None


def atomic_write_json(path, obj):
    """Write obj as JSON atomically (tmp + os.replace) so concurrent readers
    never see a torn write."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _tmpfs_base():
    """First writable RAM-backed dir on the box (/dev/shm, then /run), falling
    back to the OS temp dir."""
    for base in ("/dev/shm", "/run"):
        if os.path.isdir(base) and os.access(base, os.W_OK):
            return base
    return tempfile.gettempdir()


def stream_dir(state_dir=None):
    """RAM-only dir for the transient per-stream snapshot JPEGs; never on the
    persistent root fs. --state-dir maps to a hash of its absolute path."""
    base = _tmpfs_base()
    if state_dir:
        key = hashlib.sha1(os.path.abspath(state_dir).encode()).hexdigest()[:12]
    else:
        key = "default"
    return os.path.join(base, "aiport-streams", key)
