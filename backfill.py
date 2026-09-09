"""backfill.py — data/predictions.csv를 과거 날짜로 채운다.

daily_log.py가 하루치를 기록한다면, 이 스크립트는 기간을 훑는다. 수집·저장
로직은 snapshot.py를 공유하므로 evaluate.py 입장에서는 어느 쪽이 기록했는지
구분할 필요가 없다 — 스키마도 date 포맷도 동일하다.

수집 경로(--source):
    krx     : KRX 직접 호출 (기본). 프로세스 하나가 로그인 한 번으로 끝까지
              돈다.
    backend : StockResearchMac 백엔드 경유. 예전 기본값이었는데, 상시 구동
              프로세스는 pykrx 세션 만료 시 스레드들이 동시에 재로그인을
              시도하다 KRX 중복 로그인 거부에 걸려 세션이 깨진다(backend.log에서
              재로그인 4건 중 3건 실패 확인). 남겨는 두되 기본값에서 뺐다.

사용법:
    python backfill.py --start 20230101 --end 20260901
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import snapshot
from snapshot import KST, OUT_PATH, UpstreamFetchError

META_PATH = snapshot.DATA_DIR / "predictions.meta.json"
BACKEND_REPO = Path("/Users/jaewooan/AIstocktrading/stock-research-app")

# 하루당 KRX를 3번 호출한다(수급/시세/지수). 요청 사이를 너무 촘촘히 두면 KRX
# 요청 제한에 걸린다 — 실제로 겪었다: 지연 없이 돌렸더니 900여 거래일 중 36일만
# 성공하고 이후 전부 "휴장일"로 오판됐다(사실은 응답이 막힌 것). 그 사고 이후
# 값을 보수적으로 잡았다.
REQUEST_DELAY_SEC = 1.5
MAX_RETRIES = 4
RETRY_BACKOFF_SEC = [30, 90, 180, 300]  # 요청 제한 시 점점 길게 쉰다

# 몇 거래일마다 디스크에 흘려보낼지. 3년치 백필은 한두 시간이 걸려서, 끝까지
# 메모리에 들고 있으면 중간에 죽는 순간 전부 잃는다.
CHECKPOINT_EVERY = 25


class HardStop(Exception):
    """재시도를 다 써도 회복 안 됨 — 나머지 날짜를 '휴장일'로 오기록하느니 멈춘다."""


def backend_commit() -> str | None:
    """백엔드 경유로 받았을 때 어느 커밋 상태였는지 (계보 추적용)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(BACKEND_REPO), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def fetch_day_with_retry(source: str, d: str, market: str, top_n: int) -> pd.DataFrame | None:
    """요청 제한이면 점점 길게 쉬며 재시도, 진짜 휴장일이면 None을 돌려준다.

    재시도 자체는 snapshot이 담당한다(일시적 네트워크 끊김도 같이 처리된다).
    다만 백필은 하루치 크론과 달리 수백 일을 연속으로 두드리므로 KRX 요청
    제한에 걸리기 쉬워, 대기 시간을 훨씬 길게 잡는다.

    재시도를 다 소진하면 예외로 전체 실행을 멈춘다 — 여기서 조용히 None을
    반환해버리면 스키마상 "휴장일"과 구분이 안 돼서, 실은 데이터가 빠진 날인데
    완결된 것처럼 CSV에 남는다.
    """
    try:
        return snapshot.fetch_day_with_retry(
            source, d, market, top_n, backoffs=tuple(RETRY_BACKOFF_SEC[:MAX_RETRIES])
        )
    except UpstreamFetchError as exc:
        raise HardStop(f"{d}: {MAX_RETRIES}번 재시도해도 회복되지 않음 — {exc}")


