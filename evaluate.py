"""evaluate.py — 기록된 시그널이 실제로 돈이 됐는지 사후 측정한다.

daily_log.py의 독스트링이 계속 참조하던 파일인데 실제로는 존재한 적이 없다.
즉 "외국인 순매수 상위" 시그널을 3년 가까이 모으면서도, 그게 쓸모가 있는지
확인할 수단이 없었다. 이 스크립트가 그 자리를 채운다.

측정 규칙 (미래참조 차단이 핵심):
    외국인 순매수는 장 마감 후에 확정된다. 따라서 T일 시그널로 살 수 있는
    가장 이른 시점은 T+1 시가다. T일 종가로 진입한 것처럼 계산하면 실제로는
    알 수 없었던 정보로 매수한 셈이 되어 성과가 부풀려진다.

        진입: T+1 시가
        청산: T+h 종가   (h = 1, 5, 20 거래일)

    벤치마크(KOSPI)도 똑같은 구간으로 계산하고, 초과수익 = 종목수익 - 지수수익.
    "그냥 지수를 샀어도 벌었을 몫"을 빼야 시그널 자체의 기여가 남는다.

통계 처리:
    같은 날 상위 20종목은 그날의 시장 충격을 공유하므로 서로 독립이 아니다.
    760개 (종목,날짜) 행을 독립 표본처럼 t검정하면 유의성이 크게 과대평가된다.
    그래서 t통계량은 **날짜별 평균 초과수익**(하루 = 관측 1개)으로 계산한다.

사용법:
    python evaluate.py                    # 전체
    python evaluate.py --horizons 1 5 20
    python evaluate.py --refresh          # 가격 캐시 무시하고 다시 받기
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import snapshot
from snapshot import KST, OUT_PATH, UpstreamFetchError

CACHE_DIR = snapshot.DATA_DIR / ".price_cache"
REPORT_PATH = snapshot.DATA_DIR / "evaluation.json"

DEFAULT_HORIZONS = [1, 5, 20]
REQUEST_DELAY_SEC = 1.0
# 마지막 시그널 이후 h 거래일치 가격이 필요하다. 거래일 20일이면 달력으로는
# 주말/공휴일 때문에 그보다 훨씬 길다 — 넉넉히 잡는다.
FORWARD_PAD_DAYS = 60


# ---------------------------------------------------------------------------
# 가격 데이터 수집 (종목당 1회 호출 + 디스크 캐시)
# ---------------------------------------------------------------------------
def _cache_meta_path() -> Path:
    return CACHE_DIR / "_coverage.json"


def _load_coverage() -> dict[str, list[str]]:
    p = _cache_meta_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_coverage(cov: dict[str, list[str]]) -> None:
    _cache_meta_path().write_text(json.dumps(cov, ensure_ascii=False, indent=2), encoding="utf-8")


def _cached_if_covers(key: str, start: str, end: str, coverage: dict) -> pd.DataFrame | None:
    """캐시가 요청 구간을 포함하면 그걸 쓴다.

    예전에는 캐시 파일 이름에 start/end를 박아서, end가 "오늘"인 탓에 날짜만
    바뀌어도 캐시가 통째로 무효화됐다. 하루 뒤에 다시 돌리면 140종목을 처음부터
    다시 받는다. 구간을 따로 기록해두고 포함 관계로 판정한다.
    """
    have = coverage.get(key)
    cache = CACHE_DIR / f"{key}.csv"
    if not (have and cache.exists()):
        return None
    if have[0] > start or have[1] < end:
        return None
    df = pd.read_csv(cache, index_col=0, parse_dates=True)
    if df.empty:
        return None
    mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
    return df[mask]


def _clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    df = df[["시가", "종가"]].rename(columns={"시가": "open", "종가": "close"})
    # 거래정지일 등은 0으로 채워져 오는데, 그대로 두면 수익률이 -100%로 잡힌다.
    return df[(df["open"] > 0) & (df["close"] > 0)]


def fetch_ticker_ohlcv(
    stock_api, ticker: str, start: str, end: str, refresh: bool, coverage: dict
) -> pd.DataFrame | None:
    """종목 하나의 [start, end] OHLCV. 한 번 받으면 캐시해서 재실행은 호출 0회."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not refresh:
        cached = _cached_if_covers(ticker, start, end, coverage)
        if cached is not None:
            return cached if not cached.empty else None

    with snapshot.detect_upstream_errors():
        df = stock_api.get_market_ohlcv(start, end, ticker)
    if df is None or df.empty:
        return None

    df = _clean_ohlcv(df)
    df.to_csv(CACHE_DIR / f"{ticker}.csv")
    coverage[ticker] = [start, end]
    return df if not df.empty else None


