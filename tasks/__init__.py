"""Warehouse-backed scheduled tasks, ported from the tevi Cowork skills to run
locally with no LLM in the loop (pure ClickHouse -> format -> Telegram).

Each module exposes a ``main()`` and runs standalone:

    python3 -m tasks.mcn_daily_report      # MCN Mekoo health report
    python3 -m tasks.mcn_leave_watch       # leave-request alert (deduped)
    python3 -m tasks.daily_recap           # yesterday's payout recap (ID + PH)

Common flags: --no-send (query but don't post), --sample (offline format test).
"""
