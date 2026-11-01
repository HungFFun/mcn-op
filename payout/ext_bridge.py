"""Bridge between payout.run and the mekoo-op Chrome extension.

The agency API sits behind a Cloudflare bot check that rejects any non-browser
client, so the data is fetched by an extension (chrome-extension/) running in a
normal Chrome that stays open 24/7, one profile per agency, logged in as that
agency. The extension is a pull worker; mekoo-op stays the only scheduler:

    payout.run --source extension
      → request_data(): write job_<market>.json (fresh nonce), wait for result
    extension, every 30s:
      GET  /ext/poll?market=ph&uid=…&logged_in=1   (also the heartbeat)
        ← {} or {"job": {nonce, agency_id, api_domain, secret, page_size}}
      → reloads its business.tevi.com tab, fetches every Open + Fail page
      POST /ext/result {nonce, market, uid, open: [...], fail: [...]}
      POST /ext/error  {nonce, market, message}
      → result_<nonce>.json → payout.run continues the normal flow

Jobs, results and heartbeats are files under <state>/ext/, so the HTTP server
(a thread in the bot process, see serve_in_thread) and the payout.run
subprocess never share memory. Every request must carry X-Mekoo-Token =
EXT_BRIDGE_TOKEN; a result is accepted only for the nonce of the pending job.

Standalone: python3 -m payout.ext_bridge   (serves until Ctrl-C)
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import settings
from .config import MARKETS

EXT_DIR = settings.STATE_DIR / "ext"
_MAX_BODY = 50 * 1024 * 1024
_RECLAIM_S = 180       # re-offer a claimed job the extension never answered
_PAGE_SIZE = 100


# --- file state ----------------------------------------------------------------

def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _job_path(market: str) -> Path:
    return EXT_DIR / f"job_{market}.json"


def _result_path(nonce: str) -> Path:
    return EXT_DIR / f"result_{nonce}.json"


def heartbeat(market: str) -> dict | None:
    """Last poll from the market's extension: {at, uid, logged_in, version}."""
    return _read_json(EXT_DIR / f"heartbeat_{market}.json")


def heartbeat_age(market: str) -> float | None:
    hb = heartbeat(market)
    return time.time() - hb["at"] if hb else None


# --- payout.run side -------------------------------------------------------------

def request_data(market: str, timeout: int | None = None) -> tuple[list[dict], list[dict]]:
    """Queue a job for the market's extension and wait for its Open + Fail rows."""
    agency_id = MARKETS[market]["agency_id"]
    timeout = timeout or settings.EXT["job_timeout"]
    nonce = secrets.token_hex(16)
    _write_json(_job_path(market), {"nonce": nonce, "market": market,
                                    "created": time.time(), "claimed_at": None})
    deadline = time.time() + timeout
    result_file = _result_path(nonce)
    try:
        while time.time() < deadline:
            res = _read_json(result_file)
            if res is not None:
                break
            time.sleep(2)
        else:
            age = heartbeat_age(market)
            seen = f"last poll {int(age)}s ago" if age is not None else "never polled"
            raise RuntimeError(f"[{market}] extension gave no result within {timeout}s "
                               f"({seen}) — is the {market.upper()} Chrome open?")
    finally:
        job = _read_json(_job_path(market))
        if job and job.get("nonce") == nonce:
            _job_path(market).unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)

    if res.get("error"):
        raise RuntimeError(f"[{market}] extension error: {res['error']}")
    uid = str(res.get("uid", ""))
    if uid != agency_id:
        raise RuntimeError(f"[{market}] extension is logged in as uid {uid}, "
                           f"expected agency {agency_id}")
    return res.get("open") or [], res.get("fail") or []


# --- HTTP side ---------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected = settings.EXT["token"]
        given = self.headers.get("X-Mekoo-Token", "")
        if not expected:
            self._send(503, {"error": "EXT_BRIDGE_TOKEN not configured"})
            return False
        if not hmac.compare_digest(given.encode(), expected.encode()):
            self._send(401, {"error": "bad token"})
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/ext/poll":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return None
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        market = q.get("market", "")
        if market not in MARKETS:
            return self._send(400, {"error": "unknown market"})
        _write_json(EXT_DIR / f"heartbeat_{market}.json", {
            "at": time.time(), "uid": q.get("uid", ""),
            "logged_in": q.get("logged_in") == "1", "version": q.get("v", ""),
        })
        job = _read_json(_job_path(market))
        if not job:
            return self._send(200, {})
        claimed = job.get("claimed_at")
        if claimed and time.time() - claimed < _RECLAIM_S:
            return self._send(200, {})
        job["claimed_at"] = time.time()
        _write_json(_job_path(market), job)
        return self._send(200, {"job": {
            "nonce": job["nonce"],
            "agency_id": MARKETS[market]["agency_id"],
            "api_domain": settings.EXT["api_domain"],
            "secret": settings.EXT["sign_secret"],
            "page_size": _PAGE_SIZE,
        }})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in ("/ext/result", "/ext/error"):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            return self._send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length) or "{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        market = body.get("market", "")
        job = _read_json(_job_path(market)) if market in MARKETS else None
        if not job or not hmac.compare_digest(str(body.get("nonce", "")), job["nonce"]):
            return self._send(409, {"error": "no pending job with this nonce"})
        if u.path == "/ext/error":
            result = {"error": str(body.get("message", "unknown"))[:500]}
        else:
            result = {"uid": str(body.get("uid", "")),
                      "open": body.get("open") or [], "fail": body.get("fail") or []}
        result["received_at"] = time.time()
        _write_json(_result_path(job["nonce"]), result)
        return self._send(200, {"ok": True})


def serve_in_thread() -> ThreadingHTTPServer:
    """Start the bridge on EXT_BRIDGE_HOST:EXT_BRIDGE_PORT in a daemon thread."""
    srv = ThreadingHTTPServer((settings.EXT["host"], settings.EXT["port"]), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> None:
    srv = ThreadingHTTPServer((settings.EXT["host"], settings.EXT["port"]), _Handler)
    print(f"Extension bridge → http://{settings.EXT['host']}:{settings.EXT['port']}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