def fetch_index_ohlcv(
    stock_api, start: str, end: str, refresh: bool, coverage: dict
) -> pd.DataFrame | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = "index_1001"

    if not refresh:
        cached = _cached_if_covers(key, start, end, coverage)
        if cached is not None:
            return cached if not cached.empty else None

    with snapshot.detect_upstream_errors():
        df = stock_api.get_index_ohlcv(start, end, snapshot.INDEX_CODE["KOSPI"])
    if df is None or df.empty:
        return None
    df = _clean_ohlcv(df)
    df.to_csv(CACHE_DIR / f"{key}.csv")
    coverage[key] = [start, end]
    return df


# ---------------------------------------------------------------------------
# 수익률 계산
# ---------------------------------------------------------------------------
def forward_return(prices: pd.DataFrame, signal_date: pd.Timestamp, horizon: int) -> float | None:
    """T+1 시가 진입 → T+h 종가 청산 수익률(%). 데이터가 모자라면 None.

    prices의 인덱스는 실제 거래일만 담고 있으므로, 위치 기반으로 h칸 뒤를
    집으면 그게 곧 h거래일 뒤다 (휴장일을 세지 않는다).
    """
    idx = prices.index
    after = idx[idx > signal_date]
    if len(after) < max(horizon, 1):
        return None  # 아직 미래가 오지 않았거나 상장폐지 등으로 데이터가 끊김

    entry = prices.loc[after[0], "open"]      # T+1 시가
    exit_ = prices.loc[after[horizon - 1], "close"]  # T+h 종가
    if entry <= 0:
        return None
    return (exit_ - entry) / entry * 100.0


def summarize(df: pd.DataFrame, horizon: int) -> dict:
    """한 홀딩기간에 대한 요약. t통계량은 날짜별 평균으로 계산한다."""
    col = f"excess_{horizon}"
    sub = df.dropna(subset=[col])
    if sub.empty:
        return {"horizon": horizon, "n_positions": 0, "note": "측정 가능한 표본 없음"}

    daily = sub.groupby("date")[col].mean()
    n_days = len(daily)
    mean_daily = float(daily.mean())
    # 표본이 1일이면 표준편차가 정의되지 않는다 — t통계량을 만들지 않는다.
    if n_days > 1 and daily.std(ddof=1) > 0:
        t_stat = mean_daily / (daily.std(ddof=1) / math.sqrt(n_days))
    else:
        t_stat = None

    return {
        "horizon": horizon,
        "n_positions": int(len(sub)),
        "n_signal_days": int(n_days),
        "mean_raw_return_pct": round(float(sub[f"ret_{horizon}"].mean()), 3),
        "mean_excess_pct": round(float(sub[col].mean()), 3),
        "median_excess_pct": round(float(sub[col].median()), 3),
        "win_rate_pct": round(float((sub[col] > 0).mean() * 100), 1),
        "mean_daily_excess_pct": round(mean_daily, 3),
        "t_stat_on_daily_means": None if t_stat is None else round(float(t_stat), 2),
    }


