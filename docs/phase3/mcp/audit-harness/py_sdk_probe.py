"""Official Python SDK (mcp 2.3.0) client against the server named on argv: python py_sdk_probe.py PKG_PATH [MODULE]"""
import asyncio, os, sys, shutil, tempfile
import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

S = "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit"
pkg = sys.argv[1]
module = sys.argv[2] if len(sys.argv) > 2 else "mhbm"
home = tempfile.mkdtemp(prefix="pysdk-", dir=S + "/tmp") + "/lib"
shutil.copytree(S + "/lib-seed", home)
params = StdioServerParameters(command=sys.executable, args=["-B", "-s", "-m", module, "--home", home, "mcp"],
                               env={"PYTHONPATH": pkg, "MHBM_LANG": "en", "PATH": os.environ["PATH"]}, cwd=S + "/work")

async def attempt(label, coro):
    try:
        r = await coro
        print(f"{label}: OK {str(r)[:300]}")
    except Exception as e:
        print(f"{label}: RAISED {type(e).__name__}: {str(e)[:300]}")

async def main():
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("initialize ->", init.model_dump(exclude_none=True) if hasattr(init, "model_dump") else init)
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools][:20], "| annotations of first:", tools.tools[0].annotations, "| outputSchema:", tools.tools[0].output_schema if hasattr(tools.tools[0], "output_schema") else getattr(tools.tools[0], "outputSchema", "?"))
            await attempt("call bm_stats", session.call_tool("bm_stats", {}))
            await attempt("call unknown tool", session.call_tool("nope", {}))
            await attempt("call bm_search missing query", session.call_tool("bm_search", {}))
            await attempt("list_prompts", session.list_prompts())
            await attempt("list_resources", session.list_resources())
            await attempt("ping", session.send_ping())
            await attempt("call bm_search query=123 (wrong type)", session.call_tool("bm_search", {"query": 123}))
            await attempt("ping after that", session.send_ping())

asyncio.run(main())
