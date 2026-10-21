"""Transparent stdio tee: runs `python -m mhbm <args>` and logs every line in both directions to $TEE_LOG."""
import os, subprocess, sys, threading
pkg = os.environ.get("TEE_PKG")
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
if pkg:
    env["PYTHONPATH"] = pkg
log = open(os.environ["TEE_LOG"], "a", buffering=1, encoding="utf-8")
cmd = [sys.executable, "-B", "-s", "-m", os.environ.get("TEE_MODULE", "mhbm"), *sys.argv[1:]]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
def pin():
    for line in sys.stdin.buffer:
        log.write("C->S " + line.decode("utf-8", "replace").rstrip("\n")[:600] + "\n")
        p.stdin.write(line); p.stdin.flush()
    p.stdin.close()
def pout():
    for line in p.stdout:
        log.write("S->C " + line.decode("utf-8", "replace").rstrip("\n")[:600] + "\n")
        sys.stdout.buffer.write(line); sys.stdout.buffer.flush()
threading.Thread(target=pin, daemon=True).start()
t = threading.Thread(target=pout); t.start(); t.join()
sys.exit(p.wait())
