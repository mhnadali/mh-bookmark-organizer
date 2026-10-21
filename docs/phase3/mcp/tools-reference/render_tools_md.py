"""Renders ../TOOLS.md from tools_template.md + ../tools.json (so the reference sections cannot drift from the machine-readable spec)."""
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
doc = json.load(open(os.path.join(ROOT, "tools.json"), encoding="utf-8"))
T = doc["tools"]
tpl = open(os.path.join(HERE, "tools_template.md"), encoding="utf-8").read()


def yn(b):
    return "y" if b else "n"


def kb(n):
    return f"{n / 1000:.1f} KB" if n >= 1000 else f"{n} B"


def render_argv(spec):
    out, pos = [], []
    for tok in spec:
        if isinstance(tok, str):
            out.append(tok)
            continue
        cond = ""
        if "when" in tok:
            cond = f"  (if {tok['when']})"
        if "when_eq" in tok:
            cond = f"  (if {tok['when_eq'][0]}={json.dumps(tok['when_eq'][1])})"
        if "opt" in tok:
            if "value" in tok:
                s = f"{tok['opt']}={tok['value']}"
            elif "expr" in tok:
                s = f"{tok['opt']}=<{tok['expr']}>"
            else:
                s = f"{tok['opt']}=<{tok['arg']}>" + (f" (omitted if {tok['skip_if']})" if "skip_if" in tok else "")
            out.append(s + cond)
        elif "flag" in tok:
            eq = f" (if {tok['arg']}={json.dumps(tok['eq'])})" if "eq" in tok else (f" (if {tok['arg']})" if "arg" in tok else "")
            out.append(f"[{tok['flag']}]{eq}{cond}")
        elif "multi" in tok:
            out.append(f"{tok['multi']} <{tok['arg']}...>" + cond)
        elif "pair" in tok:
            out.append(f"{tok['pair']} <{'> <'.join(tok['args'])}>")
        elif "pos" in tok:
            pos.append(f"<{tok['pos']}>")
        elif "pos_list" in tok:
            pos.append(f"<{tok['pos_list']}...>")
    line = " ".join(out)
    if pos:
        line += " -- " + " ".join(pos)
    return line


def argv_block(t):
    impl = t["impl"]
    lines = []
    if impl.get("variants"):
        for v, src in impl["variants"].items():
            if "argv" in src:
                lines.append(f"[{v}]  {render_argv(src['argv'])}")
            elif "preview_argv" in src:
                lines.append(f"[{v}] propose: {render_argv(src['preview_argv']) or '(read the snapshot in-process)'}")
                lines.append(f"[{v}] apply:   {render_argv(src['apply_argv'])}")
            else:
                lines.append(f"[{v}]  in-process: {src.get('inproc', '')}")
    elif impl.get("argv"):
        lines.append(render_argv(impl["argv"]))
    if impl.get("preview_argv") is not None:
        lines.append("propose: " + (render_argv(impl["preview_argv"]) or "(nothing to run)"))
        lines.append("apply:   " + render_argv(impl["apply_argv"]))
    if impl["kind"] == "inproc":
        lines.append("no CLI equivalent - in-process: " + impl.get("inproc", ""))
    if impl["kind"] == "dispatch":
        lines.append("runs the apply command stored with the proposal: " + impl.get("steps", ""))
    return lines


def schema_table(t):
    s = t["inputSchema"]
    req = set(s.get("required", []))
    if not s["properties"]:
        return "_no arguments_"
    rows = ["| argument | type / constraints | req | default | meaning |", "|---|---|---|---|---|"]
    for k, v in s["properties"].items():
        ty = v.get("type", "")
        if isinstance(ty, list):
            ty = "|".join(ty)
        bits = [ty]
        if "enum" in v:
            bits = ["enum " + " / ".join(map(str, v["enum"]))]
        for a, lab in (("minimum", ">="), ("maximum", "<="), ("minLength", "len>="), ("maxLength", "len<="), ("minItems", "items>="), ("maxItems", "items<="), ("maxProperties", "entries<=")):
            if a in v:
                bits.append(f"{lab}{v[a]}")
        if v.get("uniqueItems"):
            bits.append("unique")
        if "pattern" in v:
            p = v["pattern"]
            bits.append("pattern `" + (p if len(p) < 46 else p[:43] + "...") + "`")
        if v.get("type") == "array" and "items" in v:
            it = v["items"]
            inner = it.get("enum") and "enum " + "/".join(it["enum"]) or it.get("type", "object")
            if "pattern" in it and it.get("type") == "string":
                inner += " (pattern)"
            bits.append(f"of {inner}")
        desc = v.get("description", "").replace("|", "/").replace("\n", " ")
        rows.append(f"| `{k}` | {', '.join(b for b in bits if b)} | {'yes' if k in req else ''} | {json.dumps(v['default']) if 'default' in v else ''} | {desc} |")
    return "\n".join(rows)


def compact(o, n=900):
    s = json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    return s if len(s) <= n else s[: n - 1] + "..."


