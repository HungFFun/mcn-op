"""Runtime settings loaded from env files + environment variables.

Nothing secret is stored in the repo. Credentials are read at runtime from,
in increasing priority:

  1. ~/.claude/secrets/clickhouse.env        (ClickHouse connection)
     ~/.claude/secrets/telegram_payout.env   (Telegram bot — reused from the MCP)
  2. <project>/.env                           (optional local override)
  3. real process environment variables       (highest priority)

Override the file locations with $CLICKHOUSE_ENV / $TELEGRAM_ENV / $PAYOUT_ENV.
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
    Path(os.environ.get("PAYOUT_ENV", _PROJECT_ROOT / ".env")),
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


def get(key: str, default: str = "") -> str:
    return _CFG.get(key, default)


# --- ClickHouse --------------------------------------------------------------
CLICKHOUSE = {
    "host": get("CLICKHOUSE_HOST", "172.16.0.64"),
    "port": int(get("CLICKHOUSE_PORT", "8123")),
    "username": get("CLICKHOUSE_USER", "default"),
    "password": get("CLICKHOUSE_PASSWORD", ""),
    "database": get("CLICKHOUSE_DATABASE", "tevi_fiat"),
    "secure": get("CLICKHOUSE_SECURE", "false").lower() in ("1", "true", "yes"),
}

# --- Telegram (reuses the tevi-telegram MCP credentials file) ----------------
TELEGRAM = {
    "bot_token": get("TELEGRAM_BOT_TOKEN", ""),
    "chat_id": {
        "id": get("TELEGRAM_CHAT_ID_ID", ""),
        "ph": get("TELEGRAM_CHAT_ID_PH", ""),
    },
    "mention": {
        "id": get("TELEGRAM_MENTION", ""),
        "ph": get("TELEGRAM_MENTION_PH", get("TELEGRAM_MENTION", "")),
    },
}


# --- Live agency API via the Chrome extension (payout.run --source extension) --
# See payout/ext_bridge.py and chrome-extension/README.md.
STATE_DIR = Path(get("MEKOO_STATE_DIR", str(_PROJECT_ROOT / "run" / "state")))
EXT = {
    # Default for payout.run --source (scheduler/bot runs use this).
    "source": get("PAYOUT_SOURCE", "clickhouse"),
    # Bridge HTTP server the extension polls. In Docker bind 0.0.0.0 inside the
    # container and publish only on the host's 127.0.0.1 (docker-compose.yml).
    "host": get("EXT_BRIDGE_HOST", "127.0.0.1"),
    "port": int(get("EXT_BRIDGE_PORT", "8788")),
    # Shared secret the extension sends as X-Mekoo-Token (set it in its options).
    "token": get("EXT_BRIDGE_TOKEN", ""),
    "job_timeout": int(get("EXT_JOB_TIMEOUT", "300")),
    # Request signing, handed to the extension with each job. Same value as
    # SIGN_REQUEST_SECRET_DATA in tevi-business-web/.env.prod.
    "api_domain": get("W_API_DOMAIN", "https://wapi.flowstreamx.com"),
    "sign_secret": get("SIGN_REQUEST_SECRET_DATA", ""),
}


def require_extension() -> None:
    missing = [k for k, v in (("EXT_BRIDGE_TOKEN", EXT["token"]),
                              ("SIGN_REQUEST_SECRET_DATA", EXT["sign_secret"])) if not v]
    if missing:
        raise SystemExit("Missing extension settings: " + ", ".join(missing) +
                         f" (set them in {_PROJECT_ROOT / '.env'}).")


def require_clickhouse() -> None:
    missing = [k for k in ("host", "username", "database") if not CLICKHOUSE.get(k)]
    if not CLICKHOUSE.get("password"):
        missing.append("password")
    if missing:
        raise SystemExit(
            "Missing ClickHouse settings: " + ", ".join(missing) +
            f"\nFill them in {_SECRETS / 'clickhouse.env'} (see .env.example)."
        )


def require_telegram(market: str) -> None:
    problems = []
    if not TELEGRAM["bot_token"]:
        problems.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM["chat_id"].get(market):
        problems.append(f"TELEGRAM_CHAT_ID_{market.upper()}")
    if problems:
        raise SystemExit("Missing Telegram settings: " + ", ".join(problems))
