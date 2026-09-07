"""snapshot.py — 일별 수급 스냅샷을 가져오고 CSV에 append 하는 공용 코드.

daily_log.py(하루치)와 backfill.py(기간치)가 똑같은 일을 서로 다른 코드로
하고 있었다. append_idempotent와 COLUMNS가 두 파일에 복붙돼 있었고, 그 탓에
**두 파일이 date를 서로 다른 포맷으로 기록하는 버그**가 있었다:

    daily_log.py  -> "20260901"    (YYYYMMDD)
    backfill.py   -> "2026-09-01"  (ISO, 백엔드가 그렇게 내려줌)

같은 파일에 섞여 들어가는데 중복 제거 키가 (date, ticker)라서, 같은 날을 두
경로로 기록하면 중복이 제거되지 않고 두 행이 남는다. 게다가 backfill.py의
이어받기 로직은 strptime(max_date, "%Y-%m-%d")라서 YYYYMMDD 행이 하나라도
섞이면 그대로 예외로 죽는다. 실제 data/predictions.csv는 전부 ISO 포맷이므로
ISO로 통일한다 — 기존 데이터를 손대지 않아도 된다.

수집 경로는 두 가지이고, 둘 다 같은 스키마를 낸다:

  - "krx"     : pykrx로 KRX를 직접 호출한다. 짧게 살았다 죽는 프로세스에서
                가장 안정적이다(아래 참고).
  - "backend" : StockResearchMac 백엔드(localhost:8000)를 호출한다.

기본값이 "krx"인 이유: 상시 구동되는 백엔드는 pykrx 세션(유효기간 1시간)이
만료될 때 FastAPI 스레드 여러 개가 동시에 재로그인을 시도하고, KRX가 중복
로그인을 거부해서 일부만 성공한다. 실제 backend.log에서 재로그인 4건 중
3건이 "세션 갱신 실패"로 남고, 그 뒤 모든 호출이 JSON 대신 HTML을 받아
"Expecting value: line 1 column 1"로 깨지는 것을 확인했다. 반면 크론이 매번
새로 띄우는 프로세스는 로그인 한 번으로 수 초 안에 끝나서 이 경합 자체가
생기지 않는다.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

KST = timezone(timedelta(hours=9))

REPO_DIR = Path(__file__).resolve().parent
DATA_DIR = REPO_DIR / "data"
OUT_PATH = DATA_DIR / "predictions.csv"

DEFAULT_BACKEND = os.environ.get("STOCK_RESEARCH_BACKEND", "http://127.0.0.1:8000")

INDEX_CODE = {"KOSPI": "1001", "KOSDAQ": "2001"}

COLUMNS = [
    "date",             # T (거래일, ISO YYYY-MM-DD)
    "ticker",
    "name",
    "rank",             # 1 = 그날 외국인 순매수 1위
    "foreign_net_buy",  # 원, T일 외국인 순매수 거래대금
    "close",            # T일 종가
    "kospi_close",      # T일 KOSPI 지수 종가 (벤치마크 기준선)
    "news_count",       # 당일 기사 수 (없을 수 있음)
    "logged_at",        # 기록 시각 (감사 추적용)
]


class UpstreamFetchError(RuntimeError):
    """KRX 응답 실패(요청 제한/세션 끊김). 휴장일과 반드시 구분해야 한다.

    pykrx는 JSONDecodeError 같은 파싱 실패를 내부에서 삼키고 stdout에
    "Error occurred in ..."만 찍은 뒤 빈 DataFrame을 돌려준다
    (site-packages/pykrx/website/comm/util.py의 dataframe_empty_handler).
    그래서 겉보기엔 휴장일과 똑같은 "빈 데이터"가 된다. 반복 호출하는
    쪽에서 이 둘을 같게 취급하면, 사실은 데이터가 빠진 날인데 완결된 것처럼
    CSV에 남는다 — 휴장일은 건너뛰어야 하고 요청 실패는 재시도해야 한다.
    """


def load_env() -> None:
    """로컬 실행용 .env 로드. CI(GitHub Actions)에서는 이미 환경변수가 있으므로 무해하게 넘어간다.

    KRX_ID/KRX_PW가 없으면 pykrx는 로그인하지 않고, 로그인 없이는 수급 데이터
    엔드포인트가 JSON 대신 에러 페이지를 준다 — 즉 자격증명 누락이 "휴장일"처럼
    보이게 된다. 그래서 조용히 넘어가지 않고 아래 require_krx_credentials()로
    명시적으로 확인한다.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (REPO_DIR / ".env", REPO_DIR.parent / "stock-research-app" / "backend" / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)


