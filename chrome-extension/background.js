// mekoo-op payout worker — service worker.
//
// Pull worker for mekoo-op (payout/ext_bridge.py). Every poll interval it asks
// the bridge for a job (the poll doubles as the heartbeat). On a job it reloads
// its business.tevi.com tab so the app refreshes the session, then — inside
// that page, so the request is the app's own origin/cookies/TLS — signs and
// fetches every Open and Fail page of /fiat/v1/agency/tx, and posts the rows
// back. mekoo-op decides WHEN; this only does the fetch.

const BUSINESS_URL = "https://business.tevi.com/";
const TX_PATH = "/fiat/v1/agency/tx";
const TOKEN_KEY = "user_token_identify";
const MAX_PAGES = 100;
const DEFAULTS = { market: "", bridgeUrl: "http://127.0.0.1:8788", token: "", pollSeconds: 30 };

let busy = false;

async function getConfig() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}

async function setStatus(patch) {
  const { status = {} } = await chrome.storage.local.get("status");
  await chrome.storage.local.set({ status: { ...status, ...patch } });
}

async function schedulePoll() {
  const { pollSeconds } = await getConfig();
  // chrome.alarms allows 30s minimum.
  await chrome.alarms.create("poll", { periodInMinutes: Math.max(30, pollSeconds) / 60 });
}

// --- functions injected into the business.tevi.com page (MAIN world) ---------
// They must be self-contained: no closures over this file.

function pageTokenState(tokenKey) {
  try {
    const raw = localStorage.getItem(tokenKey);
    const v = raw && (JSON.parse(raw) || {}).value;
    if (!v || !v.accessToken) return { loggedIn: false, uid: "" };
    const part = v.accessToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(atob(part));
    return { loggedIn: v.expiresIn > Date.now(), uid: String(claims.uid || "") };
  } catch (e) {
    return { loggedIn: false, uid: "", error: String(e) };
  }
}

function pageSetToken(tokenKey, value) {
  localStorage.setItem(tokenKey, value);
  return true;
}

async function pageListAll(api, secret, path, tokenKey, status, pageSize, maxPages) {
  // Same signing as tevi-business-web src/models/api.js:
  // verify = <ts>-base64(HMAC_SHA256(secret, pathname + ts))
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const items = (body) => {
    if (Array.isArray(body)) return body;
    for (const k of ["data", "items", "results", "rows"]) {
      if (body && Array.isArray(body[k])) return body[k];
      if (body && body[k] && typeof body[k] === "object") return items(body[k]);
    }
    throw new Error("unexpected response shape: " + JSON.stringify(body).slice(0, 200));
  };
  const out = [];
  for (let page = 0; page < maxPages; page++) {
    const v = (JSON.parse(localStorage.getItem(tokenKey) || "{}") || {}).value;
    if (!v || !v.accessToken) return { error: "not logged in" };
    const url = new URL(api + path);
    url.searchParams.set("status", status);
    url.searchParams.set("offset", String(page * pageSize));
    url.searchParams.set("limit", String(pageSize));
    const ts = Math.floor(Date.now() / 1000);
    const mac = await crypto.subtle.sign("HMAC", key, enc.encode(url.pathname + ts));
    url.searchParams.set("verify", `${ts}-${btoa(String.fromCharCode(...new Uint8Array(mac)))}`);
    let r;
    try {
      r = await fetch(url, { headers: {
        accept: "application/json, text/plain, */*",
        authorization: "Bearer " + v.accessToken,
      } });
    } catch (e) {
      return { error: `${status} page ${page}: ${e} (Cloudflare challenge or network?)` };
    }
    if (r.status !== 200) {
      return { error: `${status} page ${page}: HTTP ${r.status} ${(await r.text()).slice(0, 200)}` };
    }
    const batch = items(await r.json());
    out.push(...batch);
    if (batch.length < pageSize) return { items: out };
  }
  return { error: `more than ${maxPages * pageSize} ${status} rows` };
}

// --- tab handling -------------------------------------------------------------

async function ensureTab() {
  const tabs = await chrome.tabs.query({ url: BUSINESS_URL + "*" });
  if (tabs.length) return tabs[0];
  const tab = await chrome.tabs.create({ url: BUSINESS_URL, pinned: true, active: false });
  await waitComplete(tab.id);
  return tab;
}

function waitComplete(tabId, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { done(); reject(new Error("tab load timeout")); }, timeoutMs);
    const listener = (id, info) => { if (id === tabId && info.status === "complete") { done(); resolve(); } };
    const done = () => { clearTimeout(timer); chrome.tabs.onUpdated.removeListener(listener); };
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((t) => { if (t.status === "complete") { done(); resolve(); } });
  });
}

