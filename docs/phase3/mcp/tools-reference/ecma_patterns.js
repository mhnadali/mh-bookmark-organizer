// Compile every `pattern` in tools.json as an ECMAScript regular expression, with and without the `u` flag (Ajv compiles with `u`).
const fs = require("fs");
const doc = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
let n = 0, bad = 0;
function walk(s, where) {
  if (s && typeof s === "object") {
    if (typeof s.pattern === "string") {
      n++;
      for (const flags of ["", "u"]) {
        try { new RegExp(s.pattern, flags); } catch (e) { bad++; console.log("BAD", flags || "(no flags)", where, JSON.stringify(s.pattern), e.message); }
      }
    }
    for (const [k, v] of Object.entries(s)) if (typeof v === "object") walk(v, where + "." + k);
  }
}
for (const t of doc.tools) { walk(t.inputSchema, t.name + ".in"); walk(t.outputSchema, t.name + ".out"); }
console.log(`patterns compiled: ${n}, failures: ${bad}`);
process.exit(bad ? 1 : 0);
