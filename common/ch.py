"""Generic ClickHouse access over the HTTP interface (clickhouse-connect).

Runs arbitrary SQL and returns rows as a list of dicts keyed by column name.
Used by the warehouse-backed tasks (MCN report, leave-watch, daily recap).
"""

from __future__ import annotations

from typing import Any

from . import env


def _client():
    try:
        import clickhouse_connect  # noqa: WPS433 (runtime import keeps dep optional)
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "clickhouse-connect is not installed. Run:\n"
            "    pip3 install -r requirements.txt --break-system-packages"
        ) from exc

    env.require_clickhouse()
    cfg = env.CLICKHOUSE
    return clickhouse_connect.get_client(
        host=cfg["host"],
        port=cfg["port"],
        username=cfg["username"],
        password=cfg["password"],
        database=cfg["database"],
        secure=cfg["secure"],
        connect_timeout=15,
        query_limit=0,
    )


def query(sql: str) -> list[dict[str, Any]]:
    """Execute SQL and return a list of {column: value} dicts."""
    client = _client()
    try:
        res = client.query(sql)
        cols = list(res.column_names)
        return [dict(zip(cols, row)) for row in res.result_rows]
    finally:
        client.close()


def query_one(sql: str) -> dict[str, Any]:
    """Execute SQL expected to return a single row; returns {} if empty."""
    rows = query(sql)
    return rows[0] if rows else {}