async function inPage(tabId, func, args = []) {
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", func, args });
  return res && res.result;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function tokenState(tabId) {
  try {
    return await inPage(tabId, pageTokenState, [TOKEN_KEY]);
  } catch (e) {
    return { loggedIn: false, uid: "", error: String(e) };
  }
}

// Reload so the SPA refreshes an expired access token, then wait for a valid one.
async function reloadAndWaitLogin(tabId) {
  await chrome.tabs.reload(tabId);
  await waitComplete(tabId);
  for (let i = 0; i < 20; i++) {
    const st = await tokenState(tabId);
    if (st.loggedIn) { await sleep(2000); return st; } // let the post-login redirect settle
    await sleep(1000);
  }
  return tokenState(tabId);
}

// --- bridge calls -------------------------------------------------------------

async function bridge(cfg, method, path, body) {
  const r = await fetch(cfg.bridgeUrl.replace(/\/$/, "") + path, {
    method,
    headers: { "X-Mekoo-Token": cfg.token, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`bridge ${path}: HTTP ${r.status} ${data.error || ""}`);
  return data;
}

async function runJob(cfg, job) {
  const tab = await ensureTab();
  try {
    const st = await reloadAndWaitLogin(tab.id);
    if (!st.loggedIn) throw new Error("not logged in on business.tevi.com — paste a token in the options page");
    if (st.uid !== String(job.agency_id)) throw new Error(`logged in as uid ${st.uid}, expected ${job.agency_id}`);
    const result = { nonce: job.nonce, market: cfg.market, uid: st.uid };
    for (const status of ["Open", "Fail"]) {
      const res = await inPage(tab.id, pageListAll,
        [job.api_domain, job.secret, TX_PATH, TOKEN_KEY, status, job.page_size, MAX_PAGES]);
      if (!res || res.error) throw new Error(res ? res.error : `${status}: no result from page`);
      result[status.toLowerCase()] = res.items;
    }
    await bridge(cfg, "POST", "/ext/result", result);
    await setStatus({ lastJobAt: Date.now(), lastJob: `ok — ${result.open.length} Open, ${result.fail.length} Fail`, lastError: "" });
  } catch (e) {
    await setStatus({ lastJobAt: Date.now(), lastJob: "error", lastError: String(e.message || e) });
    await bridge(cfg, "POST", "/ext/error", { nonce: job.nonce, market: cfg.market, message: String(e.message || e) })
      .catch(() => {});
  }
}

async function poll() {
  if (busy) return;
  busy = true;
  try {
    const cfg = await getConfig();
    if (!cfg.market || !cfg.token) {
      await setStatus({ lastPollAt: Date.now(), lastError: "set market + token in the options page" });
      return;
    }
    const tab = await ensureTab();
    const st = await tokenState(tab.id);
    const q = new URLSearchParams({
      market: cfg.market, uid: st.uid || "", logged_in: st.loggedIn ? "1" : "0",
      v: chrome.runtime.getManifest().version,
    });
    const res = await bridge(cfg, "GET", "/ext/poll?" + q);
    await setStatus({ lastPollAt: Date.now(), uid: st.uid, loggedIn: st.loggedIn, lastPollError: "" });
    if (res.job) await runJob(cfg, res.job);
  } catch (e) {
    await setStatus({ lastPollAt: Date.now(), lastPollError: String(e.message || e) });
  } finally {
    busy = false;
  }
}

// --- wiring -------------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => { schedulePoll(); poll(); });
chrome.runtime.onStartup.addListener(() => { schedulePoll(); poll(); });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "poll") poll(); });
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
chrome.storage.onChanged.addListener((changes) => { if (changes.pollSeconds) schedulePoll(); });

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    if (msg.type === "seedToken") {
      // Log the tab in with the app's own localStorage entry, copied via
      // copy(localStorage.getItem('user_token_identify')) on a logged-in browser.
      JSON.parse(msg.value); // reject garbage before touching the page
      const tab = await ensureTab();
      await inPage(tab.id, pageSetToken, [TOKEN_KEY, msg.value]);
      const st = await reloadAndWaitLogin(tab.id);
      await setStatus({ uid: st.uid, loggedIn: st.loggedIn });
      return st;
    }
    if (msg.type === "pollNow") { await poll(); return { ok: true }; }
    return { error: "unknown message" };
  })().then(reply, (e) => reply({ error: String(e.message || e) }));
  return true; // async reply
});