def require_krx_credentials() -> None:
    missing = [k for k in ("KRX_ID", "KRX_PW") if not os.environ.get(k)]
    if missing:
        raise UpstreamFetchError(
            f"KRX 자격증명 누락: {', '.join(missing)}. "
            "pykrx 1.2.8+ 는 KRX 로그인이 있어야 수급 데이터를 준다 "
            "(없으면 휴장일과 구분되지 않는 빈 응답이 온다)."
        )


@contextlib.contextmanager
def detect_upstream_errors():
    """pykrx가 stdout으로만 흘리는 실패를 예외로 승격시킨다.

    부수적으로 pykrx가 로그인할 때마다 stdout에 찍는 KRX 로그인 ID를 걸러낸다
    (auth.py의 `print(f"  로그인 ID: {login_id}")`). 자격증명이 로그 파일이나
    CI 로그에 남을 이유가 없다.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield
    captured = buf.getvalue()
    for line in captured.splitlines():
        if "로그인 ID" in line:
            continue
        # "Error occurred in ..."은 아래에서 예외 메시지로 그대로 올라간다.
        # 여기서도 찍으면, 호출자가 "아직 데이터 없음"으로 정상 처리한 경우에도
        # 로그에 에러가 남아 진짜 고장처럼 보인다.
        if "Error occurred in" in line:
            continue
        if line.strip():
            print(line, file=sys.stdout)
    if "Error occurred in" in captured:
        raise UpstreamFetchError(captured.strip())


# pykrx가 "빈 결과"를 내면서 남기는 흔적. 빈 DataFrame에 컬럼명 8개를 붙이려다
# 나는 오류라서 문구가 이렇게 생겼다:
#   Length mismatch: Expected axis has 0 elements, new values have 8 elements
# 이건 응답 실패가 아니라 **그날 데이터가 아직 없다**는 뜻이다. 장중이나 마감
# 직후(정산 전)에 오늘 날짜로 조회하면 이 상태가 된다 — 실측 확인: 10:57 KST에
# 20260902는 이 오류, 20260901은 893행 정상.
# 휴장일과 같게 취급해서 전 거래일로 내려가야 하며, 진짜 응답 실패(로그인 끊김
# 등, "Expecting value: line 1 column 1")와는 반드시 구분해야 한다.
_NO_DATA_SIGNATURES = ("Expected axis has 0 elements",)


def is_no_data_error(exc: BaseException) -> bool:
    return any(sig in str(exc) for sig in _NO_DATA_SIGNATURES)


def import_pykrx_stock():
    """pykrx를 stdout 필터 안에서 import 한다.

    pykrx는 import 시점에 환경변수를 보고 자동 로그인하면서 로그인 ID를 그대로
    print 한다(auth.py의 `print(f"  로그인 ID: {login_id}")`). 즉 아무 방어 없이
    import 하는 것만으로 KRX 계정 ID가 크론 로그/CI 로그에 남는다. 실제 호출을
    감싸는 것만으로는 늦어서, import 자체를 필터 안에서 한다.
    """
    with detect_upstream_errors():
        from pykrx import stock
    return stock


def to_iso(date_yyyymmdd: str) -> str:
    d = date_yyyymmdd
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def to_compact(date_iso: str) -> str:
    return date_iso.replace("-", "")


# ---------------------------------------------------------------------------
# 수집 경로 1: KRX 직접 호출
# ---------------------------------------------------------------------------
def fetch_day_krx(date: str, market: str = "KOSPI", top_n: int | None = 20) -> pd.DataFrame | None:
    """하루치 스냅샷을 KRX에서 직접 가져온다. 휴장일이면 None, 응답 실패면 예외.

    date는 YYYYMMDD. 하루당 KRX 호출 3번(수급/시세/지수)이다.
    """
    if market not in INDEX_CODE:
        raise ValueError(f"지원하지 않는 시장: {market} (KOSPI/KOSDAQ만 가능)")

    stock = import_pykrx_stock()

    try:
        with detect_upstream_errors():
            flow = stock.get_market_net_purchases_of_equities_by_ticker(date, date, market, "외국인")
    except UpstreamFetchError as exc:
        if is_no_data_error(exc):
            return None  # 휴장일이거나 아직 공표 전 — 호출자가 전 거래일로 내려간다
        raise
    if flow is None or flow.empty:
        return None  # 에러 없이 비었다 = 진짜 휴장일

    with detect_upstream_errors():
        price = stock.get_market_ohlcv(date, market=market)
    with detect_upstream_errors():
        index = stock.get_index_ohlcv(date, date, INDEX_CODE[market])

    kospi_close = float(index["종가"].iloc[0]) if index is not None and not index.empty else None

    merged = flow.join(price[["종가"]], how="inner")
    merged = merged.sort_values("순매수거래대금", ascending=False)
    if top_n is not None:
        merged = merged.head(top_n)

    rows = []
    for rank, (ticker, row) in enumerate(merged.iterrows(), start=1):
        rows.append({
            "date": to_iso(date),
            "ticker": ticker,
            "name": row["종목명"],
            "rank": rank,
            "foreign_net_buy": int(row["순매수거래대금"]),
            "close": int(row["종가"]),
            # KOSDAQ으로 부르면 이 필드에 KOSDAQ 지수가 들어간다.
            # 스키마 호환을 위해 이름은 kospi_close로 고정한다.
            "kospi_close": kospi_close,
            "news_count": pd.NA,
            "logged_at": datetime.now(KST).isoformat(timespec="seconds"),
        })

    return pd.DataFrame(rows)[COLUMNS] if rows else None


# ---------------------------------------------------------------------------
# 수집 경로 2: 로컬 백엔드
# ---------------------------------------------------------------------------
def fetch_day_backend(
    date: str,
    market: str = "KOSPI",
    top_n: int | None = 20,
    backend: str = DEFAULT_BACKEND,
) -> pd.DataFrame | None:
    """백엔드 /research/daily-snapshot 한 번 호출. 404=휴장일(None), 503=요청 실패(예외)."""
    import requests

    r = requests.get(
        f"{backend}/research/daily-snapshot",
        params={"date": date, "market": market, "top_n": top_n},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    if r.status_code == 503:
        raise UpstreamFetchError(f"{date}: 백엔드가 KRX 응답 실패를 보고함 — {r.text[:200]}")
    r.raise_for_status()

    rows = r.json().get("rows") or []
    if not rows:
        return None

    df = pd.DataFrame(rows)
    # 과거 날짜의 뉴스 건수는 채울 수 없다 — 네이버 뉴스 검색 API가 최신 기사만
    # 색인하고 과거 아카이브를 지원하지 않는다(실측 확인).
    df["news_count"] = pd.NA
    df["logged_at"] = datetime.now(KST).isoformat(timespec="seconds")
    return df[COLUMNS]


def fetch_day(source: str, date: str, market: str = "KOSPI", top_n: int | None = 20) -> pd.DataFrame | None:
    if source == "krx":
        return fetch_day_krx(date, market, top_n)
    if source == "backend":
        return fetch_day_backend(date, market, top_n)
    raise ValueError(f"알 수 없는 source: {source} (krx|backend)")


# ---------------------------------------------------------------------------
# 재시도
# ---------------------------------------------------------------------------
# 네트워크가 순간적으로 끊겼을 때 나타나는 흔적들. 실제로 2026-09-03 21:00
# 실행이 RemoteDisconnected 한 번으로 exit 2까지 갔다 — 몇 초 뒤면 멀쩡한
# 상황인데 그날 기록을 통째로 날린 셈이다.
_TRANSIENT_SIGNATURES = (
    "RemoteDisconnected",
    "Connection aborted",
    "Connection reset",
    "ConnectionError",
    "IncompleteRead",
    "timed out",
    "Timeout",
    "Temporary failure in name resolution",
    "Max retries exceeded",
)


def is_transient_error(exc: BaseException) -> bool:
    blob = f"{type(exc).__name__}: {exc}"
    return any(sig in blob for sig in _TRANSIENT_SIGNATURES)


class RetriesExhausted(UpstreamFetchError):
    """재시도를 다 써도 회복되지 않음."""


def fetch_day_with_retry(
    source: str,
    date: str,
    market: str = "KOSPI",
    top_n: int | None = 20,
    backoffs: tuple[int, ...] = (5, 15, 45),
) -> pd.DataFrame | None:
    """일시적 실패는 쉬었다 다시, 휴장일은 None, 진짜 고장은 예외.

    "데이터 없음"(휴장일·공표 전)은 재시도해도 달라지지 않으므로 즉시 None을
    돌려준다. 이걸 재시도에 섞으면 휴장일마다 쓸데없이 몇 분씩 잡아먹는다.
    """
    last: BaseException | None = None
    for wait in (0,) + tuple(backoffs):
        if wait:
            print(f"[retry] {wait}초 후 재시도 ({date}) — 직전 오류: {str(last)[:120]}")
            time.sleep(wait)
        try:
            return fetch_day(source, date, market, top_n)
        except UpstreamFetchError as exc:
            last = exc
        except Exception as exc:
            if not is_transient_error(exc):
                raise
            last = exc
    raise RetriesExhausted(f"{date}: {len(backoffs)}회 재시도해도 실패 — {last}")


# ---------------------------------------------------------------------------
# 거래일 탐색 / 저장
# ---------------------------------------------------------------------------
def resolve_trading_day(source: str, start: str, market: str = "KOSPI", lookback: int = 10) -> str | None:
    """start(YYYYMMDD)부터 거꾸로 훑어 실제 데이터가 있는 첫 날을 찾는다.

    pykrx의 get_nearest_business_day_in_a_week()는 KRX를 직접 긁는데 간헐적으로
    아무것도 안 돌려줘서 쓰지 않는다. 대신 실제 데이터를 찔러보며 내려간다 —
    데이터 가용성 확인을 겸한다.

    응답 실패(UpstreamFetchError)는 여기서 삼키지 않고 그대로 올린다. 예전에는
    로그인 실패가 "lookback 내내 데이터 없음" = 휴장일처럼 보여서, 자격증명
    문제가 조용한 무데이터로 둔갑했다.
    """
    cursor = datetime.strptime(start, "%Y%m%d")
    for _ in range(lookback):
        candidate = cursor.strftime("%Y%m%d")
        df = fetch_day_with_retry(source, candidate, market, top_n=1)
        if df is not None and not df.empty:
            if candidate != start:
                print(f"[info] {start}은 데이터가 없어 {candidate}을 사용합니다")
            return candidate
        cursor -= timedelta(days=1)
    return None


def _content_signature(df: pd.DataFrame) -> pd.Series:
    """logged_at을 뺀 실제 내용의 행별 지문.

    CSV에서 읽은 결측은 NaN, 새로 만든 행은 pd.NA라서 그냥 astype(str)로
    비교하면 'nan' vs '<NA>'로 갈린다. 빈 문자열로 맞춘 뒤 비교한다.
    """
    cols = [c for c in COLUMNS if c != "logged_at"]
    return df[cols].fillna("").astype(str).agg("|".join, axis=1)


def _normalize_date(value) -> str:
    """과거에 daily_log.py가 YYYYMMDD로 쓴 행이 남아 있을 수 있어 ISO로 맞춘다."""
    if isinstance(value, str) and len(value) == 8 and value.isdigit():
        return to_iso(value)
    return value


def validate_log(df: pd.DataFrame) -> list[str]:
    """CSV가 지켜야 할 불변식을 검사한다.

    하루치 스냅샷은 "그날 상위 N종목"이므로, 한 날짜 안에서 rank는 중복 없이
    1..N이어야 하고 같은 종목이 두 번 나올 수 없다. 이게 깨지면 evaluate.py의
    순위별 집계가 조용히 어긋난다 — 실제로 2026-09-02에 rank 20이 두 개 생겨
    41일치 분석에 유령 행이 섞였다. 그래서 쓰기 경로마다 검사한다.
    """
    problems: list[str] = []

    dupes = df[df.duplicated(subset=["date", "ticker"], keep=False)]
    if not dupes.empty:
        for d in sorted(dupes["date"].unique()):
            problems.append(f"{d}: 같은 종목이 중복 기록됨")

    for d, group in df.groupby("date"):
        ranks = sorted(group["rank"].tolist())
        if ranks != list(range(1, len(group) + 1)):
            problems.append(
                f"{d}: rank가 1..{len(group)} 연속이 아님 "
                f"({len(group)}행, 중복 {len(ranks) - len(set(ranks))}개)"
            )

    return problems


def append_idempotent(new_rows: pd.DataFrame, path: Path = OUT_PATH) -> pd.DataFrame:
    """날짜 단위로 통째 교체하며 append. 크래시로 파일이 깨지지 않게 원자적으로 쓴다.

    종목 단위가 아니라 **날짜 단위로 교체**하는 게 핵심이다. 예전에는 (date,
    ticker)로 중복 제거해서 병합했는데, KRX가 장 마감 직후의 잠정 수급을 나중에
    확정치로 바꾸면 상위 N에서 밀려난 종목의 행이 그대로 남았다. 실제로
    2026-09-02은 17:00 실행이 HD현대중공업을 20위로 기록했고 22:00 확정치에서는
    한화가 20위였는데, 둘 다 남아 21행 · rank 20이 두 개가 됐다.

    하루치 스냅샷은 "그날의 상위 N" 이라는 하나의 관측이므로 부분 병합이
    성립하지 않는다 — 다시 기록하면 그 날짜는 통째로 새 관측으로 갈아끼운다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    new_rows = new_rows.copy()
    new_rows["date"] = new_rows["date"].map(_normalize_date)

    if path.exists():
        existing = pd.read_csv(path, dtype={"date": str, "ticker": str})
        existing["date"] = existing["date"].map(_normalize_date)

        # 내용이 완전히 같은 날짜는 손대지 않는다. logged_at만 갱신하면 파일이
        # 매번 바뀌고, launchd가 하루 두 번 도는 탓에 의미 없는 커밋이 매일
        # 쌓인다. 진짜 데이터가 바뀐 날만 diff에 남아야 이력을 읽을 수 있다.
        replace_dates = []
        for d, new_slice in new_rows.groupby("date"):
            old_slice = existing[existing["date"] == d]
            if set(_content_signature(old_slice)) != set(_content_signature(new_slice)):
                replace_dates.append(d)

        existing = existing[~existing["date"].isin(replace_dates)]
        new_rows = new_rows[new_rows["date"].isin(replace_dates)]
        # news_count는 값이 있는 날과 통째로 비어 있는 날이 섞인다(과거 날짜는
        # 네이버가 색인하지 않아 항상 비어 있다). 한쪽 프레임이 전부 NA면 pandas가
        # dtype 추론이 바뀔 것이라고 FutureWarning을 낸다 — 양쪽을 같은 nullable
        # 정수형으로 맞춰두면 경고도, 나중에 바뀔 동작도 없다.
        for frame in (existing, new_rows):
            frame["news_count"] = pd.to_numeric(frame["news_count"], errors="coerce").astype("Int64")

        frames = [f for f in (existing, new_rows) if not f.empty]
        combined = pd.concat(frames, ignore_index=True) if frames else existing
    else:
        combined = new_rows

    combined = combined.sort_values(["date", "rank"]).reset_index(drop=True)

    for problem in validate_log(combined):
        print(f"[warn] 데이터 불변식 위반 — {problem}")

    tmp = path.with_suffix(".csv.tmp")
    combined.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(path)
    return combined
