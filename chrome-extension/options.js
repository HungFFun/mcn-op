const FIELDS = ["market", "bridgeUrl", "token", "pollSeconds"];
const DEFAULTS = { market: "", bridgeUrl: "http://127.0.0.1:8788", token: "", pollSeconds: 30 };
const $ = (id) => document.getElementById(id);
const fmt = (ms) => (ms ? new Date(ms).toLocaleString() : "—");

async function load() {
  const cfg = { ...DEFAULTS, ...(await chrome.storage.local.get(FIELDS)) };
  for (const f of FIELDS) $(f).value = cfg[f];
  renderStatus();
}

async function renderStatus() {
  const { status = {} } = await chrome.storage.local.get("status");
  $("status").textContent = [
    `logged in : ${status.loggedIn ? "yes" : "no"}  (uid ${status.uid || "—"})`,
    `last poll : ${fmt(status.lastPollAt)}  ${status.lastPollError || "ok"}`,
    `last job  : ${fmt(status.lastJobAt)}  ${status.lastJob || "—"}`,
    status.lastError ? `job error : ${status.lastError}` : "",
  ].filter(Boolean).join("\n");
}

$("save").onclick = async () => {
  await chrome.storage.local.set({
    market: $("market").value,
    bridgeUrl: $("bridgeUrl").value.trim() || DEFAULTS.bridgeUrl,
    token: $("token").value.trim(),
    pollSeconds: Math.max(30, parseInt($("pollSeconds").value, 10) || 30),
  });
  $("save").textContent = "Saved ✓";
  setTimeout(() => ($("save").textContent = "Save"), 1500);
};

$("pollNow").onclick = async () => {
  await chrome.runtime.sendMessage({ type: "pollNow" });
  renderStatus();
};

$("seedBtn").onclick = async () => {
  const value = $("seed").value.trim();
  if (!value) return;
  $("seedBtn").textContent = "Logging in…";
  const res = await chrome.runtime.sendMessage({ type: "seedToken", value });
  $("seedBtn").textContent = "Log in";
  $("seed").value = "";
  alert(res.error ? `Failed: ${res.error}` : `Logged in: ${res.loggedIn ? "yes" : "no"}, uid ${res.uid}`);
  renderStatus();
};

chrome.storage.onChanged.addListener((c) => { if (c.status) renderStatus(); });
load();
