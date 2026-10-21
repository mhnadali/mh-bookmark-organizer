// Service worker: owns the connection to the native host and relays requests from the popup.
// The host (mhbm native-host) is started by the browser through the manifest that `mhbm install-native-host` writes.

const HOST = "com.mhbm.organizer";
let port = null;
let nextId = 1;
const pending = new Map();

function connect() {
  if (port) return port;
  port = chrome.runtime.connectNative(HOST);
  port.onMessage.addListener((msg) => {
    const slot = pending.get(msg.id);
    if (!slot) return;
    pending.delete(msg.id);
    clearTimeout(slot.timer);
    msg.ok ? slot.resolve(msg.result) : slot.reject(new Error(msg.error || "host error"));
  });
  port.onDisconnect.addListener(() => {
    const reason = chrome.runtime.lastError?.message || "the native host closed";
    port = null;
    for (const [id, slot] of pending) { clearTimeout(slot.timer); slot.reject(new Error(reason)); pending.delete(id); }
  });
  return port;
}

function call(msg, timeoutMs = 180000) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("the host took too long (the first request trains the filing model)")); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    try { connect().postMessage({ ...msg, id }); } catch (e) { pending.delete(id); clearTimeout(timer); reject(e); }
  });
}

chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
  if (request?.type !== "host") return false;
  call(request.msg).then((result) => sendResponse({ ok: true, result }), (err) => sendResponse({ ok: false, error: String(err.message || err) }));
  return true; // keep the channel open for the async reply
});
