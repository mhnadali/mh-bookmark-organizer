import asyncio, os, sys, shutil, tempfile, json, time
import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
S = "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit"
home = tempfile.mkdtemp(prefix="pysdk-", dir=S + "/tmp") + "/lib"; shutil.copytree(S + "/lib-seed", home)
dead = "http://127.0.0.1:9"
params = StdioServerParameters(command=sys.executable, args=["-B", "-s", "-m", "mhbm", "--home", home, "mcp"],
    env={"PYTHONPATH": S + "/pkg_proto", "MHBM_LANG": "en", "PATH": os.environ["PATH"], "HTTP_PROXY": dead, "HTTPS_PROXY": dead, "http_proxy": dead, "https_proxy": dead}, cwd=S + "/work")
async def attempt(label, coro):
    try:
        r = await coro; print(f"{label}: OK {str(r)[:260]}")
    except Exception as e:
        print(f"{label}: RAISED {type(e).__name__}: {str(e)[:200]}")
async def main():
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            print("initialize:", init.protocol_version, init.server_info.name, init.server_info.version, "| caps:", init.capabilities.model_dump(exclude_none=True), "| instructions chars:", len(init.instructions or ""))
            tools = await s.list_tools()
            t = {x.name: x for x in tools.tools}
            print("tools:", len(t), "| bm_search annotations:", t["bm_search"].annotations.model_dump(exclude_none=True), "| outputSchema keys:", list((t["bm_search"].output_schema or {}).get("properties", {})))
            r = await s.call_tool("bm_stats", {}); print("bm_stats: is_error", r.is_error, "structured total:", (r.structured_content or {}).get("total"))
            r = await s.call_tool("bm_search", {"query": 123}); print("bm_search(query=123): is_error", r.is_error, "|", r.content[0].text[:100])
            await attempt("unknown tool", s.call_tool("nope", {}))
            await attempt("prompts", s.list_prompts())
            await attempt("get_prompt", s.get_prompt("tidy-unfiled", {"focus": "recipes"}))
            await attempt("resources", s.list_resources())
            await attempt("templates", s.list_resource_templates())
            await attempt("read_resource", s.read_resource("mhbm://library/summary"))
            await attempt("read_resource unknown", s.read_resource("mhbm://nope"))
            await attempt("set_logging_level", s.set_logging_level("info"))
            ev = []
            async def on_progress(progress, total, message): ev.append((round(progress), total, message))
            r = await s.call_tool("bm_check_links", {"limit": 30}, progress_callback=on_progress)
            print("bm_check_links progress events:", len(ev), ev[:3], "| result:", r.structured_content)
            await attempt("ping", s.send_ping())
asyncio.run(main())
