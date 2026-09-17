#!/usr/bin/env python3
"""Build the GitHub Pages stock snapshot from a read-only PostgreSQL query.

This script intentionally has no dependency on the VCPScanner runtime. It is
executed by GitHub Actions and writes one static ``current.json`` file into
the Pages artifact. It never writes to PostgreSQL and never talks to R2.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
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
    sm.ticker, c.company_name, sm.price, sm.market_cap, sm.return_1d,
    sm.rs_rating, sm.price_to_sma50, sm.price_to_sma200,
    sm.price_to_52w_high, sm.volume_ratio, sm.last_updated
FROM screening_metrics AS sm
JOIN companies AS c ON c.ticker = sm.ticker
WHERE c.country = 'US'
  AND sm.instrument_type = 'common'
  AND sm.market_cap >= 500000000
  AND sm.price IS NOT NULL
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


def ticker_color(ticker: str) -> str:
    hash_val = 0
    for char in ticker:
        hash_val = ord(char) + ((hash_val << 5) - hash_val)
        hash_val &= 0xFFFFFFFF
    hue = abs(hash_val) % 360
    return f"hsl({hue}, 55%, 42%)"


def render_table_rows(stocks: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for stock in stocks:
        ticker = html.escape(str(stock.get("ticker") or "").upper())
        name = html.escape(str(stock.get("name") or ticker))
        price = stock.get("price")
        price_str = f"${price:,.2f}" if price is not None else "—"

        change_pct = stock.get("change_pct")
        if change_pct is not None:
            change_class = "pos" if change_pct >= 0 else "neg"
            change_str = f"{'+' if change_pct >= 0 else ''}{change_pct:.1f}%"
        else:
            change_class = ""
            change_str = "—"

        rs = stock.get("rs_rating")
        if rs is not None:
            rs_val = round(rs)
            rs_class = "rs-super" if rs_val >= 95 else "rs-elite" if rs_val >= 90 else ""
            rs_str = str(rs_val)
        else:
            rs_class = ""
            rs_str = "—"

        sma50 = stock.get("price_to_sma50")
        if sma50 is not None:
            val = (sma50 - 1) * 100
            sma50_str = f"{'+' if val >= 0 else ''}{val:.1f}%"
        else:
            sma50_str = "—"

        sma200 = stock.get("price_to_sma200")
        if sma200 is not None:
            val = (sma200 - 1) * 100
            sma200_str = f"{'+' if val >= 0 else ''}{val:.1f}%"
        else:
            sma200_str = "—"

        gap = stock.get("below_52w_high_pct")
        if gap is not None:
            range_pct = max(10, min(100, round(100 - (gap / 25) * 60)))
            gap_str = f"-{gap:.1f}%"
        else:
            range_pct = 50
            gap_str = "—"

        vol = stock.get("volume_ratio") or 1.0
        vol_width = min(100, round((vol / 2.5) * 100))
        vol_class = "high" if vol >= 1.5 else ""
        vol_str = f"{vol:.2f}×"

        logo_url = f"https://assets.vcpscanner.com/logos/{ticker}.webp"
        fallback_bg = ticker_color(ticker)
        initial = ticker[0] if ticker else ""
        ticker_lower = ticker.lower()

        row_html = f"""      <tr data-ticker="{ticker}">
        <td class="td-star" onclick="event.stopPropagation(); window.toggleStar('{ticker}')">
          <button class="star-btn" type="button" title="Star Stock">★</button>
        </td>
        <td>
          <div class="stock-cell">
            <img class="stock-logo" src="{logo_url}" alt="{ticker}" loading="lazy"
                 onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
            <span class="stock-logo-fallback" style="display:none; background-color: {fallback_bg};">{initial}</span>
            <div class="stock-info">
              <span class="stock-ticker">{ticker}</span>
              <span class="stock-name" title="{name}">{name}</span>
            </div>
          </div>
        </td>
        <td class="td-num">{price_str}</td>
        <td class="td-num">
          <span class="change-pill {change_class}">{change_str}</span>
        </td>
        <td class="td-num">
          <strong class="rs-val {rs_class}">{rs_str}</strong>
        </td>
        <td class="td-num text-teal">{sma50_str}</td>
        <td class="td-num text-teal">{sma200_str}</td>
        <td class="range-cell">
          <div class="range-wrap">
            <div class="range-track">
              <div class="range-fill" style="width: {range_pct}%;"></div>
            </div>
            <span class="range-label">{gap_str}</span>
          </div>
        </td>
        <td class="vol-cell td-num">
          <div class="vol-wrap">
            <span class="font-mono">{vol_str}</span>
            <div class="vol-bar">
              <div class="vol-fill {vol_class}" style="width: {vol_width}%;"></div>
            </div>
          </div>
        </td>
        <td class="th-actions" onclick="event.stopPropagation();">
          <a class="row-chart-link" href="https://vcpscanner.com/technical/{ticker_lower}?utm_source=github&utm_medium=table_row&utm_campaign=minervini-trend-template-screener" target="_blank" rel="noopener noreferrer" title="Open chart on VCPScanner">
            Chart ↗
          </a>
        </td>
      </tr>"""
        rows.append(row_html)
    return "\n".join(rows)


def calculate_kpis(stocks: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(stocks)
    pct_universe = round((count / 5800) * 100, 1)
    bar_pct = max(1, min(100, round((count / 5800) * 100)))

    rs_vals = sorted([s["rs_rating"] for s in stocks if s.get("rs_rating") is not None])
    if rs_vals:
        mid = len(rs_vals) // 2
        median_rs = round(rs_vals[mid] if len(rs_vals) % 2 else (rs_vals[mid - 1] + rs_vals[mid]) / 2)
    else:
        median_rs = "—"

    high_vals = sorted([s["below_52w_high_pct"] for s in stocks if s.get("below_52w_high_pct") is not None])
    if high_vals:
        mid = len(high_vals) // 2
        med_high = high_vals[mid] if len(high_vals) % 2 else (high_vals[mid - 1] + high_vals[mid]) / 2
        median_high_str = f"-{med_high:.1f}%"
    else:
        median_high_str = "—"

    near_high_count = sum(
        1 for s in stocks if s.get("below_52w_high_pct") is not None and s["below_52w_high_pct"] <= 5.0
    )

    return {
        "count": count,
        "pct_universe": pct_universe,
        "bar_pct": bar_pct,
        "median_rs": median_rs,
        "median_high_str": median_high_str,
        "near_high_count": near_high_count,
    }


def format_session_date(session_date_str: str) -> str:
    try:
        dt = date.fromisoformat(session_date_str[:10])
        return dt.strftime("%b %d, %Y")
    except Exception:
        return session_date_str


def hydrate_html(html_text: str, payload: dict[str, Any]) -> str:
    stocks = payload.get("stocks") or []
    kpis = calculate_kpis(stocks)
    session_str = payload.get("session_date") or payload.get("data_as_of") or payload.get("generated_at") or ""
    date_formatted = format_session_date(session_str)

    # 1. Render Table Rows
    rendered_rows = render_table_rows(stocks)
    row_pattern = r"(<!--\s*SSR_TABLE_ROWS_START\s*-->).*?(<!--\s*SSR_TABLE_ROWS_END\s*-->)"
    html_text = re.sub(
        row_pattern,
        lambda m: f"{m.group(1)}\n{rendered_rows}\n              {m.group(2)}",
        html_text,
        flags=re.DOTALL,
    )

    # 2. Update Freshness and Universe Note
    as_of_text = f"Data as of market close · {date_formatted}"
    universe_text = f"{kpis['count']} Qualified US Common Equities ($500M+ Cap)"
    html_text = re.sub(
        r'(<strong id="as-of"[^>]*>)(.*?)(</strong>)',
        lambda m: f"{m.group(1)}{as_of_text}{m.group(3)}",
        html_text,
    )
    html_text = re.sub(
        r'(<span id="universe-note"[^>]*>)(.*?)(</span>)',
        lambda m: f"{m.group(1)}{universe_text}{m.group(3)}",
        html_text,
    )

    # 3. Update KPIs
    html_text = re.sub(
        r'(<strong id="stat-count"[^>]*>)(.*?)(</strong>)',
        lambda m: f"{m.group(1)}{kpis['count']:,}{m.group(3)}",
        html_text,
    )
    html_text = re.sub(
        r'(<span id="stat-universe-pct"[^>]*>)(.*?)(</span>)',
        lambda m: f"{m.group(1)}{kpis['pct_universe']}% of US market{m.group(3)}",
        html_text,
    )
    html_text = re.sub(
        r'(<div id="kpi-qual-bar"[^>]*style=")[^"]*(")',
        lambda m: f"{m.group(1)}width: {kpis['bar_pct']}%;{m.group(2)}",
        html_text,
    )
    html_text = re.sub(
        r'(<strong id="stat-rs"[^>]*>)(.*?)(</strong>)',
        lambda m: f"{m.group(1)}{kpis['median_rs']}{m.group(3)}",
        html_text,
    )
    html_text = re.sub(
        r'(<strong id="stat-high"[^>]*>)(.*?)(</strong>)',
        lambda m: f"{m.group(1)}{kpis['median_high_str']}{m.group(3)}",
        html_text,
    )
    html_text = re.sub(
        r'(<strong id="stat-high5"[^>]*>)(.*?)(</strong>)',
        lambda m: f"{m.group(1)}{kpis['near_high_count']} Setups{m.group(3)}",
        html_text,
    )

    # 4. Update Table Meta Count
    candidates_label = f"{kpis['count']} candidate{'s' if kpis['count'] != 1 else ''}"
    html_text = re.sub(
        r'(<span id="result-count"[^>]*>)(.*?)(</span>)',
        lambda m: f"{m.group(1)}{candidates_label}{m.group(3)}",
        html_text,
    )

    # 5. Reveal Table Container and Hide Loading State
    html_text = html_text.replace('id="table-state" class="table-container hidden"', 'id="table-state" class="table-container"')
    html_text = html_text.replace('id="loading-state" class="state-container"', 'id="loading-state" class="state-container hidden"')

    # 6. Inject Embedded Initial Snapshot Script
    compact_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    script_tag = f'<!-- SSR_INITIAL_SNAPSHOT -->\n  <script id="initial-snapshot" type="application/json">{compact_json}</script>'

    if '<script id="initial-snapshot"' in html_text:
        html_text = re.sub(
            r'<script id="initial-snapshot"[^>]*>.*?</script>',
            f'<script id="initial-snapshot" type="application/json">{compact_json}</script>',
            html_text,
            flags=re.DOTALL,
        )
    else:
        html_text = html_text.replace('<!-- SSR_INITIAL_SNAPSHOT -->', script_tag)

    return html_text


def update_sitemap(sitemap_path: Path, session_date_str: str) -> None:
    if not sitemap_path.is_file():
        return
    text = sitemap_path.read_text(encoding="utf-8")
    formatted_date = session_date_str[:10]
    updated = re.sub(r'<lastmod>.*?</lastmod>', f'<lastmod>{formatted_date}</lastmod>', text)
    sitemap_path.write_text(updated, encoding="utf-8")
    print(f"SITEMAP_UPDATED path={sitemap_path} lastmod={formatted_date}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("current.json"))
    parser.add_argument(
        "--existing-url",
        default=os.environ.get("PUBLIC_SNAPSHOT_URL"),
        help="Optional live Pages URL used to make unchanged sessions a no-op",
    )
    parser.add_argument("--force", action="store_true", help="Build even when the live session is current")
    parser.add_argument("--html-input", type=Path, default=None, help="Optional index.html to pre-render with snapshot data")
    parser.add_argument("--html-output", type=Path, default=None, help="Optional output path for pre-rendered index.html")
    parser.add_argument("--sitemap", type=Path, default=None, help="Optional sitemap.xml to update with latest session date")
    args = parser.parse_args()

    html_in = args.html_input or (Path("index.html") if Path("index.html").is_file() else None)
    html_out = args.html_output or html_in
    sitemap_target = args.sitemap or (Path("sitemap.xml") if Path("sitemap.xml").is_file() else None)

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
        if html_in and html_out and html_in.is_file():
            html_content = html_in.read_text(encoding="utf-8")
            hydrated = hydrate_html(html_content, current_payload)
            html_out.write_text(hydrated, encoding="utf-8")
            print(f"HTML_HYDRATED (noop session) output={html_out}")
        if sitemap_target:
            update_sitemap(sitemap_target, session_date.isoformat())
        print(f"SNAPSHOT_NOOP session={session_date} existing={current}; preserved existing payload")
        return 0

    print(f"Read {len(universe)} eligible stocks for session {session_date}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = None
    for slug, definition in SCREENS.items():
        payload = build_payload(slug, definition, universe, session_date, metrics_updated_at)
        validate_payload(payload, slug)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"SNAPSHOT_BUILT slug={slug} session={session_date} count={payload['count']} output={args.output}")

    if payload and html_in and html_out and html_in.is_file():
        html_content = html_in.read_text(encoding="utf-8")
        hydrated = hydrate_html(html_content, payload)
        html_out.write_text(hydrated, encoding="utf-8")
        print(f"HTML_HYDRATED output={html_out}")

    if sitemap_target:
        update_sitemap(sitemap_target, session_date.isoformat())

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"SNAPSHOT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
