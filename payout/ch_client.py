"""Direct ClickHouse access over the HTTP interface (clickhouse-connect).

This replaces the ClickHouse MCP round-trip: the runner now opens its own
connection and executes the queries from queries.py itself. Returns results in
the same {"columns": [...], "rows": [...]} shape that parse.parse_rows accepts.
"""

from __future__ import annotations

from typing import Any

from . import settings
from . import queries


def _client():
    try:
        import clickhouse_connect  # noqa: WPS433 (runtime import so the dep is optional)
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "clickhouse-connect is not installed. Run:\n"
            "    pip3 install -r requirements.txt --break-system-packages"
        ) from exc

    settings.require_clickhouse()
    cfg = settings.CLICKHOUSE
    return clickhouse_connect.get_client(
        host=cfg["host"],
        port=cfg["port"],
        username=cfg["username"],
        password=cfg["password"],
        database=cfg["database"],
        secure=cfg["secure"],
        connect_timeout=15,
        query_limit=0,  # no implicit row cap
    )


def _run(sql: str) -> dict[str, Any]:
    client = _client()
    try:
        res = client.query(sql)
        return {
            "columns": list(res.column_names),
            "rows": [list(r) for r in res.result_rows],
        }
    finally:
        client.close()


def fetch_open(agency_id: str) -> dict[str, Any]:
    """All Open payout requests for one agency."""
    return _run(queries.open_requests_sql(agency_id))


def fetch_fail_credits(agency_id: str, cutoff: str) -> dict[str, Any]:
    """Fail credits since a cutoff timestamp (optional reconciliation)."""
    return _run(queries.fail_credits_since_sql(agency_id, cutoff))


def fetch_vai_leaks(agency_id: str) -> list[dict[str, Any]]:
    """Done requests with no VAI credit yet (leak candidates), as list[dict]."""
    client = _client()
    try:
        res = client.query(queries.vai_leaks_sql(agency_id))
        cols = list(res.column_names)
        return [dict(zip(cols, row)) for row in res.result_rows]
    finally:
        client.close()
