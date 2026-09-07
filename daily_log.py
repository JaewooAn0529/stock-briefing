"""daily_log.py — 시그널 검증용 일별 스냅샷 기록기.

거래일 T마다 기록한다:
    - 외국인 순매수 상위 N종목
    - 그날 종가
    - KOSPI 지수 종가 (벤치마크)
    - (선택) 당일 기사 수

미래참조(lookahead) 방지:
    이 스크립트는 T일 정보만 기록하고 수익률은 절대 계산하지 않는다. 외국인
    순매수는 장 마감 후 확정되므로, 여기서 나온 시그널은 T+1부터만 실행할 수
    있다. 수익률 평가는 evaluate.py가 따로 T+1/T+5/T+20으로 측정한다.

출력: data/predictions.csv (append 전용, (date, ticker)당 멱등)

종료 코드:
    0 -> 거래일 데이터를 정상 기록(또는 이미 존재)
    1 -> 돌긴 했으나 쓸 데이터가 없음(휴장일)
    2 -> 하드 실패(KRX 자격증명/응답 실패) — 크론이 조용히 초록불이 되지 않게 한다
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

import snapshot
from snapshot import KST, OUT_PATH, UpstreamFetchError

TOP_N = 20
MARKET = "KOSPI"
LOOKBACK_DAYS = 10

EXIT_OK = 0
EXIT_NOTHING = 1
EXIT_HARD_FAIL = 2


def today_kst() -> str:
    """러너의 타임존과 무관하게 한국 기준 오늘."""
    return datetime.now(KST).strftime("%Y%m%d")


# 네이버 검색 API 한 페이지 최대치와, start 파라미터가 허용하는 상한.
NAVER_DISPLAY = 100
NAVER_MAX_START = 1000


def _naver_news_page(name: str, start: int, client_id: str, client_secret: str) -> list[dict]:
    """뉴스 검색 한 페이지. 일시적 오류는 짧게 한 번 재시도한다.

    실제로 IncompleteRead로 한 종목 조회가 통째로 날아간 적이 있다
    (2026-09-03 로그). 한 번만 다시 시도해도 대부분 넘어간다.
    """
    url = (
        "https://openapi.naver.com/v1/search/news.json"
        f"?query={urllib.parse.quote(name)}&display={NAVER_DISPLAY}&start={start}&sort=date"
    )
    for attempt in range(2):
        try:
            req = urllib.request.Request(url)
            req.add_header("X-Naver-Client-Id", client_id)
            req.add_header("X-Naver-Client-Secret", client_secret)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8")).get("items", [])
        except Exception:
            if attempt == 1:
                raise
            time.sleep(1)
    return []


def _pub_date(item: dict):
    pub = item.get("pubDate")
    if not pub:
        return None
    try:
        return datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST).date()
    except ValueError:
        return None


def fetch_news_counts(names: list[str], date: str) -> dict[str, int | None]:
    """종목별 당일 기사 수 (네이버 뉴스 API). 자격증명이 없으면 조용히 건너뛴다.

    과거 날짜에는 의미가 없다 — 네이버 검색 API가 최신 기사만 색인하고 과거
    아카이브를 지원하지 않기 때문이다. 그래서 당일 기록에서만 값이 찬다.

    한 페이지(100건)만 받아서 세면 기사가 많은 종목은 전부 정확히 100이 된다.
    실제로 기록된 39개 값 중 21개가 100이었다 — "100건"이 아니라 "100건 이상"인데
    숫자로는 구분이 안 되니, 이 필드로 분석하면 인기 종목만 골라서 값이 눌린다.
    sort=date라 오늘 기사가 앞쪽에 몰려 있으므로, 어제 기사가 나올 때까지만
    페이지를 넘기면 정확한 개수를 얻는다(보통 1~2번, 많아야 10번).
    """
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not (client_id and client_secret):
        return {n: None for n in names}

    target = datetime.strptime(date, "%Y%m%d").date()
    counts: dict[str, int | None] = {}

    for name in names:
        try:
            same_day = 0
            start = 1
            saturated = True
            while start <= NAVER_MAX_START:
                items = _naver_news_page(name, start, client_id, client_secret)
                if not items:
                    saturated = False
                    break

                older_seen = False
                for item in items:
                    pub_date = _pub_date(item)
                    if pub_date is None:
                        continue
                    if pub_date == target:
                        same_day += 1
                    elif pub_date < target:
                        # 날짜 내림차순이라 목표일보다 과거가 나오면 더 볼 필요가 없다.
                        older_seen = True
                        break

                if older_seen or len(items) < NAVER_DISPLAY:
                    saturated = False
                    break
                start += NAVER_DISPLAY

            if saturated:
                # API가 허용하는 끝(1000건)까지 갔는데도 전부 당일 기사였다.
                # 드문 경우지만 이때의 값은 여전히 "이상"이라 로그로 남긴다.
                print(f"[warn] {name}: 당일 기사가 {NAVER_MAX_START}건 이상 — 값이 상한에 걸렸습니다")
            counts[name] = same_day
        except Exception as exc:  # 뉴스 때문에 본 로그가 깨지면 안 된다
            print(f"[warn] {name} 뉴스 조회 실패: {exc}")
            counts[name] = None

    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="하루치 수급 스냅샷을 기록한다")
    ap.add_argument("date", nargs="?", help="YYYYMMDD (생략하면 한국 기준 오늘)")
    ap.add_argument("--source", choices=["krx", "backend"], default="krx",
                    help="krx=KRX 직접 호출(기본), backend=로컬 백엔드 경유")
    ap.add_argument("--market", default=MARKET)
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--no-news", action="store_true", help="뉴스 건수 수집을 건너뛴다")
    args = ap.parse_args()

    snapshot.load_env()
    if args.source == "krx":
        try:
            snapshot.require_krx_credentials()
        except UpstreamFetchError as exc:
            print(f"[fatal] {exc}")
            return EXIT_HARD_FAIL

    start = args.date or today_kst()

    try:
        date = snapshot.resolve_trading_day(args.source, start, args.market, LOOKBACK_DAYS)
    except UpstreamFetchError as exc:
        # 응답 실패를 "데이터 없음"으로 삼키지 않는다. 예전 버전은 이걸 구분하지
        # 못해서 로그인 실패가 휴장일처럼 보였다.
        print(f"[fatal] KRX 응답 실패로 거래일을 확정할 수 없습니다: {exc}")
        return EXIT_HARD_FAIL
    except Exception as exc:
        print(f"[fatal] 거래일 확정 실패: {exc}")
        return EXIT_HARD_FAIL

    if date is None:
        print(f"[skip] {start} 이전 {LOOKBACK_DAYS}일 안에 거래일이 없습니다 (연휴일 수 있음)")
        return EXIT_NOTHING

    print(f"[info] 거래일 {date} 기록 (source={args.source})")

    try:
        rows = snapshot.fetch_day_with_retry(args.source, date, args.market, args.top_n)
    except UpstreamFetchError as exc:
        print(f"[fatal] {date} 수급 데이터 조회 실패: {exc}")
        return EXIT_HARD_FAIL
    except Exception as exc:
        print(f"[fatal] {date} 조회 중 예기치 못한 오류: {exc}")
        return EXIT_HARD_FAIL

    if rows is None or rows.empty:
        print(f"[warn] {date} 수급 데이터가 없습니다; 기록하지 않음")
        return EXIT_NOTHING

    if not args.no_news and date == today_kst():
        news = fetch_news_counts(rows["name"].tolist(), date)
        rows["news_count"] = rows["name"].map(news)

    try:
        combined = snapshot.append_idempotent(rows, OUT_PATH)
    except Exception as exc:
        print(f"[fatal] {OUT_PATH} 쓰기 실패: {exc}")
        return EXIT_HARD_FAIL

    if not OUT_PATH.exists():
        print(f"[fatal] 기록 후에도 {OUT_PATH}가 존재하지 않습니다")
        return EXIT_HARD_FAIL

    print(f"[done] {len(rows)}행 기록 ({date}) -> {OUT_PATH}")
    print(f"[done] 누적 {len(combined)}행 · 거래일 {combined['date'].nunique()}일")
    print(rows.head(5).to_string(index=False))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
