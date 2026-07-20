"""Runtime configuration loaded from env files + real environment variables.

Nothing secret lives in the repo. Values are merged in increasing priority:

  1. ~/.claude/secrets/clickhouse.env        (ClickHouse connection)
     ~/.claude/secrets/telegram_payout.env   (payout bot — reused by payout/)
     ~/.claude/secrets/telegram_recap.env    (recap bot — used by the tasks here)
  2. <project>/.env                           (optional local override)
  3. real process environment variables       (highest priority)

Override any file location with the matching $*_ENV variable below.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_HOME = Path.home()
_SECRETS = _HOME / ".claude" / "secrets"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Candidate env files, lowest priority first.
_ENV_FILES = [
    Path(os.environ.get("CLICKHOUSE_ENV", _SECRETS / "clickhouse.env")),
    Path(os.environ.get("TELEGRAM_ENV", _SECRETS / "telegram_payout.env")),
    Path(os.environ.get("TELEGRAM_RECAP_ENV", _SECRETS / "telegram_recap.env")),
    Path(os.environ.get("MEKOO_ENV", _PROJECT_ROOT / ".env")),
]


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = _clean_value(val)
    return out


def _clean_value(val: str) -> str:
    """Strip surrounding quotes and any trailing inline `# comment`.

    A quoted value is taken up to its closing quote (so `"@x"  # note` -> `@x`).
    An unquoted value drops a comment only when it starts after whitespace, so a
    literal `#` inside a password is preserved.
    """
    val = val.strip()
    if val[:1] in ("'", '"'):
        quote = val[0]
        end = val.find(quote, 1)
        return val[1:end] if end != -1 else val[1:]
    hash_pos = re.search(r"\s#", val)
    if hash_pos:
        val = val[: hash_pos.start()]
    return val.strip()


def _load() -> dict[str, str]:
    merged: dict[str, str] = {}
    for f in _ENV_FILES:
        merged.update(_parse_env_file(f))
    merged.update(os.environ)  # real env wins
    return merged


_CFG = _load()

PROJECT_ROOT = _PROJECT_ROOT
STATE_DIR = Path(os.environ.get("MEKOO_STATE_DIR", _PROJECT_ROOT / "run" / "state"))


def get(key: str, default: str = "") -> str:
    return _CFG.get(key, default)


# --- ClickHouse --------------------------------------------------------------
# Note: MCN / recap queries read from the ``tevi_warehouse`` database using
# fully-qualified table names, so the connected default database does not matter.
CLICKHOUSE = {
    "host": get("CLICKHOUSE_HOST", "172.16.0.64"),
    "port": int(get("CLICKHOUSE_PORT", "8123")),
    "username": get("CLICKHOUSE_USER", "default"),
    "password": get("CLICKHOUSE_PASSWORD", ""),
    "database": get("CLICKHOUSE_DATABASE", "tevi_fiat"),
    "secure": get("CLICKHOUSE_SECURE", "false").lower() in ("1", "true", "yes"),
}

# --- Telegram: recap bot (@mekoo_ops_reporter_bot) ---------------------------
# The three warehouse tasks post through the recap bot, NOT the payout bot.
RECAP_BOT_TOKEN = (
    get("TELEGRAM_RECAP_BOT_TOKEN")
    or get("TELEGRAM_RECAP_TOKEN")
    or get("RECAP_BOT_TOKEN")
)

# Group chat IDs (hardcoded in the original skills; overridable via env).
CHAT_MEKOO = get("TELEGRAM_CHAT_MEKOO", "-1003553250473")   # MCN report + leave-watch
CHAT_RECAP_ID = get("TELEGRAM_CHAT_RECAP_ID", "-4609230789")  # daily-recap ID group
CHAT_RECAP_PH = get("TELEGRAM_CHAT_RECAP_PH", "-5293108578")  # daily-recap PH group

# Mention handle appended to leave-watch alerts.
LEAVE_WATCH_CC = get("TELEGRAM_LEAVE_CC", "@banned_tv1")

# --- Control bot (Telegram remote control: schedule / run / logs) ------------
# Its own bot (from @BotFather); falls back to the recap bot token if unset.
# Only chat ids in CONTROL_ALLOWED may issue commands (comma-separated). Empty =
# locked down: the bot replies only to /start with the caller's id to allow-list.
CONTROL_BOT_TOKEN = (
    get("TELEGRAM_CONTROL_BOT_TOKEN")
    or get("CONTROL_BOT_TOKEN")
    or RECAP_BOT_TOKEN
)
CONTROL_ALLOWED = [x.strip() for x in get("TELEGRAM_CONTROL_CHAT_IDS", "").split(",") if x.strip()]


def require_control_bot() -> None:
    if not CONTROL_BOT_TOKEN:
        raise SystemExit(
            "Missing control bot token. Set TELEGRAM_CONTROL_BOT_TOKEN in "
            f"{_SECRETS / 'telegram_recap.env'} (or reuse the recap bot). See .env.example."
        )

# --- Business constants ------------------------------------------------------
MEKOO_MCN_ID = get("MEKOO_MCN_ID", "0edb1271-3db6-4f70-a84d-58b1029bad40")
TIMEZONE = "Asia/Ho_Chi_Minh"

# Payout agency IDs (used by the daily recap, which spans both markets).
AGENCY_ID_ID = get("AGENCY_ID_ID", "1711465765")
AGENCY_ID_PH = get("AGENCY_ID_PH", "3635488650")


def require_clickhouse() -> None:
    missing = [k for k in ("host", "username", "database") if not CLICKHOUSE.get(k)]
    if not CLICKHOUSE.get("password"):
        missing.append("password")
    if missing:
        raise SystemExit(
            "Missing ClickHouse settings: " + ", ".join(missing)
            + f"\nFill them in {_SECRETS / 'clickhouse.env'} (see .env.example)."
        )


def require_recap_bot() -> None:
    if not RECAP_BOT_TOKEN:
        raise SystemExit(
            "Missing recap bot token. Set TELEGRAM_RECAP_BOT_TOKEN in "
            f"{_SECRETS / 'telegram_recap.env'} (see .env.example)."
        )
