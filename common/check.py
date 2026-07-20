"""Config diagnostic — verify credentials are wired without touching the DB.

    python3 -m common.check          # show resolved config (no network)
    python3 -m common.check --live   # also ping ClickHouse + Telegram getMe
"""

from __future__ import annotations

import argparse

from . import env


def _mask(v: str) -> str:
    if not v:
        return "(unset)"
    return v[:4] + "…" + v[-3:] if len(v) > 10 else "set"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="also test ClickHouse connectivity + Telegram getMe")
    args = ap.parse_args()

    print("== ClickHouse ==")
    ch = env.CLICKHOUSE
    print(f"  host       : {ch['host']}:{ch['port']}  secure={ch['secure']}")
    print(f"  user       : {ch['username']}")
    print(f"  database   : {ch['database']}  (warehouse tasks query tevi_warehouse.*)")
    print(f"  password   : {'set' if ch['password'] else '(unset)'}")

    print("== Telegram (recap bot) ==")
    print(f"  bot token  : {_mask(env.RECAP_BOT_TOKEN)}")
    print(f"  chat Mekoo : {env.CHAT_MEKOO}")
    print(f"  chat ID/PH : {env.CHAT_RECAP_ID} / {env.CHAT_RECAP_PH}")
    print(f"  leave cc   : {env.LEAVE_WATCH_CC}")

    print("== Business ==")
    print(f"  MCN id     : {env.MEKOO_MCN_ID}")
    print(f"  agencies   : ID={env.AGENCY_ID_ID}  PH={env.AGENCY_ID_PH}")
    print(f"  state dir  : {env.STATE_DIR}")

    if not args.live:
        print("\n(add --live to test connectivity)")
        return

    print("\n== Live checks ==")
    try:
        from . import ch as chmod
        r = chmod.query_one("SELECT version() AS v, now('Asia/Ho_Chi_Minh') AS ts")
        print(f"  ClickHouse : OK  version={r.get('v')}  now={r.get('ts')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ClickHouse : FAIL  {type(exc).__name__}: {exc}")

    try:
        import requests
        env.require_recap_bot()
        me = requests.get(
            f"https://api.telegram.org/bot{env.RECAP_BOT_TOKEN}/getMe", timeout=15
        ).json()
        if me.get("ok"):
            print(f"  Telegram   : OK  bot=@{me['result'].get('username')}")
        else:
            print(f"  Telegram   : FAIL  {me}")
    except Exception as exc:  # noqa: BLE001
        print(f"  Telegram   : FAIL  {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
