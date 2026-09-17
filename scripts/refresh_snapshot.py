#!/usr/bin/env python3
"""Build the GitHub Pages stock snapshot from a read-only PostgreSQL query.

This script intentionally has no dependency on the VCPScanner runtime. It is
executed by GitHub Actions and writes one static ``current.json`` file into
the Pages artifact. It never writes to PostgreSQL and never talks to R2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

import psycopg2
from psycopg2.extras import RealDictCursor


BASE_QUERY = """
SELECT
    ticker, company_name, price, market_cap, return_1d, rs_rating,
    price_to_sma50, price_to_sma200, price_to_52w_high,
    volume_ratio, last_updated
FROM screening_metrics
WHERE country = 'US'
  AND instrument_type = 'common'
  AND market_cap >= 500000000
  AND price IS NOT NULL
"""


def as_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def qualifies_minervini_candidate(row: dict[str, Any]) -> bool:
    sma50 = as_number(row.get("price_to_sma50"))
    sma200 = as_number(row.get("price_to_sma200"))
    high = as_number(row.get("price_to_52w_high"))
    rs = as_number(row.get("rs_rating"))
    return (
        all(value is not None for value in (sma50, sma200, high, rs))
        and sma50 >= 1
        and sma200 >= 1
        and high >= 0.75
        and rs >= 70
    )


SCREENS: dict[str, dict[str, Any]] = {
    "minervini-trend-template": {
        "title": "Minervini Trend Template Candidates",
        "predicate": qualifies_minervini_candidate,
        "sort": lambda row: as_number(row.get("rs_rating")) or 0,
        "limit": 300,
    }
}


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    daily_return = as_number(row.get("return_1d"))
    high_ratio = as_number(row.get("price_to_52w_high"))
    updated = row.get("last_updated")
    return {
        "ticker": str(row.get("ticker") or ""),
        "name": str(row.get("company_name") or row.get("ticker") or ""),
        "price": as_number(row.get("price")),
        "change_pct": daily_return * 100 if daily_return is not None else None,
        "market_cap": as_number(row.get("market_cap")),
        "rs_rating": as_number(row.get("rs_rating")),
        "price_to_sma50": as_number(row.get("price_to_sma50")),
        "price_to_sma200": as_number(row.get("price_to_sma200")),
        "price_to_52w_high": high_ratio,
        "below_52w_high_pct": (1 - high_ratio) * 100 if high_ratio is not None else None,
        "volume_ratio": as_number(row.get("volume_ratio")),
        "last_updated": updated.isoformat() if hasattr(updated, "isoformat") else updated,
    }


def read_database(database_url: str) -> tuple[date, datetime | None, list[dict[str, Any]]]:
    """Read the session marker and universe in one read-only transaction."""
    connection = psycopg2.connect(
        database_url,
        connect_timeout=20,
        application_name="github-pages-public-snapshot",
    )
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SELECT MAX(date)::date AS session_date FROM price_daily")
            marker = cursor.fetchone()
            if not marker or marker["session_date"] is None:
                raise RuntimeError("price_daily contains no completed market session")

            cursor.execute("SELECT MAX(last_updated) AS metrics_updated_at FROM screening_metrics")
            freshness = cursor.fetchone()["metrics_updated_at"]
            cursor.execute(BASE_QUERY)
            universe = [dict(row) for row in cursor.fetchall()]
        connection.commit()
        return marker["session_date"], freshness, universe
    finally:
        connection.close()


def build_payload(
    slug: str,
    definition: dict[str, Any],
    universe: list[dict[str, Any]],
    session_date: date,
    metrics_updated_at: datetime | None,
) -> dict[str, Any]:
    predicate: Callable[[dict[str, Any]], bool] = definition["predicate"]
    rows = sorted(
        (row for row in universe if predicate(row)),
        key=definition["sort"],
        reverse=True,
    )[: definition["limit"]]
    if not rows:
        raise RuntimeError(f"Refusing to publish an empty snapshot for {slug}")

    normalized = [normalize(row) for row in rows]
    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 2,
        "slug": slug,
        "title": definition["title"],
        "generated_at": generated_at,
        "session_date": session_date.isoformat(),
        "data_as_of": metrics_updated_at.isoformat() if metrics_updated_at else None,
        "universe": "US common stocks with market cap of at least $500 million",
        "filters": {
            "minimum_market_cap": 500000000,
            "minimum_price_to_sma50": 1,
            "minimum_price_to_sma200": 1,
            "minimum_rs_rating": 70,
            "minimum_price_to_52w_high": 0.75,
        },
        "count": len(normalized),
        "stocks": normalized,
    }


def validate_payload(payload: dict[str, Any], slug: str) -> None:
    if payload.get("schema_version") != 2 or payload.get("slug") != slug:
        raise RuntimeError(f"Invalid payload identity for {slug}")
    session = payload.get("session_date")
    try:
        date.fromisoformat(str(session))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid session_date: {session!r}") from exc
    stocks = payload.get("stocks")
    if not isinstance(stocks, list) or not stocks:
        raise RuntimeError("Snapshot must contain a non-empty stocks array")
    if payload.get("count") != len(stocks):
        raise RuntimeError("Snapshot count does not match stocks array")
    if any(not isinstance(stock, dict) for stock in stocks):
        raise RuntimeError("Snapshot contains a non-object stock row")
    tickers = [stock.get("ticker") for stock in stocks if isinstance(stock, dict)]
    if len(tickers) != len(set(tickers)) or any(not ticker for ticker in tickers):
        raise RuntimeError("Snapshot contains duplicate or empty tickers")


def fetch_existing(url: str | None) -> dict[str, Any] | None:
    """Return the live Pages payload, if configured and reachable."""
    if not url:
        return None
    try:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "public-snapshot-refresh/1.0"})
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("existing snapshot is not a JSON object")
        value = payload.get("session_date")
        if value:
            date.fromisoformat(value)
        return payload
    except (OSError, URLError, ValueError, TypeError, json.JSONDecodeError):
        print("Existing Pages snapshot unavailable; continuing with a fresh build.", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("current.json"))
    parser.add_argument(
        "--existing-url",
        default=os.environ.get("PUBLIC_SNAPSHOT_URL"),
        help="Optional live Pages URL used to make unchanged sessions a no-op",
    )
    parser.add_argument("--force", action="store_true", help="Build even when the live session is current")
    args = parser.parse_args()

    database_url = os.environ.get("PUBLIC_SNAPSHOT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("PUBLIC_SNAPSHOT_DATABASE_URL is required")

    session_date, metrics_updated_at, universe = read_database(database_url)
    if metrics_updated_at is None or metrics_updated_at.date() < session_date:
        raise RuntimeError(
            "screening_metrics has not been refreshed through the latest price session "
            f"(session={session_date}, metrics_updated={metrics_updated_at})"
        )
    current_payload = fetch_existing(args.existing_url)
    current_value = current_payload.get("session_date") if current_payload else None
    current = date.fromisoformat(current_value) if current_value else None
    if current is not None and current >= session_date and not args.force:
        validate_payload(current_payload, "minervini-trend-template")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(current_payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        print(f"SNAPSHOT_NOOP session={session_date} existing={current}; preserved existing payload")
        return 0

    print(f"Read {len(universe)} eligible stocks for session {session_date}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for slug, definition in SCREENS.items():
        payload = build_payload(slug, definition, universe, session_date, metrics_updated_at)
        validate_payload(payload, slug)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"SNAPSHOT_BUILT slug={slug} session={session_date} count={payload['count']} output={args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"SNAPSHOT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
