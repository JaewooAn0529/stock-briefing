"""
매일 아침 주식 브리핑 (수신자별 종목 다르게 발송)

- 본인(KAKAO_REFRESH_TOKEN): 4종목 전체
    삼성전자, SK하이닉스, ABL바이오, LG에너지솔루션
- 가족(KAKAO_REFRESH_TOKEN_2, 있으면 자동 발송): 신규 2종목만
    ABL바이오, LG에너지솔루션
"""

import os
import json
import requests
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

from pykrx import stock

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.date()
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# 종목 마스터 목록 (이름, 티커)
STOCKS = {
    "삼성전자": "005930",
    "SK하이닉스": "000660",
    "ABL바이오": "298380",
    "LG에너지솔루션": "373220",
}

# 수신자별로 어떤 종목을 받을지 정의
# key: 환경변수 이름 / value: 그 사람이 받을 종목 이름 리스트
RECIPIENTS = {
    "KAKAO_REFRESH_TOKEN": ["삼성전자", "SK하이닉스", "ABL바이오", "LG에너지솔루션"],   # 본인: 4종목
    "KAKAO_REFRESH_TOKEN_2": ["ABL바이오", "LG에너지솔루션"],                            # 가족: 신규 2종목만
}

NAVER_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_SECRET = os.environ["NAVER_CLIENT_SECRET"]
KAKAO_KEY = os.environ["KAKAO_REST_API_KEY"]


def get_price(ticker: str):
    end = NOW
    start = end - timedelta(days=14)
    df = stock.get_market_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker)
    if df.empty or len(df) < 2:
        return None
    latest, prev = df.iloc[-1], df.iloc[-2]
    close = int(latest["종가"])
    pct = (close - prev["종가"]) / prev["종가"] * 100
    return {"close": close, "pct": pct, "date": df.index[-1].strftime("%m/%d")}


def strip_tags(s: str) -> str:
    return (
        s.replace("<b>", "").replace("</b>", "")
         .replace("&quot;", '"').replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">")
    )


def today_news(query: str, limit: int = 2):
    res = requests.get(
        "https://openapi.naver.com/v1/search/news.json",
        headers={
            "X-Naver-Client-Id": NAVER_ID,
            "X-Naver-Client-Secret": NAVER_SECRET,
        },
        params={"query": query, "display": 100, "sort": "date"},
        timeout=10,
    )
    res.raise_for_status()
    out = []
    for item in res.json().get("items", []):
        pub = parsedate_to_datetime(item["pubDate"]).astimezone(KST)
        if pub.date() != TODAY:
            continue
        title = strip_tags(item["title"])
        if query[:2] not in title:
            continue
        out.append(title)
        if len(out) >= limit:
            break
    return out


def kakao_access_token(refresh_token: str) -> str:
    res = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": KAKAO_KEY,
            "refresh_token": refresh_token,
        },
        timeout=10,
    )
    res.raise_for_status()
    return res.json()["access_token"]


def send_kakao(text: str, token: str):
    res = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "template_object": json.dumps({
                "object_type": "text",
                "text": text[:200],
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
        _price_cache[ticker] = get_price(ticker)
    return _price_cache[ticker]


def get_news_cached(name: str):
    if name not in _news_cache:
        _news_cache[name] = today_news(name)
    return _news_cache[name]


def build_messages_for(stock_names: list[str]):
    """주어진 종목 리스트에 대한 메시지 목록을 만든다."""
    msgs = []
    lines = [f"📊 주식브리핑 {NOW.month}/{NOW.day}({WEEKDAY_KR[NOW.weekday()]})"]
    for name in stock_names:
        ticker = STOCKS[name]
        p = get_price_cached(ticker)
        if p:
            lines.append(f"▸{name} {p['close']:,}원 {p['pct']:+.2f}% ({p['date']} 종가)")
        else:
            lines.append(f"▸{name} 시세 조회 실패")
    msgs.append("\n".join(lines))

    for name in stock_names:
        news = get_news_cached(name)
        if news:
            body = "\n".join(f"■ {t}" for t in news)
            msgs.append(f"📰 {name} 오늘 뉴스\n{body}")

    return msgs


def main():
    ok = 0
    total = 0
    for env_name, stock_names in RECIPIENTS.items():
        refresh = os.environ.get(env_name)
        if not refresh:
            continue  # 토큰 없으면 조용히 스킵 (가족 미등록 상태에서도 안전)
        total += 1
        try:
            msgs = build_messages_for(stock_names)
            token = kakao_access_token(refresh)
            for m in msgs:
                send_kakao(m, token)
            ok += 1
            print(f"[{env_name}] sent {len(msgs)} message(s) for {stock_names}")
        except Exception as e:
            print(f"[{env_name}] FAILED - {e}")
    print(f"done: {ok}/{total} recipients at {NOW.isoformat()}")


if __name__ == "__main__":
    main()
