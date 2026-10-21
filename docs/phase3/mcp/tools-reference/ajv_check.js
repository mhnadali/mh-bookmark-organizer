// Compile every input/output schema with Ajv (2020-12, strict mode - what the MCP TypeScript SDK client uses) and validate:
//  * every example's arguments against inputSchema
//  * every successful tools/call result in transcripts/*.jsonl against outputSchema
const fs = require("fs"), path = require("path");
const Ajv2020 = require(process.argv[2] + "/node_modules/ajv/dist/2020");
const doc = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "tools.json"), "utf8"));
const ajv = new Ajv2020({ strict: true, allErrors: true });
const vin = {}, vout = {};
let bad = 0, nIn = 0, nOut = 0;
for (const t of doc.tools) {
  try { vin[t.name] = ajv.compile(t.inputSchema); vout[t.name] = ajv.compile(t.outputSchema); }
  catch (e) { bad++; console.log("COMPILE FAIL", t.name, e.message.slice(0, 200)); }
}
for (const t of doc.tools) for (const ex of t.examples) {
  if (ex.arguments && typeof ex.arguments === "object" && vin[t.name]) {
    nIn++;
    if (!vin[t.name](ex.arguments)) { bad++; console.log("INPUT INVALID", t.name, JSON.stringify(ajv.errorsText(vin[t.name].errors)).slice(0, 200)); }
  }
}
const dir = path.join(__dirname, "..", "transcripts");
for (const f of fs.readdirSync(dir).filter(f => f.endsWith(".jsonl"))) {
  for (const line of fs.readFileSync(path.join(dir, f), "utf8").split("\n").filter(Boolean)) {
    const e = JSON.parse(line); const rq = e.request || {}, rs = e.response || e;
    if (rq.method !== "tools/call" || !rs.result) continue;
    const name = rq.params.name, r = rs.result;
    if (r.isError) { if (r.structuredContent !== undefined) { bad++; console.log("ERROR WITH structuredContent", f, name); } continue; }
    nOut++;
    if (!vout[name](r.structuredContent)) { bad++; console.log("OUTPUT INVALID", f, name, ajv.errorsText(vout[name].errors).slice(0, 200)); }
  }
}
console.log(`ajv strict 2020-12: schemas compiled ${Object.keys(vin).length}+${Object.keys(vout).length}; example inputs ${nIn}; recorded outputs ${nOut}; problems ${bad}`);
process.exit(bad ? 1 : 0);