def recorded_dates(path: Path) -> set:
    """CSV에 이미 기록된 날짜 집합. ISO/YYYYMMDD가 섞여 있어도 죽지 않는다."""
    if not path.exists():
        return set()
    dates = pd.read_csv(path, usecols=["date"], dtype={"date": str})["date"].dropna()
    if dates.empty:
        return set()
    parsed = pd.to_datetime(dates.map(snapshot.to_compact), format="%Y%m%d", errors="coerce").dropna()
    return set(parsed.dt.date)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20230101")
    ap.add_argument("--end", default=datetime.now(KST).strftime("%Y%m%d"))
    ap.add_argument("--market", default="KOSPI")
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--source", choices=["krx", "backend"], default="krx")
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC, help="요청 사이 대기(초)")
    ap.add_argument("--no-resume", action="store_true", help="이미 기록된 날짜도 처음부터 다시 받는다")
    args = ap.parse_args()

    snapshot.load_env()

    if args.source == "krx":
        try:
            snapshot.require_krx_credentials()
        except UpstreamFetchError as exc:
            print(f"[fatal] {exc}")
            return 2
    else:
        import requests
        try:
            requests.get(f"{snapshot.DEFAULT_BACKEND}/health", timeout=3).raise_for_status()
        except Exception:
            print(f"[fatal] {snapshot.DEFAULT_BACKEND} 에 연결할 수 없습니다. 백엔드가 떠 있는지 확인하세요.")
            print("        (launchctl list | grep stockresearch 로 확인)")
            return 2

    start = datetime.strptime(args.start, "%Y%m%d").date()
    end = datetime.strptime(args.end, "%Y%m%d").date()

    # 이미 확보한 날짜는 건너뛴다 — idempotent append라 다시 받아도 안전은
    # 하지만, 굳이 KRX를 또 두드릴 이유가 없다.
    #
    # 예전에는 "기록된 최대 날짜 다음날부터"로 이어받았는데, 그러면 데이터가
    # 앞에서부터 빈틈없이 채워진다는 가정이 깔린다. 실제로는 2023-02에서 한 번
    # 끊긴 뒤 launchd가 2026-08부터 오늘치를 기록하기 시작해서, 가운데가 3년 반
    # 비어 있는데도 최대 날짜는 "오늘"이었다. 그 결과 백필이 "받을 구간이 없다"며
    # 아무것도 하지 않고 끝났다 — 구멍을 영영 못 채우는 상태였다.
    # 날짜 집합으로 판정하면 구간이 어떻게 흩어져 있든 빠진 날만 받는다.
    already = set() if args.no_resume else recorded_dates(OUT_PATH)
    if already:
        in_range = sum(1 for d in already if start <= d <= end)
        print(f"[info] 이미 확보한 날짜 {in_range}일은 건너뜁니다 (--no-resume으로 끄기 가능)")

    all_rows: list[pd.DataFrame] = []
    pending: list[pd.DataFrame] = []
    trading_days = skipped_days = cached_days = 0
    stopped_early_at: str | None = None
    d = start
    try:
        while d <= end:
            if d in already:
                cached_days += 1
                d += timedelta(days=1)
                continue

            df = fetch_day_with_retry(args.source, d.strftime("%Y%m%d"), args.market, args.top_n)
            if df is not None and not df.empty:
                all_rows.append(df)
                pending.append(df)
                trading_days += 1
            else:
                skipped_days += 1

            # 3년치를 받으면 한두 시간이 걸린다. 끝까지 메모리에 들고 있다가
            # 마지막에 한 번 쓰면, 도중에 프로세스가 죽는 순간 전부 날아간다
            # (예전 백필이 실제로 중간에 멈췄던 전력이 있다). 주기적으로
            # 흘려보내면 최악의 경우에도 CHECKPOINT_EVERY 거래일치만 잃는다.
            if len(pending) >= CHECKPOINT_EVERY:
                snapshot.append_idempotent(pd.concat(pending, ignore_index=True), OUT_PATH)
                print(f"[checkpoint] {d} 까지 저장 · 거래일 {trading_days}", flush=True)
                pending = []

            if (trading_days + skipped_days) % 100 == 0:
                print(f"[progress] {d} 까지 · 거래일 {trading_days} · 휴장/주말 {skipped_days}", flush=True)
            time.sleep(args.delay)
            d += timedelta(days=1)
    except HardStop as exc:
        stopped_early_at = d.strftime("%Y%m%d")
        print(f"[stopped] {exc}")
        print(f"[stopped] 여기까지({stopped_early_at} 이전) 확보한 데이터는 저장하고 멈춥니다.")
        print("[stopped] 회복되면 같은 명령을 다시 실행하세요 — 빠진 날짜만 자동으로 이어받습니다.")
    except KeyboardInterrupt:
        stopped_early_at = d.strftime("%Y%m%d")
        print(f"\n[stopped] 사용자 중단 — {stopped_early_at} 이전까지 저장하고 멈춥니다.")

    if pending:
        snapshot.append_idempotent(pd.concat(pending, ignore_index=True), OUT_PATH)

    if not all_rows:
        if cached_days:
            print(f"[done] 요청 구간이 이미 모두 확보돼 있습니다 ({cached_days}일). 받을 것이 없습니다.")
            return 0
        print("[warn] 수집된 데이터가 없습니다.")
        return 1

    combined = pd.read_csv(OUT_PATH, dtype={"date": str, "ticker": str})

    meta = {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "source": ("KRX 직접 호출 (pykrx)" if args.source == "krx"
                   else "StockResearchMac backend (local FastAPI, /research/daily-snapshot)"),
        "backend_commit": backend_commit() if args.source == "backend" else None,
        "date_range": [args.start, args.end],
        "market": args.market,
        "top_n": args.top_n,
        "row_count": int(len(combined)),
        "trading_days_this_run": trading_days,
        "skipped_days_this_run": skipped_days,
        "trading_days_total_in_file": int(combined["date"].nunique()),
        "incomplete_stopped_at": stopped_early_at,
        "note": (
            "news_count는 과거 날짜에 대해 항상 비어 있습니다 — 네이버 뉴스 검색 API가 "
            "최신 기사만 색인하고 과거 날짜 아카이브를 지원하지 않아, 실측으로 확인 후 "
            "역사적 조회를 채우지 않기로 했습니다."
        ),
    }
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[done] {OUT_PATH} — 총 {len(combined)}행, 거래일 {combined['date'].nunique()}일 커버")
    print(f"[done] 계보 메타데이터: {META_PATH}")
    return 2 if stopped_early_at else 0


if __name__ == "__main__":
    raise SystemExit(main())
