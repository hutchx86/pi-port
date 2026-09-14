#!/usr/bin/env python3
"""Minimal dev-box system monitor for the Orange Pi running this emulator
(NOT part of the AI Port protocol). Shows CPU/memory/GPU/NPU usage in a
browser. Stdlib-only (http.server).

Board-specific sysfs paths (Orange Pi 5 Plus / RK3588):
  CPU:  /proc/stat
  Mem:  /proc/meminfo
  GPU:  /sys/devices/platform/fb000000.gpu/utilisation (0-100)
  NPU:  /sys/kernel/debug/rknpu/load (three cores, needs root)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# Shelled out to (not imported) so this stays stdlib-only and independently
# runnable; also inherits this process's root privileges.
INSTANCE_MANAGER = os.path.join(HERE, "instance_manager.py")

GPU_UTIL_PATH = "/sys/devices/platform/fb000000.gpu/utilisation"
NPU_LOAD_PATH = "/sys/kernel/debug/rknpu/load"

_NPU_CORE_RE = re.compile(r"Core\d+:\s*(\d+)%")

# Two /proc/stat samples needed for a CPU delta; keep previous across requests.
_prev_cpu_total = None
_prev_cpu_idle = None
# Per-core prev samples, keyed by /proc/stat's own "cpuN" label so the core
# count isn't hardcoded.
_prev_core_total = {}
_prev_core_idle = {}


def _read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _cpu_percent():
    global _prev_cpu_total, _prev_cpu_idle
    line = _read_file("/proc/stat")
    if not line:
        return None
    first_line = line.splitlines()[0]  # aggregate "cpu ..." line, all cores
    fields = [int(x) for x in first_line.split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    total = sum(fields)
    pct = None
    if _prev_cpu_total is not None:
        dt = total - _prev_cpu_total
        di = idle - _prev_cpu_idle
        if dt > 0:
            pct = round(100.0 * (dt - di) / dt, 1)
    _prev_cpu_total, _prev_cpu_idle = total, idle
    return pct


def _cpu_core_percents():
    text = _read_file("/proc/stat")
    if not text:
        return None
    percents = []
    for line in text.splitlines():
        if not line.startswith("cpu") or line[3] not in "0123456789":
            continue
        label, *rest = line.split()
        fields = [int(x) for x in rest]
        idle = fields[3] + fields[4]
        total = sum(fields)
        prev_total = _prev_core_total.get(label)
        prev_idle = _prev_core_idle.get(label)
        pct = None
        if prev_total is not None:
            dt = total - prev_total
            di = idle - prev_idle
            if dt > 0:
                pct = round(100.0 * (dt - di) / dt, 1)
        _prev_core_total[label] = total
        _prev_core_idle[label] = idle
        percents.append(pct)
    return percents or None


def _mem_stats():
    text = _read_file("/proc/meminfo")
    if not text:
        return None, None, None
    values = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            values[parts[0].strip()] = int(parts[1].strip().split()[0])  # kB
    total_kb = values.get("MemTotal")
    avail_kb = values.get("MemAvailable")
    if total_kb is None or avail_kb is None:
        return None, None, None
    used_kb = total_kb - avail_kb
    pct = round(100.0 * used_kb / total_kb, 1) if total_kb else None
    return round(used_kb / 1024), round(total_kb / 1024), pct  # MB, MB, %


def _gpu_percent():
    text = _read_file(GPU_UTIL_PATH)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _npu_percent():
    text = _read_file(NPU_LOAD_PATH)
    if not text:
        return None
    cores = [int(m) for m in _NPU_CORE_RE.findall(text)]
    if not cores:
        return None
    return {"average": round(sum(cores) / len(cores), 1), "cores": cores}


def collect_stats():
    mem_used_mb, mem_total_mb, mem_pct = _mem_stats()
    return {
        "timestamp": time.time(),
        "cpu_percent": _cpu_percent(),
        "cpu_cores": _cpu_core_percents(),
        "mem_used_mb": mem_used_mb,
        "mem_total_mb": mem_total_mb,
        "mem_percent": mem_pct,
        "gpu_percent": _gpu_percent(),
        "npu": _npu_percent(),
    }


def list_instances():
    try:
        out = subprocess.run([sys.executable, INSTANCE_MANAGER, "list"],
                              capture_output=True, text=True, timeout=10)
        return json.loads(out.stdout or "[]")
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return []


def create_instance(name):
    """Blocks up to ~20s for the DHCP lease; safe because each request runs
    on its own thread."""
    proc = subprocess.run([sys.executable, INSTANCE_MANAGER, "create", name],
                           capture_output=True, text=True, timeout=35)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "unknown error").strip())
    return json.loads(proc.stdout)


def destroy_instance(name):
    proc = subprocess.run([sys.executable, INSTANCE_MANAGER, "destroy", name],
                           capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "unknown error").strip())


PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>AI Port Dev Box</title>
<style>
  body { background:#111; color:#eee; font-family: system-ui, sans-serif; margin:0; padding:2rem; }
  h1 { font-size:1.1rem; font-weight:600; color:#888; margin:0 0 1.5rem; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px,1fr)); gap:1rem; max-width:900px; }
  .card { background:#1b1b1b; border-radius:10px; padding:1rem 1.2rem; }
  .label { color:#999; font-size:0.8rem; text-transform:uppercase; letter-spacing:0.05em; }
  .value { font-size:2rem; font-weight:700; margin-top:0.2rem; }
  .sub { color:#777; font-size:0.8rem; margin-top:0.3rem; }
  .bar { height:6px; background:#333; border-radius:3px; margin-top:0.7rem; overflow:hidden; }
  .bar-fill { height:100%; background:#4ade80; transition:width 0.3s; }
  .stale { opacity:0.4; }
  .cores-card { grid-column:1 / -1; }
  .cores-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(70px,1fr)); gap:0.8rem; margin-top:0.8rem; }
  .core { text-align:center; }
  .core-pct { font-size:0.85rem; font-weight:600; margin-bottom:0.3rem; }
  .core-bar { height:40px; width:100%; background:#333; border-radius:4px; overflow:hidden; display:flex; align-items:flex-end; }
  .core-bar-fill { width:100%; background:#4ade80; transition:height 0.3s; }
  .core-label { color:#777; font-size:0.7rem; margin-top:0.3rem; }
  .instances-card { grid-column:1 / -1; }
  .new-instance { display:flex; gap:0.6rem; margin-top:0.8rem; }
  .new-instance input { background:#111; border:1px solid #333; color:#eee; border-radius:6px;
    padding:0.5rem 0.7rem; font-size:0.9rem; flex:1; max-width:200px; }
  button { background:#2a2a2a; border:1px solid #3a3a3a; color:#eee; border-radius:6px;
    padding:0.5rem 1rem; font-size:0.9rem; cursor:pointer; }
  button:hover { background:#333; }
  button:disabled { opacity:0.5; cursor:default; }
  button.danger:hover { background:#5c2323; border-color:#7a2e2e; }
  table.instances { width:100%; border-collapse:collapse; margin-top:1rem; font-size:0.85rem; }
  table.instances th { text-align:left; color:#888; font-weight:600; font-size:0.75rem;
    text-transform:uppercase; letter-spacing:0.04em; padding:0.4rem 0.6rem; border-bottom:1px solid #2a2a2a; }
  table.instances td { padding:0.5rem 0.6rem; border-bottom:1px solid #222; }
  table.instances tr:last-child td { border-bottom:none; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:0.5rem; }
  .dot.up { background:#4ade80; }
  .dot.down { background:#666; }
  .empty-row td { color:#666; text-align:center; padding:1rem; }
  .err { color:#f87171; font-size:0.85rem; margin-top:0.6rem; }
  .err.ok { color:#4ade80; }
  code { background:#111; padding:0.1rem 0.35rem; border-radius:4px; font-size:0.85em; }
</style>
</head>
<body>
<h1>Orange Pi 5 Plus &mdash; AI Port emulator host</h1>
<div class="grid">
  <div class="card"><div class="label">CPU</div><div class="value" id="cpu-val">--</div>
    <div class="bar"><div class="bar-fill" id="cpu-bar" style="width:0%"></div></div></div>
  <div class="card"><div class="label">Memory</div><div class="value" id="mem-val">--</div>
    <div class="sub" id="mem-sub"></div>
    <div class="bar"><div class="bar-fill" id="mem-bar" style="width:0%"></div></div></div>
  <div class="card"><div class="label">GPU (Mali)</div><div class="value" id="gpu-val">--</div>
    <div class="bar"><div class="bar-fill" id="gpu-bar" style="width:0%"></div></div></div>
  <div class="card"><div class="label">NPU (RKNN, avg of 3 cores)</div><div class="value" id="npu-val">--</div>
    <div class="sub" id="npu-sub"></div>
    <div class="bar"><div class="bar-fill" id="npu-bar" style="width:0%"></div></div></div>
  <div class="card cores-card"><div class="label">Per-core CPU</div>
    <div class="cores-grid" id="cores-grid"></div></div>
  <div class="card instances-card">
    <div class="label">AI Port instances</div>
    <div class="sub">The box's original/production AI Port (systemd-managed) isn't listed here &mdash;
      this only manages extra instances, each on its own macvlan interface + MAC + DHCP lease.</div>
    <table class="instances"><thead><tr>
      <th></th><th>Name</th><th>MAC</th><th>IP</th><th>Created</th><th></th>
    </tr></thead><tbody id="instances-body">
      <tr class="empty-row"><td colspan="6">loading&hellip;</td></tr>
    </tbody></table>
    <div class="new-instance">
      <input id="new-name" placeholder="name (e.g. lab2)" maxlength="12">
      <button id="create-btn" onclick="createInstance()">+ New instance</button>
    </div>
    <div class="err" id="instances-err" hidden></div>
  </div>
</div>
<script>
function setStat(prefix, pct, label, sub) {
  document.getElementById(prefix + "-val").textContent = (pct === null || pct === undefined) ? "n/a" : label;
  document.getElementById(prefix + "-bar").style.width = (pct || 0) + "%";
  if (sub !== undefined) document.getElementById(prefix + "-sub").textContent = sub;
}
const coresGrid = document.getElementById("cores-grid");
let coreEls = [];
function coreColor(pct) {
  if (pct >= 80) return "#f87171";
  if (pct >= 50) return "#facc15";
  return "#4ade80";
}
function renderCores(cores) {
  if (!cores) return;
  if (coreEls.length !== cores.length) {
    coresGrid.innerHTML = "";
    coreEls = cores.map((_, i) => {
      const el = document.createElement("div");
      el.className = "core";
      el.innerHTML = '<div class="core-pct">--</div>' +
        '<div class="core-bar"><div class="core-bar-fill" style="height:0%"></div></div>' +
        '<div class="core-label">C' + i + '</div>';
      coresGrid.appendChild(el);
      return el;
    });
  }
  cores.forEach((pct, i) => {
    const el = coreEls[i];
    const p = pct === null || pct === undefined ? 0 : pct;
    el.querySelector(".core-pct").textContent = (pct === null || pct === undefined) ? "n/a" : p + "%";
    const fill = el.querySelector(".core-bar-fill");
    fill.style.height = p + "%";
    fill.style.background = coreColor(p);
  });
}
async function poll() {
  try {
    const r = await fetch("/api/stats");
    const d = await r.json();
    document.body.classList.remove("stale");
    setStat("cpu", d.cpu_percent, d.cpu_percent + "%");
    setStat("mem", d.mem_percent, d.mem_percent + "%", d.mem_used_mb + " / " + d.mem_total_mb + " MB");
    setStat("gpu", d.gpu_percent, d.gpu_percent + "%");
    if (d.npu) {
      setStat("npu", d.npu.average, d.npu.average + "%", "cores: " + d.npu.cores.join("% / ") + "%");
    } else {
      setStat("npu", null, "n/a");
    }
    renderCores(d.cpu_cores);
  } catch (e) {
    document.body.classList.add("stale");
  }
}
poll();
setInterval(poll, 1000);

const instancesBody = document.getElementById("instances-body");
const instancesErr = document.getElementById("instances-err");
let msgTimer = null;
function showMsg(msg, ok) {
  clearTimeout(msgTimer);
  instancesErr.textContent = msg;
  instancesErr.hidden = !msg;
  instancesErr.classList.toggle("ok", !!ok);
  if (msg && ok) msgTimer = setTimeout(() => showMsg(""), 4000);
}
function showErr(msg) { showMsg(msg, false); }
function fmtAge(createdEpoch) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - createdEpoch));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}
async function pollInstances() {
  try {
    const r = await fetch("/api/instances");
    const list = await r.json();
    if (!list.length) {
      instancesBody.innerHTML = '<tr class="empty-row"><td colspan="6">no extra instances running</td></tr>';
      return;
    }
    instancesBody.innerHTML = list.map(inst => `
      <tr>
        <td><span class="dot ${inst.running ? 'up' : 'down'}" title="${inst.running ? 'running' : 'stopped'}"></span></td>
        <td>${inst.name}</td>
        <td><code>${inst.mac_display || inst.mac || '--'}</code></td>
        <td><code>${inst.ip || '--'}</code></td>
        <td>${inst.created ? fmtAge(inst.created) : '--'}</td>
        <td><button class="danger" onclick="destroyInstance('${inst.name}', this)">Tear down</button></td>
      </tr>`).join("");
  } catch (e) {
    // leave existing rows in place on a transient fetch failure
  }
}
async function createInstance() {
  const input = document.getElementById("new-name");
  const btn = document.getElementById("create-btn");
  const name = input.value.trim();
  if (!name) { showErr("enter a name first"); return; }
  showErr("");
  btn.disabled = true;
  btn.textContent = "Starting… (DHCP can take up to 20s)";
  try {
    const r = await fetch("/api/instances/create", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "failed");
    input.value = "";
    await pollInstances();
  } catch (e) {
    showErr(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "+ New instance";
  }
}
async function destroyInstance(name, btn) {
  showMsg("");
  if (btn) { btn.disabled = true; btn.textContent = "Tearing down…"; }
  try {
    const r = await fetch("/api/instances/destroy", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || "failed");
    await pollInstances();
    showMsg(`'${name}' torn down`, true);
  } catch (e) {
    showMsg(e.message, false);
    if (btn) { btn.disabled = false; btn.textContent = "Tear down"; }
  }
}
pollInstances();
setInterval(pollInstances, 4000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep this quiet -- polled every second, not worth logging

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = PAGE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/stats":
            self._send_json(collect_stats())
        elif self.path == "/api/instances":
            # Hide physical-mode instances (permanent, systemd-managed) so
            # there's no misleading "Tear down" button on them.
            self._send_json([i for i in list_instances() if i.get("mode") != "physical"])
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path not in ("/api/instances/create", "/api/instances/destroy"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            name = str(payload.get("name", "")).strip()
            if not name:
                raise ValueError("name is required")
            if self.path == "/api/instances/create":
                result = create_instance(name)
            else:
                destroy_instance(name)
                result = {"ok": True}
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, status=400)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind-ip", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind_ip, args.port), Handler)
    print(f"sysinfo server listening on {args.bind_ip}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
