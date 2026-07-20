"""Minimal Telegram Bot API sender for the recap bot (@mekoo_ops_reporter_bot).

No MCP involved — posts directly over HTTPS. Credentials come from common.env.
"""

from __future__ import annotations

from typing import Any

from . import env

_API = "https://api.telegram.org/bot{token}/{method}"


def _requests():
    try:
        import requests  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "requests is not installed. Run:\n"
            "    pip3 install -r requirements.txt --break-system-packages"
        ) from exc
    return requests


def send_message(chat_id: str, text: str, parse_mode: str = "HTML",
                 token: str | None = None, disable_preview: bool = True) -> dict[str, Any]:
    """Send a text message via the recap bot. Returns Telegram's result dict.

    Raises RuntimeError if the API responds with ok=false (does NOT retry).
    """
    env.require_recap_bot()
    requests = _requests()
    token = token or env.RECAP_BOT_TOKEN
    resp = requests.post(
        _API.format(token=token, method="sendMessage"),
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true" if disable_preview else "false",
        },
        timeout=30,
    ).json()
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {resp}")
    return resp["result"]