def per_tool():
    out = []
    for t in T:
        a = t["annotations"]
        meta = t.get("_meta")
        out.append(f"### {t['name']} - {t['title']}\n")
        out.append(f"group `{t['group']}` · priority {t['priority']} · `--read-only` set: **{'yes' if t.get('read_only_set') else 'no'}** · annotations: readOnly={yn(a['readOnlyHint'])} destructive={yn(a['destructiveHint'])} idempotent={yn(a['idempotentHint'])} openWorld={yn(a['openWorldHint'])}"
                   + (f" · `_meta`: `{json.dumps(meta)}`" if meta else "") + "\n")
        out.append(t["description"] + "\n")
        out.append("**Arguments** (closed object)\n\n" + schema_table(t) + "\n")
        out.append("**argv**\n\n```\n" + "\n".join(argv_block(t)) + "\n```\n")
        impl = t["impl"]
        extra = [impl[k] for k in ("post", "pre", "notes") if impl.get(k)]
        if extra:
            out.append("**Server-side** " + " · ".join(extra) + "\n")
        if impl.get("derived"):
            out.append("**Derived values** " + "; ".join(f"`{k}` = {v}" for k, v in impl["derived"].items()) + "\n")
        if impl.get("cli_prerequisite") or impl.get("new_cli_suggested") or impl.get("code_change"):
            out.append("**Needs** " + " · ".join(str(impl[k]) for k in ("cli_prerequisite", "new_cli_suggested", "code_change") if impl.get(k)) + "\n")
        fs, net, sl, o, mu = t["fs"], t["network"], t["slow"], t["output"], t["mutation"]
        facts = []
        if fs["path_args"]:
            facts.append("filesystem: **user-chosen paths** - " + "; ".join(
                f"`{p['arg']}` {p['mode']} confined to {p['confine']}" + (f" ext {p['ext']}" if p.get("ext") else "") + (f" max {p['max_bytes']} B" if p.get("max_bytes") else "") for p in fs["path_args"]))
        if fs["writes"]:
            facts.append("writes inside the library home: " + ", ".join(fs["writes"]))
        if fs["reads"]:
            facts.append("reads: " + ", ".join(fs["reads"]))
        if not fs["path_args"] and not fs["writes"] and not fs["reads"]:
            facts.append("filesystem: none")
        facts.append("network: " + (("**yes**, SSRF guard" + (f" - {net['verified']}" if net.get("verified") else "") + f"; hosts: {net.get('hosts', '')}") if net["uses"] else "none" + (f" ({net['note']})" if net.get("note") else "")))
        facts.append("slow: " + (f"**yes**, progress notifications; typical {sl.get('typical_s', '')}; worst {sl.get('worst_s', '')}" if sl["slow"] else "no" + (f" ({sl['typical_s']})" if sl.get("typical_s") else "")))
        facts.append(f"output: typical {kb(o['typical_bytes'])}, worst {kb(o['worst_bytes'])} - {o['policy']}" + (f". Measured: {o['measured']}" if o.get("measured") else ""))
        facts.append(f"mutation: journal `{mu['journal']}`; undo: {mu['undo']}; snapshot: {mu.get('snapshot') or '-'}; handshake: {mu['handshake']}")
        out.append("\n".join("* " + f for f in facts) + "\n")
        if t.get("risk"):
            r = t["risk"]
            out.append(f"**Injection risk** scenario: {r['injection_scenario']} → worst case: {r['worst_case']} → controls: {r['controls']} → recovery: {r['recovery']}\n")
        for ex in t["examples"][:2]:
            out.append(f"**Example - {ex['title']}** (library: {ex['library']}; {ex.get('seconds', '?')} s)\n")
            out.append("```\narguments: " + compact(ex["arguments"], 600))
            if "protocol_error" in ex:
                out.append("protocol error: " + compact(ex["protocol_error"]))
            else:
                out.append(("error: " if ex.get("is_error") else "result: ") + compact(ex.get("result"), 900))
                if "apply_result" in ex:
                    out.append("then bm_apply: " + compact(ex["apply_arguments"], 300))
                    out.append("applied: " + compact(ex["apply_result"], 600))
                    if ex.get("undo_result"):
                        out.append("bm_undo: " + compact(ex["undo_result"], 300))
            out.append("```\n")
    return "\n".join(out)


def summary_table():
    rows = ["| tool | grp | pri | RO set | R/D/I/O | filesystem | net | slow | bytes typ -> worst | journal / undo |", "|---|---|---|---|---|---|---|---|---|---|"]
    for t in T:
        a = t["annotations"]
        fs = t["fs"]
        fst = ("paths: " + ",".join(p["arg"] for p in fs["path_args"])) if fs["path_args"] else ("home" if fs["writes"] else "-")
        j = t["mutation"]["journal"]
        j = "-" if j == "none" and t.get("read_only_set") else j.split(" (")[0].replace("applied by bm_apply as ", "via bm_apply: ")
        rows.append(f"| `{t['name']}` | {t['group']} | {t['priority']} | {yn(t.get('read_only_set'))} | {yn(a['readOnlyHint'])}/{yn(a['destructiveHint'])}/{yn(a['idempotentHint'])}/{yn(a['openWorldHint'])} | {fst} | {yn(t['network']['uses'])} | "
                    f"{'progress' if t['slow']['slow'] else '-'} | {kb(t['output']['typical_bytes'])} -> {kb(t['output']['worst_bytes'])} | {j[:60]} |")
    return "\n".join(rows)


