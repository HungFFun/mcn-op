# mekoo-op payout worker (Chrome extension)

Fetches the agency's Open and Fail payout requests from business.tevi.com for
`payout.run --source extension`. The agency API (`wapi.flowstreamx.com`) sits
behind a Cloudflare bot check that blocks every non-browser client, so the
request has to come from a real, logged-in Chrome.

mekoo-op stays the only scheduler. The extension polls the bridge
(`payout/ext_bridge.py`, port 8788) every 30 s. A poll only reaches mekoo-op on
the same machine; Tevi's API is called only when a job is queued. The poll also
acts as the heartbeat: the bot's Status shows it and alerts after 10 min of
silence.

## Setup (Ubuntu desktop, mekoo-op in Docker)

1. **mekoo-op `.env`**
   ```
   PAYOUT_SOURCE=extension
   EXT_BRIDGE_TOKEN=<python3 -c "import secrets; print(secrets.token_urlsafe(32))">
   SIGN_REQUEST_SECRET_DATA=<same value as tevi-business-web/.env.prod>
   ```
   Then run `docker compose up -d --build`. The bridge is published on
   `127.0.0.1:8788` only.
2. **Chrome windows.** Run `scripts/agency-chrome.sh autostart`. It opens one
   Chrome per agency, each with its own profile in `~/.config/mekoo-chrome/<market>`,
   and starts them again at every login. Use Google Chrome from the `.deb`, not
   the Chromium snap.
3. **In each window:**
   1. Open `chrome://extensions` and turn on Developer mode.
   2. Click **Load unpacked** and select this folder.
   3. Open the extension's options (click its toolbar icon) and set the market
      (`id` / `ph`) and the bridge token. Leave the bridge URL at
      `http://127.0.0.1:8788`.
   4. Log in. Either log in to business.tevi.com normally with that market's
      agency account, or paste a token in **Log in with a token**. To get the
      token, run `copy(localStorage.getItem('user_token_identify'))` in DevTools
      on a browser already logged in as that agency.
   5. Status should show `logged in: yes`, the agency's uid, and a recent poll.
4. **Keep the machine awake.**
   ```
   gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
   ```
   A locked screen is fine; suspend is not.

Check from Telegram: **Status** shows `🧩 Extension ID/PH: 🟢`. A dry run with
**Run → Payout (chạy thử)** proves the whole path.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `🔴 mất kết nối` / `no result within 300s` | Chrome window closed, machine asleep | `scripts/agency-chrome.sh` |
| `🟡 chưa đăng nhập` | refresh token expired (~6 months) or logged out | log in again in that window |
| `🟡 sai tài khoản` | wrong agency logged in to that profile | log out, log in with the right agency |
| `extension error: … Failed to fetch (Cloudflare challenge…)` | Cloudflare challenged the session | open the window, pass the check, then run again |
| `bridge …: HTTP 401` (options status) | token in options ≠ `EXT_BRIDGE_TOKEN` | fix one side |

The extension updates when you reload it on `chrome://extensions` after a
`git pull`.
