"""
daily_log.py — Daily snapshot logger for out-of-sample signal testing.

Records, for each trading day T:
    - Top-N stocks by foreign investor net purchase value
    - That day's close price
    - KOSPI index close (benchmark)
    - (optional) same-day news article count

IMPORTANT (lookahead-bias guard):
    This script records T-day information ONLY. It never computes returns.
    Foreign net-purchase data is finalized after the close, so any signal
    derived from it can only be acted on from T+1 onward. Return evaluation
    is done separately in evaluate.py, which reads this log and measures
    T+1 / T+5 / T+20 performance.

Output: data/predictions.csv (append-only, idempotent per (date, ticker))
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from pykrx import stock

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
TOP_N = 20                    # how many top net-bought stocks to record each day
MARKET = "KOSPI"              # "KOSPI" | "KOSDAQ" | "ALL"
KOSPI_INDEX_TICKER = "1001"   # KRX index code for KOSPI composite
DATA_DIR = Path(__file__).resolve().parent / "data"
OUT_PATH = DATA_DIR / "predictions.csv"

COLUMNS = [
    "date",             # T (trading day, YYYYMMDD)
    "ticker",
    "name",
    "rank",             # 1 = largest foreign net buy that day
    "foreign_net_buy",  # KRW, net purchase VALUE by foreign investors on T
    "close",            # close price on T
    "kospi_close",      # KOSPI index close on T (benchmark baseline)
    "news_count",       # same-day article count (nullable)
    "logged_at",        # when this row was written (audit trail)
]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def resolve_trading_day(today: str | None = None) -> str | None:
    """Return the trading day to log, or None if today is not a trading day.

    We only log on actual trading days. If run on a weekend/holiday, exit
    quietly rather than duplicating the previous session.
    """
    today = today or datetime.now().strftime("%Y%m%d")
    nearest = stock.get_nearest_business_day_in_a_week(date=today, prev=True)
    if nearest != today:
        print(f"[skip] {today} is not a trading day (nearest: {nearest})")
        return None
    return today


def fetch_foreign_net_buy(date: str) -> pd.DataFrame:
    """Top-N stocks by foreign investor net purchase VALUE on `date`."""
    df = stock.get_market_net_purchases_of_equities(
        date, date, MARKET, "외국인"
    )
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.reset_index()
    # pykrx returns the ticker either as the index name or a '티커' column
    ticker_col = "티커" if "티커" in df.columns else df.columns[0]
    df = df.rename(
        columns={
            ticker_col: "ticker",
            "종목명": "name",
            "순매수거래대금": "foreign_net_buy",
        }
    )
    df = df[["ticker", "name", "foreign_net_buy"]]
    df = df.sort_values("foreign_net_buy", ascending=False).head(TOP_N)
    df["rank"] = range(1, len(df) + 1)
    return df


def fetch_closes(date: str) -> pd.Series:
    """Close price per ticker on `date`."""
    ohlcv = stock.get_market_ohlcv(date, market=MARKET)
    return ohlcv["종가"]


def fetch_kospi_close(date: str) -> float | None:
    """KOSPI composite index close on `date` (benchmark)."""
    idx = stock.get_index_ohlcv(date, date, KOSPI_INDEX_TICKER)
    if idx is None or idx.empty:
        return None
    return float(idx["종가"].iloc[0])


def fetch_news_counts(names: list[str], date: str) -> dict[str, int | None]:
    """Optional: same-day article count per stock via Naver News API.

    Skipped silently if credentials are absent, so the core log never breaks.
    """
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not (client_id and client_secret):
        return {n: None for n in names}

    import urllib.parse
    import urllib.request
    import json

    counts: dict[str, int | None] = {}
    for name in names:
        try:
            query = urllib.parse.quote(name)
            url = (
                "https://openapi.naver.com/v1/search/news.json"
                f"?query={query}&display=100&sort=date"
            )
            req = urllib.request.Request(url)
            req.add_header("X-Naver-Client-Id", client_id)
            req.add_header("X-Naver-Client-Secret", client_secret)
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))

            target = datetime.strptime(date, "%Y%m%d").date()
            same_day = 0
            for item in payload.get("items", []):
                pub = item.get("pubDate")
                if not pub:
                    continue
                try:
                    pub_date = datetime.strptime(
                        pub, "%a, %d %b %Y %H:%M:%S %z"
                    ).date()
                except ValueError:
                    continue
                if pub_date == target:
                    same_day += 1
            counts[name] = same_day
        except Exception as exc:  # never let news break the core log
            print(f"[warn] news fetch failed for {name}: {exc}")
            counts[name] = None
    return counts


def append_idempotent(new_rows: pd.DataFrame, path: Path) -> int:
    """Append rows, replacing any existing rows with the same (date, ticker)."""
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        existing = pd.read_csv(path, dtype={"date": str, "ticker": str})
        combined = pd.concat([existing, new_rows], ignore_index=True)
        # keep the newest write for any duplicated (date, ticker)
        combined = combined.drop_duplicates(
            subset=["date", "ticker"], keep="last"
        )
    else:
        combined = new_rows

    combined = combined.sort_values(["date", "rank"]).reset_index(drop=True)

    # atomic write: temp file then replace, so a crash can't corrupt the log
    tmp = path.with_suffix(".csv.tmp")
    combined.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(path)

    return len(new_rows)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    # allow manual backfill: python daily_log.py 20260722
    override = sys.argv[1] if len(sys.argv) > 1 else None
    date = resolve_trading_day(override)
    if date is None:
        return 0

    print(f"[info] logging trading day {date}")

    flows = fetch_foreign_net_buy(date)
    if flows.empty:
        print(f"[warn] no foreign flow data for {date}; nothing logged")
        return 0

    closes = fetch_closes(date)
    flows["close"] = flows["ticker"].map(closes)

    flows["kospi_close"] = fetch_kospi_close(date)

    news = fetch_news_counts(flows["name"].tolist(), date)
    flows["news_count"] = flows["name"].map(news)

    flows["date"] = date
    flows["logged_at"] = datetime.now().isoformat(timespec="seconds")

    rows = flows[COLUMNS]
    written = append_idempotent(rows, OUT_PATH)

    print(f"[done] wrote {written} rows for {date} -> {OUT_PATH}")
    print(rows.head(5).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
