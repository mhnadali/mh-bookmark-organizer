import json, sys, shutil
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *
d = tempfile.mkdtemp(prefix="big-", dir=SCRATCH + "/tmp"); shutil.copytree(SCRATCH + "/lib-big", d + "/lib")
s = Server(d + "/lib"); s.init()
t = time.time()
r = s.rpc("tools/call", {"name": "bm_triage", "arguments": {"limit": 5}}, id=1, timeout=300)
print(round(time.time() - t, 1), "s", json.dumps(r)[:600])
print("stderr:", s.stderr()[-500:])
r = s.rpc("ping", id=2)
print("alive:", r)
s.close()
