"""Scenario 7: SSRF through bm_check_links / bm_archive.  Bookmarks pointing at loopback / metadata / odd IP notations / odd schemes.
Case A: direct connections.  Case B: an HTTP proxy is configured (the netsafe docstring says names cannot be checked locally then)."""
import csv
import http.server
import json
import socketserver
import sqlite3
import sys
import threading
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *  # noqa

out = []
hits_direct, hits_proxy = [], []


class Direct(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        hits_direct.append(("GET", self.headers.get("Host"), self.path))
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(b"<html><title>direct hit</title><body>INTERNAL-SECRET</body></html>")
    do_HEAD = do_GET


class Proxy(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        hits_proxy.append((self.command, self.path))
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(b"<html><title>via proxy</title><body>proxy body</body></html>")
    do_HEAD = do_GET
    def do_CONNECT(self):
        hits_proxy.append(("CONNECT", self.path)); self.send_response(502); self.end_headers()


class TS(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


d = TS(("127.0.0.1", 0), Direct); P1 = d.server_address[1]
p = TS(("127.0.0.1", 0), Proxy); P2 = p.server_address[1]
threading.Thread(target=d.serve_forever, daemon=True).start()
threading.Thread(target=p.serve_forever, daemon=True).start()
URLS = [f"http://127.0.0.1:{P1}/direct", f"http://localhost:{P1}/localhost", f"http://[::1]:{P1}/v6", f"http://2130706433:{P1}/decimal",
        f"http://0x7f.0.0.1:{P1}/hex", f"http://127.1:{P1}/short", f"http://0177.0.0.1:{P1}/octal", f"http://[::ffff:127.0.0.1]:{P1}/mapped",
        "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/admin", "http://192.168.0.1/", "http://metadata.google.internal/computeMetadata/v1/",
        "file:///etc/hostname", "ftp://127.0.0.1/x", f"http://user:pw@127.0.0.1:{P1}/creds", "http://intranet.example.test/internal-wiki", "http://public.example.test/page"]


def build_home():
    h = tempfile.mkdtemp(prefix="ssrf-", dir=SCRATCH + "/tmp")
    csvp = h + "/in.csv"
    with open(csvp, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["url", "title", "folder"])
        for i, u in enumerate(URLS):
            w.writerow([u, f"t{i}", "Probe"])
    r = subprocess.run([sys.executable, "-B", "-s", "-m", "mhbm", "--home", h + "/lib", "--json", "--quiet", "import", csvp], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=PKG_FIXED, PYTHONDONTWRITEBYTECODE="1"))
    return h + "/lib", json.loads(r.stdout)["imported"]


import subprocess  # noqa

NOPROXY = {k: "" for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")}
for case, env in (("A: direct connections (no proxy env)", dict(NOPROXY, NO_PROXY="", no_proxy="")),
                  ("B: HTTP proxy configured (fake proxy that answers 200 to everything)", dict(NOPROXY, HTTP_PROXY=f"http://127.0.0.1:{P2}", http_proxy=f"http://127.0.0.1:{P2}", NO_PROXY="", no_proxy=""))):
    for tool, args in (("bm_check_links", {"limit": 50}), ("bm_archive", {"limit": 50})):
        hits_direct.clear(); hits_proxy.clear()
        home, n = build_home()
        s = Server(home, env_extra=env)
        s.init()
        t = time.time()
        r = s.rpc("tools/call", {"name": tool, "arguments": args}, id=1, timeout=300)
        txt = r["result"]["content"][0]["text"] if r and "result" in r else str(r)
        out.append(f"=== {case} / {tool} ({n} probe bookmarks, {time.time() - t:.1f}s)\n  reply: {txt.replace(chr(10), ' ')[:420]}")
        out.append(f"  requests that reached the LOCAL TARGET server: {hits_direct}")
        out.append(f"  requests that reached the PROXY: {hits_proxy}")
        # per-bookmark outcome
        c = sqlite3.connect(home + "/library.db")
        rows = c.execute("select url,status,http_code from bookmarks order by title").fetchall()
        c.close()
        out.append("  library status per probe URL: " + "; ".join(f"{u.split('//',1)[1][:28]}={st or '-'}/{code}" for u, st, code in rows))
        s.close()

open(SCRATCH + "/transcripts/s07_ssrf.txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
d.shutdown(); p.shutdown()
