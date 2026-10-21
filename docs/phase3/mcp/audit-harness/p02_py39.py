import json, sys, os
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
import mcpdrive
from mcpdrive import *
py = sys.argv[1]
class S2(Server):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
home = fresh_home()
env = dict(os.environ, PYTHONPATH=SCRATCH + "/pkg_proto", PYTHONDONTWRITEBYTECODE="1")
import subprocess
p = subprocess.Popen([py, "-B", "-m", "mhbm", "--home", home, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=SCRATCH + "/work")
def rpc(msg):
    p.stdin.write((json.dumps(msg) + "\n").encode()); p.stdin.flush(); return json.loads(p.stdout.readline())
print(rpc({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {}}})["result"]["protocolVersion"])
r = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_search", "arguments": {"query": "chisel", "limit": 1}}})
print("search ok:", not r["result"]["isError"], "| child python:", sys.argv[1])
p.stdin.close(); print("exit", p.wait(10)); print(p.stderr.read().decode()[-300:])