def md_list(items):
    return "\n".join(f"* {i}" for i in items)


def resources():
    rows = ["| URI / template | mime | backed by | description |", "|---|---|---|---|"]
    for r in doc["resources"]:
        rows.append(f"| `{r['uri']}` | {r['mimeType']} | {r['backed_by']} | {r['description']} |")
    for r in doc["resource_templates"]:
        rows.append(f"| `{r['uriTemplate']}` | {r['mimeType']} | {r['backed_by']} | {r['description']} |")
    return "\n".join(rows)


def prompts():
    out = []
    for p in doc["prompts"]:
        args = ", ".join(f"`{a['name']}`{' (required)' if a['required'] else ''}: {a['description']}" for a in p["arguments"]) or "none"
        out.append(f"#### `{p['name']}` - {p['title']}\n\n{p['description']}  Arguments: {args}.\n\nMessage (role `user`, one text block):\n")
        out.append("```text\n" + p["messages"][0]["content"]["text"] + "\n```\n")
    return "\n".join(out)


def coverage():
    rows = ["| CLI command / sub-mode | decision | tool | reason |", "|---|---|---|---|"]
    for r in doc["coverage"]:
        rows.append(f"| `{r['cli']}` | {r['decision']} | {('`' + r['tool'] + '`') if r['tool'] else '-'} | {r['reason']} |")
    return "\n".join(rows)


def prereq():
    rows = ["| id | severity | where | change |", "|---|---|---|---|"]
    for r in doc["cli_prerequisites"]:
        rows.append(f"| {r['id']} | {r['severity']} | `{r['where']}` | {r['change']} |")
    return "\n".join(rows)


def risk_table():
    rows = ["| tool | injected instruction (example) | worst case | controls | recovery |", "|---|---|---|---|---|"]
    for t in T:
        if t.get("risk"):
            r = t["risk"]
            rows.append(f"| `{t['name']}` | {r['injection_scenario']} | {r['worst_case']} | {r['controls']} | {r['recovery']} |")
    return "\n".join(rows)


def group_rows():
    return "\n".join(f"| `{g}` | {', '.join('`' + n + '`' for n in v['tools'])} | {'yes' if v['default'] else 'no'} | {v['description']} |" for g, v in doc["groups"].items())


c = doc["confirmation"]
ja = doc["journal_audit"]
tiers = md_list(f"**Tier {k}** - {v}" for k, v in c["tiers"].items())
cs = c["client_side_controls"]
tiers += "\n\n**Client-side controls.** " + cs["claude_code_requiresUserInteraction"]["what"] + " " + cs["claude_code_requiresUserInteraction"]["effect"] + " " + cs["claude_code_requiresUserInteraction"]["consequence"] + \
    " Automation: " + cs["claude_code_requiresUserInteraction"]["automation"] + " Other clients: " + cs["other_clients"] + " Allow-list example: " + ", ".join(f"`{x}`" for x in cs["allow_list_example"]) + "."
elicit = (f"When: {c['elicitation']['when']}\n\nRequest: `{json.dumps(c['elicitation']['request'])}`\n\nOn decline: {c['elicitation']['on_decline']}\n\nClient support: {c['elicitation']['client_support']}")
journal_table = md_list(ja["undoable_verified"])
not_j = "\n".join(f"* `{x['cmd']}` - {x['evidence']}. Impact: {x['impact']}." for x in ja["not_journaled_verified"])
desc_max = max(len(t["description"]) for t in T)

text = tpl
for k, v in {
    "{{GROUP_ROWS}}": group_rows(), "{{MAX_DESC}}": str(desc_max), "{{INSTR_LEN}}": str(len(doc["server"]["instructions"])),
    "{{SUMMARY_TABLE}}": summary_table(), "{{PER_TOOL}}": per_tool(), "{{TIERS}}": tiers, "{{JOURNAL_TABLE}}": journal_table,
    "{{NOT_JOURNALED}}": not_j, "{{JOURNAL_MINOR}}": "; ".join(ja["minor"]), "{{ELICIT}}": elicit, "{{THREAT}}": doc["prompt_injection"]["threat"],
    "{{RISK_TABLE}}": risk_table(), "{{CONTROLS}}": md_list(doc["prompt_injection"]["controls"]), "{{INSTRUCTIONS}}": doc["server"]["instructions"],
    "{{RESOURCES}}": resources(), "{{PROMPTS}}": prompts(), "{{COVERAGE}}": coverage(), "{{PREREQ}}": prereq(),
}.items():
    assert k in text, k
    text = text.replace(k, v)
open(os.path.join(ROOT, "TOOLS.md"), "w", encoding="utf-8").write(text)
print(f"TOOLS.md: {len(text.splitlines())} lines, {len(text)} characters; longest tool description {desc_max} chars")
