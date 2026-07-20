"""Local web UI to manage the ops tasks and read their logs.

    python3 -m webui                     # http://127.0.0.1:8787
    python3 -m webui --port 9000 --host 127.0.0.1

Pure stdlib (http.server) — no extra dependencies. Reuses scheduler.TASKS /
SCHEDULE / LOG_DIR as the single source of truth, so runs triggered here log to
the same run/logs/<task>.log the scheduler writes.

Endpoints:
  GET  /                 dashboard (HTML)
  GET  /api/tasks        task list + schedule + last-run + scheduler heartbeat
  GET  /api/log?task=X&lines=N   tail of run/logs/X.log
  GET  /api/running      launches started via this UI (with live status)
  POST /api/run          JSON {task, no_send} → launch a task subprocess

Binds 127.0.0.1 by default. "Run" performs the REAL task (payout/report sends to
Telegram); "Dry-run" appends --no-send. Do not expose this to a public interface.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import scheduler

PROJECT_ROOT = scheduler.PROJECT_ROOT
LOG_DIR = scheduler.LOG_DIR

# Runnable tasks come straight from the scheduler (includes the config check).
TASKS = scheduler.TASKS
NO_SEND = scheduler.NO_SEND
UI_ORDER = ["payout", "mcn-report", "daily-recap", "leave-watch", "check"]

_launches: list[dict] = []   # {pid, task, no_send, started, popen}


def schedule_for(key: str) -> list[str]:
    return scheduler.load_schedule().get(key, [])


def last_run(key: str) -> dict | None:
    f = LOG_DIR / f"{key}.log"
    if not f.exists():
        return None
    st = f.stat()
    return {"mtime": st.st_mtime, "size": st.st_size}


def tail(key: str, lines: int) -> str:
    f = LOG_DIR / f"{key}.log"
    if not f.exists():
        return ""
    data = f.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


def scheduler_status() -> dict:
    hb = scheduler.HEARTBEAT
    if not hb.exists():
        return {"alive": False, "last_beat": None, "age": None}
    try:
        beat = hb.read_text(encoding="utf-8").strip()
        dt = datetime.fromisoformat(beat)
        age = (scheduler.now_ict() - dt).total_seconds()
        return {"alive": age < 90, "last_beat": beat, "age": round(age)}
    except (OSError, ValueError):
        return {"alive": False, "last_beat": None, "age": None}


def launch(key: str, no_send: bool) -> int:
    cmd = scheduler.task_cmd(key, no_send)   # frozen-aware (dev: -m; exe: --task)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"{key}.log"
    stamp = scheduler.now_ict().strftime("%Y-%m-%d %H:%M:%S %Z")
    fh = open(logfile, "a", encoding="utf-8")
    fh.write(f"\n===== {stamp} — UI run {key}{' (--no-send)' if no_send and key in NO_SEND else ''} =====\n")
    fh.flush()
    p = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=fh, stderr=subprocess.STDOUT)
    _launches.append({"pid": p.pid, "task": key, "no_send": bool(no_send and key in NO_SEND),
                      "started": scheduler.now_ict().isoformat(timespec="seconds"), "popen": p})
    del _launches[:-50]  # keep the last 50
    return p.pid


def running() -> list[dict]:
    out = []
    for r in _launches:
        p = r["popen"]
        rc = p.poll()
        out.append({"pid": r["pid"], "task": r["task"], "no_send": r["no_send"],
                    "started": r["started"], "alive": rc is None, "returncode": rc})
    return out[::-1]


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>mekoo-op · Task Manager</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
         background:#0f1419; color:#e6e9ee; }
  @media (prefers-color-scheme: light){ body{ background:#f5f6f8; color:#1b1f24; } }
  header { padding:16px 20px; border-bottom:1px solid #2a3038; display:flex; align-items:center;
           gap:14px; flex-wrap:wrap; }
  @media (prefers-color-scheme: light){ header{ border-color:#e2e5ea; } }
  h1 { font-size:16px; margin:0; font-weight:700; }
  .muted { color:#8b95a3; }
  .pill { padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600; }
  .up { background:#123d22; color:#5ad07f; } .down { background:#3d1212; color:#ff8f8f; }
  main { padding:20px; display:grid; grid-template-columns: minmax(320px,420px) 1fr; gap:20px; align-items:start; }
  @media (max-width:820px){ main{ grid-template-columns:1fr; } }
  .card { background:#161c24; border:1px solid #262d38; border-radius:12px; padding:14px 16px; margin-bottom:12px; }
  @media (prefers-color-scheme: light){ .card{ background:#fff; border-color:#e2e5ea; } }
  .task { display:flex; flex-direction:column; gap:8px; }
  .task .row1 { display:flex; align-items:center; gap:10px; justify-content:space-between; }
  .task .name { font-weight:700; font-size:15px; }
  .times { font-size:12px; }
  .times b { color:#7aa2ff; }
  .btns { display:flex; gap:8px; flex-wrap:wrap; }
  button { font:inherit; border:1px solid #33404f; background:#1f2937; color:#e6e9ee; border-radius:8px;
           padding:6px 12px; cursor:pointer; font-weight:600; }
  button:hover { border-color:#4b5a6d; }
  button.run { background:#1d4ed8; border-color:#1d4ed8; color:#fff; }
  button.dry { background:transparent; }
  button.log { background:transparent; color:#8b95a3; }
  button:disabled { opacity:.5; cursor:default; }
  @media (prefers-color-scheme: light){ button{ background:#f0f2f5; color:#1b1f24; border-color:#d3d8e0; }
    button.run{ background:#1d4ed8; color:#fff; } }
  .logwrap { position:sticky; top:20px; }
  .loghead { display:flex; align-items:center; gap:10px; margin-bottom:8px; flex-wrap:wrap; }
  pre#log { background:#0b0e13; border:1px solid #262d38; border-radius:10px; padding:12px;
            height:70vh; overflow:auto; white-space:pre-wrap; word-break:break-word; font-size:12.5px;
            font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; margin:0; }
  @media (prefers-color-scheme: light){ pre#log{ background:#0b0e13; color:#e6e9ee; } }
  label.chk { font-size:12px; color:#8b95a3; display:flex; align-items:center; gap:5px; cursor:pointer; }
  #toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%); background:#1d4ed8; color:#fff;
           padding:9px 16px; border-radius:8px; font-weight:600; opacity:0; transition:opacity .2s; pointer-events:none; }
  #toast.show { opacity:1; }
  .ago { font-size:12px; color:#8b95a3; }
</style></head>
<body>
<header>
  <h1>mekoo-op · Task Manager</h1>
  <span id="sched" class="pill down">scheduler: …</span>
  <span class="muted" id="clock"></span>
</header>
<main>
  <section id="tasks"></section>
  <section class="logwrap">
    <div class="loghead">
      <b id="logtitle">Log</b>
      <label class="chk"><input type="checkbox" id="follow" checked> auto-refresh</label>
      <select id="lines"><option>200</option><option selected>500</option><option>2000</option></select>
      <button class="log" onclick="refreshLog()">reload</button>
    </div>
    <pre id="log" class="muted">Select a task's “Log” to view its output…</pre>
  </section>
</main>
<div id="toast"></div>
<script>
let current = null;
const $ = s => document.querySelector(s);

function toast(m){ const t=$("#toast"); t.textContent=m; t.classList.add("show"); setTimeout(()=>t.classList.remove("show"),2200); }
function ago(sec){ if(sec==null) return "never"; if(sec<60) return sec+"s ago"; if(sec<3600) return Math.floor(sec/60)+"m ago"; if(sec<86400) return Math.floor(sec/3600)+"h ago"; return Math.floor(sec/86400)+"d ago"; }

async function loadTasks(){
  const r = await fetch("api/tasks"); const d = await r.json();
  const s = $("#sched");
  if(d.scheduler.alive){ s.className="pill up"; s.textContent="scheduler: up ("+ago(d.scheduler.age)+")"; }
  else { s.className="pill down"; s.textContent="scheduler: down"; }
  $("#clock").textContent = "ICT "+d.now.replace("T"," ").slice(0,16);
  const box = $("#tasks"); box.innerHTML="";
  for(const t of d.tasks){
    const times = t.schedule.length ? t.schedule.map(x=>"<b>"+x+"</b>").join(" · ") : '<span class="muted">on demand</span>';
    const lr = t.last_run ? ago(Math.round(Date.now()/1000 - t.last_run.mtime)) : "no log yet";
    const div = document.createElement("div"); div.className="card task";
    div.innerHTML =
      '<div class="row1"><span class="name">'+t.key+'</span><span class="ago">'+lr+'</span></div>'+
      '<div class="times">'+times+'</div>'+
      '<div class="btns">'+
        '<button class="run" onclick="run(\''+t.key+'\',false)">Run</button>'+
        (t.no_send ? '<button class="dry" onclick="run(\''+t.key+'\',true)">Dry-run</button>' : '')+
        '<button class="log" onclick="viewLog(\''+t.key+'\')">Log</button>'+
      '</div>';
    box.appendChild(div);
  }
}
async function run(task, noSend){
  if(!noSend && !confirm("Run '"+task+"' for real? This may send to Telegram.")) return;
  const r = await fetch("api/run",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({task, no_send:noSend})});
  const d = await r.json();
  if(d.ok){ toast("Started "+task+(noSend?" (dry-run)":"")+" · pid "+d.pid); viewLog(task); }
  else toast("Error: "+(d.error||"failed"));
}
async function viewLog(task){ current=task; $("#logtitle").textContent="Log · "+task; await refreshLog(); }
async function refreshLog(){
  if(!current) return;
  const n = $("#lines").value;
  const r = await fetch("api/log?task="+encodeURIComponent(current)+"&lines="+n);
  const d = await r.json();
  const pre = $("#log"); pre.classList.remove("muted");
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.textContent = d.text || "(empty)";
  if(atBottom) pre.scrollTop = pre.scrollHeight;
}
setInterval(loadTasks, 5000);
setInterval(()=>{ if(current && $("#follow").checked) refreshLog(); }, 3000);
loadTasks();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html")
        if u.path == "/api/tasks":
            tasks = [{"key": k, "schedule": schedule_for(k),
                      "no_send": k in NO_SEND, "last_run": last_run(k)}
                     for k in UI_ORDER if k in TASKS]
            return self._send(200, json.dumps({
                "now": scheduler.now_ict().isoformat(timespec="seconds"),
                "scheduler": scheduler_status(), "tasks": tasks}))
        if u.path == "/api/log":
            q = parse_qs(u.query)
            task = (q.get("task") or [""])[0]
            lines = int((q.get("lines") or ["500"])[0])
            if task not in TASKS:
                return self._send(400, json.dumps({"error": "unknown task"}))
            return self._send(200, json.dumps({"task": task, "text": tail(task, lines)}))
        if u.path == "/api/running":
            return self._send(200, json.dumps({"running": running()}))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/run":
            return self._send(404, json.dumps({"error": "not found"}))
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or "{}")
        except json.JSONDecodeError:
            return self._send(400, json.dumps({"error": "bad json"}))
        task = body.get("task")
        if task not in TASKS:
            return self._send(400, json.dumps({"error": "unknown task"}))
        try:
            pid = launch(task, bool(body.get("no_send")))
        except Exception as exc:  # noqa: BLE001
            return self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return self._send(200, json.dumps({"ok": True, "pid": pid}))


def main() -> None:
    ap = argparse.ArgumentParser(description="mekoo-op task manager web UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Task Manager UI → http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
