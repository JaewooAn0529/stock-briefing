"""매일 아침 주식 브리핑 (수신자별 종목 다르게 발송)

- 본인(KAKAO_REFRESH_TOKEN): 4종목 전체
    삼성전자, SK하이닉스, ABL바이오, LG에너지솔루션
- 가족(KAKAO_REFRESH_TOKEN_2, 있으면 자동 발송): 신규 2종목만
    ABL바이오, LG에너지솔루션
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.date()
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# 카카오 text 템플릿의 text 필드 상한. 넘으면 API가 거절한다.
KAKAO_TEXT_LIMIT = 200

# refresh token 잔여가 이 일수 이하로 떨어지면 경고로 승격한다.
# 재발급은 브라우저 로그인이 필요해서 사람이 시간을 내야 하므로 여유를 둔다.
EXPIRY_WARN_DAYS = 14

# 종목 마스터 목록 (이름, 티커)
STOCKS = {
    "삼성전자": "005930",
    "SK하이닉스": "000660",
    "ABL바이오": "298380",
    "LG에너지솔루션": "373220",
}

# 뉴스 제목에서 이 종목을 가리키는 것으로 인정할 표기들.
# 기사 제목은 정식 종목명을 그대로 쓰지 않는 경우가 많다(LG엔솔, 삼전 등).
NEWS_ALIASES = {
    "삼성전자": ["삼성전자"],
    "SK하이닉스": ["SK하이닉스", "하이닉스"],
    "ABL바이오": ["ABL바이오", "에이비엘바이오"],
    "LG에너지솔루션": ["LG에너지솔루션", "LG엔솔"],
}

# 수신자별로 어떤 종목을 받을지 정의
# key: 환경변수 이름 / value: 그 사람이 받을 종목 이름 리스트
RECIPIENTS = {
    "KAKAO_REFRESH_TOKEN": ["삼성전자", "SK하이닉스", "ABL바이오", "LG에너지솔루션"],   # 본인: 4종목
    "KAKAO_REFRESH_TOKEN_2": ["ABL바이오", "LG에너지솔루션"],                            # 가족: 신규 2종목만
}


def require_env(name: str) -> str:
    """없으면 무슨 값이 빠졌는지 분명히 알리고 죽는다.

    예전에는 모듈 최상단에서 os.environ["..."]로 읽어서, Secrets 하나가 비면
    스택트레이스만 남는 KeyError로 죽었다. 원인을 로그에서 바로 읽을 수 있게 한다.
    """
    value = os.environ.get(name)
    if not value:
        fail(f"필수 환경변수 {name} 가 비어 있습니다. GitHub Secrets를 확인하세요.")
        raise SystemExit(1)
    return value


def fail(message: str) -> None:
    """실패를 눈에 띄게 남긴다.

    브리핑이 안 오는 걸 사용자가 알아채려면 Actions 탭을 직접 봐야 했다.
    `::error::` 로 찍으면 GitHub이 실행을 빨간불로 만들고 기본 설정에서 실패
    알림 메일을 보내준다 — 추가 인프라 없이 쓸 수 있는 가장 확실한 통보 경로다.
    """
    print(f"::error::{message}", file=sys.stderr)


def warn(message: str) -> None:
    print(f"::warning::{message}", file=sys.stderr)


NAVER_ID = require_env("NAVER_CLIENT_ID")
NAVER_SECRET = require_env("NAVER_CLIENT_SECRET")
KAKAO_KEY = require_env("KAKAO_REST_API_KEY")


def get_price(ticker: str):
    from pykrx import stock

    end = NOW
    start = end - timedelta(days=14)
    df = stock.get_market_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker)
    if df is None or df.empty or len(df) < 2:
        return None
    latest, prev = df.iloc[-1], df.iloc[-2]
    if prev["종가"] == 0:
        return None
    close = int(latest["종가"])
    pct = (close - prev["종가"]) / prev["종가"] * 100
    return {"close": close, "pct": pct, "date": df.index[-1].strftime("%m/%d")}


def strip_tags(s: str) -> str:
    return (
        s.replace("<b>", "").replace("</b>", "")
         .replace("&quot;", '"').replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">")
    )


def mentions_stock(title: str, name: str) -> bool:
    """제목이 이 종목을 다루는지 판정.

    예전 조건은 `query[:2] not in title` — 종목명 앞 두 글자만 보는 것이라
    "SK하이닉스"를 찾는데 "SK"만 걸려도 통과했다. 그래서 SK텔레콤·SK이노베이션
    같은 무관한 SK그룹 기사가 하이닉스 뉴스로 섞여 들어왔다. 별칭을 포함한
    전체 표기가 들어있을 때만 인정한다.
    """
    normalized = title.replace(" ", "").upper()
    return any(alias.replace(" ", "").upper() in normalized for alias in NEWS_ALIASES.get(name, [name]))


def today_news(name: str, limit: int = 2):
    try:
        res = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            headers={
                "X-Naver-Client-Id": NAVER_ID,
                "X-Naver-Client-Secret": NAVER_SECRET,
            },
            params={"query": name, "display": 100, "sort": "date"},
            timeout=10,
        )
        res.raise_for_status()
    except Exception as exc:
        # 뉴스가 안 되더라도 시세 브리핑은 나가야 한다.
        warn(f"{name} 뉴스 조회 실패: {exc}")
        return []

    out = []
    for item in res.json().get("items", []):
        try:
            pub = parsedate_to_datetime(item["pubDate"]).astimezone(KST)
        except Exception:
            continue
        if pub.date() != TODAY:
            continue
        title = strip_tags(item["title"])
        if not mentions_stock(title, name):
            continue
        out.append(title)
        if len(out) >= limit:
            break
    return out


class TokenExpired(RuntimeError):
    """refresh token이 만료/무효. 사람이 재발급해야 하므로 다른 실패와 구분한다."""


def kakao_access_token(refresh_token: str, env_name: str) -> str:
    res = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": KAKAO_KEY,
            "refresh_token": refresh_token,
        },
        timeout=10,
    )

    if res.status_code == 400:
        # 카카오 refresh token은 약 2개월이면 만료된다. 만료를 일반 오류처럼
        # 넘기면 "왜 브리핑이 안 오지"만 반복하게 된다 — 할 일을 명시한다.
        raise TokenExpired(
            f"{env_name} 이 만료/무효입니다(HTTP 400). "
            f"python get_kakao_token.py 를 다시 실행해 새 refresh token을 발급하고 "
            f"GitHub Secrets의 {env_name} 를 갱신하세요. 응답: {res.text[:200]}"
        )
    res.raise_for_status()

    payload = res.json()

    # 남은 수명을 매번 남긴다. Secrets는 등록 후 다시 읽을 수 없어서, 이 로그가
    # 아니면 "언제 만료되는지"를 알 방법이 없다 — 브리핑이 안 오고 나서야
    # 알아채게 된다.
    remaining = payload.get("refresh_token_expires_in")
    if remaining:
        days = remaining // 86400
        if days <= EXPIRY_WARN_DAYS:
            warn(
                f"{env_name}: refresh token이 약 {days}일 뒤 만료됩니다. "
                f"python get_kakao_token.py 로 재발급해 Secrets를 갱신하세요."
            )
        else:
            print(f"[info] {env_name}: refresh token 잔여 약 {days}일")

    # 카카오는 refresh token의 남은 기간이 1개월 미만이면 응답에 새 refresh
    # token을 함께 준다. 이때 갱신해두지 않으면 결국 만료로 발송이 멈춘다.
    if payload.get("refresh_token"):
        warn(
            f"{env_name}: 카카오가 새 refresh token을 발급했습니다(기존 토큰 만료 임박). "
            f"GitHub Secrets의 {env_name} 를 새 값으로 갱신하세요. "
            f"새 토큰은 보안상 로그에 남기지 않으니, 로컬에서 get_kakao_token.py 로 재발급받으면 됩니다."
        )
    return payload["access_token"]


def split_for_kakao(text: str) -> list[str]:
    """카카오 200자 제한에 맞춰 줄 단위로 나눈다.

    예전에는 text[:200]으로 잘라 버려서, 종목이 늘어나면 뒷부분이 조용히
    사라졌다(4종목이면 이미 상한에 근접한다). 잘라내는 대신 나눠 보낸다.
    """
    lines = text.split("\n")
    chunks, current = [], ""
    for line in lines:
        # 한 줄 자체가 상한을 넘으면 그 줄만 어쩔 수 없이 자른다.
        if len(line) > KAKAO_TEXT_LIMIT:
            line = line[:KAKAO_TEXT_LIMIT - 1] + "…"
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > KAKAO_TEXT_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_kakao(text: str, token: str):
    res = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "template_object": json.dumps({
                "object_type": "text",
                "text": text,
                "link": {
                    "web_url": "https://finance.naver.com",
                    "mobile_web_url": "https://m.stock.naver.com",
                },
                "button_title": "시세 확인",
            }, ensure_ascii=False)
        },
        timeout=10,
    )
    res.raise_for_status()


# 가격/뉴스는 종목당 한 번만 조회해서 캐싱 (같은 종목을 여러 명이 받아도 중복 조회 안 함)
_price_cache = {}
_news_cache = {}


def get_price_cached(ticker: str):
    if ticker not in _price_cache:
        try:
            _price_cache[ticker] = get_price(ticker)
        except Exception as exc:
            warn(f"{ticker} 시세 조회 실패: {exc}")
            _price_cache[ticker] = None
    return _price_cache[ticker]


def get_news_cached(name: str):
    if name not in _news_cache:
        _news_cache[name] = today_news(name)
    return _news_cache[name]


def build_messages_for(stock_names: list[str]):
    """주어진 종목 리스트에 대한 메시지 목록을 만든다."""
    msgs = []
    lines = [f"📊 주식브리핑 {NOW.month}/{NOW.day}({WEEKDAY_KR[NOW.weekday()]})"]
    price_failures = 0
    for name in stock_names:
        ticker = STOCKS[name]
        p = get_price_cached(ticker)
        if p:
            lines.append(f"▸{name} {p['close']:,}원 {p['pct']:+.2f}% ({p['date']} 종가)")
        else:
            lines.append(f"▸{name} 시세 조회 실패")
            price_failures += 1
    msgs.extend(split_for_kakao("\n".join(lines)))

    for name in stock_names:
        news = get_news_cached(name)
        if news:
            body = "\n".join(f"■ {t}" for t in news)
            msgs.extend(split_for_kakao(f"📰 {name} 오늘 뉴스\n{body}"))

    return msgs, price_failures


def main() -> int:
    ok = 0
    total = 0
    hard_failures = []

    for env_name, stock_names in RECIPIENTS.items():
        refresh = os.environ.get(env_name)
        if not refresh:
            # 미등록 수신자는 건너뛰되, 건너뛴 사실은 남긴다. 완전히 조용히
            # 넘어가는 바람에 워크플로가 KAKAO_REFRESH_TOKEN_2를 job에 전달하지
            # 않는다는 걸 아무도 눈치채지 못했고, 가족 발송이 계속 안 나갔다.
            print(f"[skip] {env_name} 미설정 — {stock_names} 발송 건너뜀")
            continue
        total += 1
        try:
            msgs, price_failures = build_messages_for(stock_names)
            token = kakao_access_token(refresh, env_name)
            for m in msgs:
                send_kakao(m, token)
            ok += 1
            note = f" (시세 실패 {price_failures}건)" if price_failures else ""
            print(f"[{env_name}] {len(msgs)}건 발송 · {stock_names}{note}")
            if price_failures == len(stock_names):
                # 전 종목 시세가 실패했다면 "브리핑은 갔지만 내용이 비어있다" —
                # 성공으로 넘기면 안 된다.
                hard_failures.append(f"{env_name}: 모든 종목 시세 조회 실패")
        except TokenExpired as exc:
            hard_failures.append(str(exc))
        except Exception as exc:
            hard_failures.append(f"{env_name} 발송 실패 — {type(exc).__name__}: {exc}")

    if total == 0:
        fail("발송 대상이 없습니다 — KAKAO_REFRESH_TOKEN 이 설정되지 않았습니다.")
        return 1

    for message in hard_failures:
        fail(message)

    print(f"done: {ok}/{total} recipients at {NOW.isoformat()}")
    # 한 명이라도 실패하면 워크플로를 빨간불로 만든다(= 실패 알림 메일).
    return 1 if hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