def summarize_by_rank(df: pd.DataFrame, horizon: int) -> list[dict]:
    col = f"excess_{horizon}"
    buckets = [("1-5", 1, 5), ("6-10", 6, 10), ("11-20", 11, 20)]
    out = []
    for label, lo, hi in buckets:
        sub = df[(df["rank"] >= lo) & (df["rank"] <= hi)].dropna(subset=[col])
        if sub.empty:
            continue
        out.append({
            "rank_bucket": label,
            "n": int(len(sub)),
            "mean_excess_pct": round(float(sub[col].mean()), 3),
            "win_rate_pct": round(float((sub[col] > 0).mean() * 100), 1),
        })
    return out


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="기록된 시그널의 사후 수익률을 측정한다")
    ap.add_argument("--horizons", type=int, nargs="+", default=DEFAULT_HORIZONS,
                    help="홀딩 거래일 수 (기본: 1 5 20)")
    ap.add_argument("--refresh", action="store_true", help="가격 캐시를 무시하고 다시 받는다")
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC)
    args = ap.parse_args()

    if not OUT_PATH.exists():
        print(f"[fatal] {OUT_PATH} 가 없습니다. 먼저 daily_log.py / backfill.py 로 데이터를 모으세요.")
        return 2

    snapshot.load_env()
    try:
        snapshot.require_krx_credentials()
    except UpstreamFetchError as exc:
        print(f"[fatal] {exc}")
        return 2

    signals = pd.read_csv(OUT_PATH, dtype={"date": str, "ticker": str})
    signals["date"] = pd.to_datetime(signals["date"].map(snapshot.to_compact), format="%Y%m%d")
    signals = signals.dropna(subset=["date", "ticker"])
    if signals.empty:
        print("[fatal] 기록된 시그널이 없습니다.")
        return 2

    start = signals["date"].min().strftime("%Y%m%d")
    end = (signals["date"].max() + timedelta(days=FORWARD_PAD_DAYS)).strftime("%Y%m%d")
    today = datetime.now(KST).strftime("%Y%m%d")
    end = min(end, today)

    tickers = sorted(signals["ticker"].unique())
    max_h = max(args.horizons)
    print(f"[info] 시그널 {len(signals)}행 · 거래일 {signals['date'].nunique()}일 · 종목 {len(tickers)}개")
    print(f"[info] 가격 조회 구간 {start} ~ {end} (홀딩 최대 {max_h}거래일)")

    stock_api = snapshot.import_pykrx_stock()
    coverage = _load_coverage()

    print("[info] KOSPI 지수 조회...")
    index_px = fetch_index_ohlcv(stock_api, start, end, args.refresh, coverage)
    if index_px is None:
        print("[fatal] KOSPI 지수 데이터를 받지 못했습니다.")
        return 2

    # 종목마다 필요한 구간은 다르다. 마지막 시그널이 2023년인 종목은 그 뒤로
    # 몇 거래일치만 있으면 되고, 오늘 데이터를 받을 이유가 없다. 전 종목을
    # 공통 구간(=오늘까지)으로 받으면 날짜가 하루 지날 때마다 캐시가 전부
    # 무효화돼서 매번 140종목을 다시 받게 된다.
    first_signal = signals.groupby("ticker")["date"].min()
    last_signal = signals.groupby("ticker")["date"].max()

    prices: dict[str, pd.DataFrame] = {}
    misses = 0
    fetched = 0
    for i, t in enumerate(tickers, 1):
        t_start = first_signal[t].strftime("%Y%m%d")
        t_end = min((last_signal[t] + timedelta(days=FORWARD_PAD_DAYS)).strftime("%Y%m%d"), today)
        was_cached = _cached_if_covers(t, t_start, t_end, coverage) is not None and not args.refresh
        try:
            df = fetch_ticker_ohlcv(stock_api, t, t_start, t_end, args.refresh, coverage)
        except UpstreamFetchError as exc:
            print(f"  [warn] {t} 조회 실패 — 건너뜁니다: {str(exc)[:120]}")
            df = None
        if df is None:
            misses += 1
        else:
            prices[t] = df
        if not was_cached:
            fetched += 1
            time.sleep(args.delay)
        if i % 25 == 0 or i == len(tickers):
            print(f"  [progress] {i}/{len(tickers)} (신규조회 {fetched} · 미확보 {misses})", flush=True)

    _save_coverage(coverage)

    if not prices:
        print("[fatal] 가격 데이터를 하나도 받지 못했습니다.")
        return 2

    # 수익률 계산
    for h in args.horizons:
        signals[f"ret_{h}"] = pd.NA
        signals[f"bench_{h}"] = pd.NA

    for i, row in signals.iterrows():
        px = prices.get(row["ticker"])
        for h in args.horizons:
            if px is not None:
                signals.at[i, f"ret_{h}"] = forward_return(px, row["date"], h)
            signals.at[i, f"bench_{h}"] = forward_return(index_px, row["date"], h)

    for h in args.horizons:
        signals[f"ret_{h}"] = pd.to_numeric(signals[f"ret_{h}"], errors="coerce")
        signals[f"bench_{h}"] = pd.to_numeric(signals[f"bench_{h}"], errors="coerce")
        signals[f"excess_{h}"] = signals[f"ret_{h}"] - signals[f"bench_{h}"]

    # ---- 리포트 ----
    report = {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "entry_rule": "T+1 시가 진입, T+h 종가 청산 (외국인 순매수는 T일 장 마감 후 확정되므로 T+1이 최초 실행 가능 시점)",
        "benchmark": "KOSPI 지수, 동일 구간",
        "signal_rows": int(len(signals)),
        "signal_days": int(signals["date"].nunique()),
        "tickers": len(tickers),
        "tickers_without_price": misses,
        "date_range": [signals["date"].min().strftime("%Y-%m-%d"), signals["date"].max().strftime("%Y-%m-%d")],
        "horizons": {},
    }

    print("\n" + "=" * 72)
    print("시그널 성과 — 외국인 순매수 상위 (KOSPI 대비 초과수익)")
    print("=" * 72)
    print(f"{'홀딩':>6} {'표본':>6} {'시그널일':>8} {'평균수익':>9} {'평균초과':>9} {'중앙초과':>9} {'승률':>7} {'t값':>7}")
    print("-" * 72)

    for h in args.horizons:
        s = summarize(signals, h)
        report["horizons"][str(h)] = {"overall": s, "by_rank": summarize_by_rank(signals, h)}
        if s["n_positions"] == 0:
            print(f"{h:>5}일 {'-':>6} {'측정 가능한 표본 없음':>30}")
            continue
        t_disp = "n/a" if s["t_stat_on_daily_means"] is None else f"{s['t_stat_on_daily_means']:+.2f}"
        print(
            f"{h:>5}일 {s['n_positions']:>6} {s['n_signal_days']:>8} "
            f"{s['mean_raw_return_pct']:>+8.2f}% {s['mean_excess_pct']:>+8.2f}% "
            f"{s['median_excess_pct']:>+8.2f}% {s['win_rate_pct']:>6.1f}% {t_disp:>7}"
        )

    print("-" * 72)
    for h in args.horizons:
        by_rank = report["horizons"][str(h)]["by_rank"]
        if by_rank:
            parts = " | ".join(
                f"{b['rank_bucket']}위: {b['mean_excess_pct']:+.2f}% (승률 {b['win_rate_pct']:.0f}%, n={b['n']})"
                for b in by_rank
            )
            print(f"  {h:>2}일 순위별  {parts}")

    # 표본이 얼마나 얇은지 숨기지 않는다 — 이 데이터로 결론을 내리면 안 되는
    # 구간이 분명히 있다.
    n_days = signals["date"].nunique()
    print("\n[해석 주의]")
    print(f"  · 시그널일이 {n_days}일뿐이라 t값은 참고치다. 통상 |t|>2 를 유의하다고 보지만,")
    print(f"    그 기준도 표본이 수십 일 이상 쌓였을 때 이야기다.")
    print(f"  · t값은 (종목,날짜) {len(signals)}행이 아니라 '날짜별 평균' {n_days}개로 계산했다.")
    print(f"    같은 날 20종목은 시장 충격을 공유해 독립 표본이 아니기 때문이다.")
    print(f"  · 수수료·세금·슬리피지·시가 체결 가정은 반영하지 않았다. 실제 성과는 이보다 낮다.")
    if misses:
        print(f"  · 종목 {misses}개는 가격을 확보하지 못해 제외됐다(상장폐지·티커 변경 가능성).")

    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] 리포트 저장: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
