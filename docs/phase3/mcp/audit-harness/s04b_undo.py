import json, sys, sqlite3, shutil
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *
home = fresh_home()
def n():
    c = sqlite3.connect(home + "/library.db"); v = c.execute("select count(*) from bookmarks").fetchone()[0]; c.close(); return v
s = Server(home); s.init()
print("rows before:", n())
r = s.rpc("tools/call", {"name": "bm_undo", "arguments": {}}, id=2)
print("bm_undo ->", r["result"]["content"][0]["text"].replace("\n", " ")[:200], "| isError", r["result"]["isError"])
print("rows after bm_undo (no confirmation, no dry-run):", n())
r = s.rpc("tools/call", {"name": "bm_undo", "arguments": {}}, id=3)
print("2nd bm_undo ->", r["result"]["content"][0]["text"][:200].replace("\n", " "), "| isError", r["result"]["isError"])
s.close()
