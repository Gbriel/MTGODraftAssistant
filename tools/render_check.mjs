// Deterministic page check: drive headless Chrome over the DevTools protocol,
// wait until a CSS selector matches (the page's async data has rendered), then
// print a JSON report of what is on the page plus any console errors.
//
//   node tools/render_check.mjs <url> [--wait <selector>] [--chrome <path>] [--profile <dir>]
//
// `--dump-dom` cannot do this: it dumps at the load event, before fetches
// resolve, and a virtual time budget hangs on the page's open SSE stream.
// Needs Node 22+ (built-in fetch and WebSocket).
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; };
const url = args.find((a) => !a.startsWith("--") && !args[args.indexOf(a) - 1]?.startsWith("--"));
const waitFor = opt("--wait", "#stats-table tbody tr");
const chrome = opt("--chrome", "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe");
const profile = opt("--profile", mkdtempSync(join(tmpdir(), "render-check-")));
const port = 9333 + Math.floor(Math.random() * 500);

const proc = spawn(chrome, ["--headless", "--disable-gpu", "--no-sandbox", `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try {
  let targets = null;
  for (let i = 0; i < 50 && !targets; i++) {
    await sleep(200);
    try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch { /* not up yet */ }
  }
  if (!targets) throw new Error("chrome did not come up");
  const page = targets.find((t) => t.type === "page") || targets[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0; const pending = new Map(); const logs = [];
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    else if (msg.method === "Runtime.exceptionThrown") logs.push("EXCEPTION " + JSON.stringify(msg.params.exceptionDetails).slice(0, 800));
    else if (msg.method === "Runtime.consoleAPICalled") logs.push(msg.params.type + " " + msg.params.args.map((a) => a.value ?? a.description).join(" ").slice(0, 400));
  };
  const send = (method, params = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  const evalJs = async (expr) => (await send("Runtime.evaluate", { expression: expr, returnByValue: true })).result?.result?.value;
  await send("Runtime.enable"); await send("Page.enable");
  await send("Page.navigate", { url });
  let found = 0;
  for (let i = 0; i < 60 && !found; i++) {
    await sleep(250);
    found = (await evalJs(`document.querySelectorAll(${JSON.stringify(waitFor)}).length`)) || 0;
  }
  const report = await evalJs(`JSON.stringify({
    matched: document.querySelectorAll(${JSON.stringify(waitFor)}).length,
    title: document.title,
    averages: (document.getElementById('stats-avg') || {}).innerText,
    note: (document.getElementById('stats-note') || {}).textContent,
    empty: (document.getElementById('stats-empty') || {}).textContent,
    header: [...document.querySelectorAll('#stats-table thead th')].map((t) => t.textContent),
    first: [...document.querySelectorAll('#stats-table tbody tr')].slice(0, 4).map((tr) => [...tr.children].map((td) => td.innerText.replace(/\\s+/g, ' ')).join(' | ')),
    up: document.querySelectorAll('.wr.up').length, down: document.querySelectorAll('.wr.down').length,
    decks: document.querySelectorAll('.drafts-row').length,
    seats: [...document.querySelectorAll('.seat .who')].map((e) => e.textContent),
  }, null, 1)`);
  console.log(report);
  if (logs.length) console.log("console:\n" + logs.join("\n"));
  ws.close();
} finally {
  proc.kill();
}
