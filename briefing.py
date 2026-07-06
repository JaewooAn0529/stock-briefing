"""
매일 아침 주식 브리핑 자동 발송 스크립트 (GitHub Actions용)

동작 순서:
  1) pykrx      → 삼성전자·SK하이닉스 최근 종가
  2) 네이버 API  → 종목별 오늘자 뉴스 (발행시각 필터)
  3) 카카오 REST → refresh token으로 access token 갱신 후 '나에게 보내기'

필요한 환경변수 (GitHub Secrets로 등록):
  NAVER_CLIENT_ID      : 네이버 개발자센터 Client ID
  NAVER_CLIENT_SECRET  : 네이버 개발자센터 Client Secret
  KAKAO_REST_API_KEY   : 카카오 개발자센터 REST API 키
  KAKAO_REFRESH_TOKEN  : 카카오 refresh token (최초 1회, get_kakao_token.py로 발급)
"""

import os
import json
import requests
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

from pykrx import stock

# ── 기준 시각: 실행 시점의 실제 한국 시간 ─────────────────────────
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.date()

PORTFOLIO = [
    ("삼성전자", "005930"),
    ("SK하이닉스", "000660"),
]

NAVER_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_SECRET = os.environ["NAVER_CLIENT_SECRET"]
KAKAO_KEY = os.environ["KAKAO_REST_API_KEY"]
KAKAO_REFRESH = os.environ["KAKAO_REFRESH_TOKEN"]


# ─────────────────────────────────────────────────────────────
# 1) 종가 (pykrx)
# ─────────────────────────────────────────────────────────────
def get_price(ticker: str):
    """최근 영업일 종가와 전일 대비 등락률을 반환."""
    end = NOW
    start = end - timedelta(days=14)
    df = stock.get_market_ohlcv(
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker
    )
    if df.empty or len(df) < 2:
        return None
    latest, prev = df.iloc[-1], df.iloc[-2]
    close = int(latest["종가"])
    pct = (close - prev["종가"]) / prev["종가"] * 100
    date = df.index[-1].strftime("%m/%d")
    return {"close": close, "pct": pct, "date": date}


# ─────────────────────────────────────────────────────────────
# 2) 오늘자 뉴스 (네이버 검색 API)
# ─────────────────────────────────────────────────────────────
def strip_tags(s: str) -> str:
    return (
        s.replace("<b>", "").replace("</b>", "")
         .replace("&quot;", '"').replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">")
    )


def today_news(query: str, limit: int = 2):
    """발행일이 '오늘(KST)'인 뉴스 제목을 최대 limit건 반환."""
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
        # 제목에 종목명이 실제로 포함된 기사만 (무관 기사 제거)
        if query.split()[0] not in title:
            continue
        out.append(title)
        if len(out) >= limit:
            break
    return out


# ─────────────────────────────────────────────────────────────
# 3) 카카오 '나에게 보내기'
# ─────────────────────────────────────────────────────────────
def kakao_access_token() -> str:
    """refresh token으로 새 access token 발급 (access token은 ~6시간 만료라 매번 갱신)."""
    res = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": KAKAO_KEY,
            "refresh_token": KAKAO_REFRESH,
        },
        timeout=10,
    )
    res.raise_for_status()
    return res.json()["access_token"]


def send_kakao(text: str, token: str):
    """텍스트 메시지를 '나와의 채팅'으로 발송. (템플릿 규격상 link 필수)"""
    res = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "template_object": json.dumps({
                "object_type": "text",
                "text": text[:200],  # 카카오 텍스트 템플릿 표시 한도
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


# ─────────────────────────────────────────────────────────────
# 브리핑 조립 & 발송
# ─────────────────────────────────────────────────────────────
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def build_messages():
    """카카오 200자 한도에 맞춰 메시지 리스트를 만든다."""
    msgs = []

    # (1) 가격
    lines = [f"📊 주식브리핑 {NOW.month}/{NOW.day}({WEEKDAY_KR[NOW.weekday()]})"]
    for name, ticker in PORTFOLIO:
        p = get_price(ticker)
        if p:
            lines.append(f"▸{name} {p['close']:,}원 {p['pct']:+.2f}% ({p['date']} 종가)")
        else:
            lines.append(f"▸{name} 시세 조회 실패")
    msgs.append("\n".join(lines))

    # (2) 종목별 오늘 뉴스
    for name, _ in PORTFOLIO:
        news = today_news(name)
        if news:
            body = "\n".join(f"■ {t}" for t in news)
            msgs.append(f"📰 {name} 오늘 뉴스\n{body}")

    return msgs


def main():
    msgs = build_messages()
    token = kakao_access_token()
    for m in msgs:
        send_kakao(m, token)
        print(f"sent ({len(m)} chars)")
    print(f"done: {len(msgs)} message(s) at {NOW.isoformat()}")


if __name__ == "__main__":
    main()
