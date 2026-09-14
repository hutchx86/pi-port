import urllib.request, ssl, json, http.cookiejar, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pi_ssh

# Hosts/paths are env-configurable so the script survives DHCP lease changes.
UNVR_HOST = os.environ.get("UNVR_HOST")
if not UNVR_HOST:
    sys.exit("UNVR_HOST is unset")
# AI Port lives in the `aiports` collection (NOT `aiprocessors`, which is AI-Key
# specific). Optional AIPORT_ID narrows to one row; otherwise the whole
# collection is watched.
AIPORTS_API = os.environ.get("AIPORTS_API", "/proxy/protect/api/aiports")
AIPORT_ID = os.environ.get("AIPORT_ID")
# Pi-side log to tail. Default is this project's own log; set ORANGEPI_LOG to
# another path, or to a full command (e.g. "journalctl -u aiport-instance-main -n 200").
PI_LOG = os.environ.get("ORANGEPI_LOG", "tail -n 200 /root/piport/run_all.log")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                      urllib.request.HTTPSHandler(context=ctx))
body = json.dumps({"username": "Token", "password": os.environ["UNVR_PASS"]}).encode()
req = urllib.request.Request(f"https://{UNVR_HOST}/api/auth/login", data=body, method="POST",
                              headers={"Content-Type": "application/json"})
resp = opener.open(req, timeout=10)
csrf = resp.headers.get("X-Updated-Csrf-Token")

aiports_url = f"https://{UNVR_HOST}{AIPORTS_API}" + (f"/{AIPORT_ID}" if AIPORT_ID else "")

last_state = None
last_log_line_count = 0

print(f"watching {aiports_url} ...", flush=True)
start = time.time()
while time.time() - start < 90:
    try:
        req = urllib.request.Request(aiports_url, headers={"X-Csrf-Token": csrf})
        r = opener.open(req, timeout=5)
        d = json.loads(r.read())
        entries = d if isinstance(d, list) else [d]
        snapshot = [{k: e.get(k) for k in ["state","isAdopted","isProvisioned","connectionHost",
                                            "lastSeen","name","mac","modelKey"]}
                    for e in entries]
        if snapshot != last_state:
            print(f"[{time.strftime('%H:%M:%S')}] NVR state change: {snapshot}", flush=True)
            last_state = snapshot
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] NVR poll error: {e}", flush=True)

    try:
        rc, out, err = pi_ssh.run(PI_LOG, timeout=10)
        lines = out.splitlines()
        if len(lines) > last_log_line_count:
            for line in lines[last_log_line_count:]:
                print(f"[PI LOG] {line}", flush=True)
            last_log_line_count = len(lines)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] PI poll error: {e}", flush=True)

    time.sleep(1)
print("done watching")
