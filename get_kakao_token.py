"""get_kakao_token.py — 카카오 refresh token 발급 도우미 (최초 1회 / 만료 시 재발급).

README가 계속 이 파일을 안내하고 있었는데 저장소에 존재한 적이 없었다.
카카오 refresh token은 약 2개월이면 만료되고, 만료되면 아침 브리핑이 조용히
멈추기 때문에 재발급 절차가 저장소 안에 있어야 한다.

사전 준비 (developers.kakao.com):
    1) 애플리케이션 생성 → REST API 키 확인
    2) 카카오 로그인 활성화 ON
    3) Redirect URI에 https://localhost 등록
    4) 동의항목에서 "카카오톡 메시지 전송(talk_message)" 사용 설정

실행:
    python get_kakao_token.py
    (REST API 키는 KAKAO_REST_API_KEY 환경변수로 넘겨도 되고, 물어보면 붙여넣어도 된다)

출력된 refresh token은 GitHub Secrets의 KAKAO_REFRESH_TOKEN 에 넣는다.
가족용으로 하나 더 발급하려면, 그 사람 계정으로 로그인한 뒤 나온 값을
KAKAO_REFRESH_TOKEN_2 에 넣는다.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
import webbrowser

import requests

REDIRECT_URI = "https://localhost"
AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SCOPE = "talk_message"


def extract_code(raw: str) -> str:
    """붙여넣은 값에서 code만 뽑는다.

    주소창을 통째로(https://localhost/?code=ABC), code=ABC 형태로, 혹은 값만
    붙여넣는 세 경우를 모두 받는다. 예전 구현은 가운데 경우에서 "code=ABC"를
    그대로 code로 넘겨 토큰 발급이 실패했다.
    """
    raw = raw.strip()
    if not raw:
        return ""
    match = re.search(r"[?&]?code=([^&\s]+)", raw)
    return match.group(1) if match else raw


def verify_refresh_token(rest_api_key: str, refresh_token: str) -> dict | None:
    """발급받은 refresh token이 실제로 쓸 수 있는지 즉시 확인한다.

    refresh 호출은 토큰을 소모하지 않으므로 안전하게 검증할 수 있다. Secrets에
    넣고 다음 날 아침 브리핑이 안 와서야 잘못된 걸 알게 되는 상황을 막는다.
    """
    res = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": rest_api_key,
            "refresh_token": refresh_token,
        },
        timeout=10,
    )
    if res.status_code != 200:
        print(f"[warn] 검증 실패 (HTTP {res.status_code}): {res.text[:200]}")
        return None
    return res.json()


def main() -> int:
    rest_api_key = os.environ.get("KAKAO_REST_API_KEY") or input("카카오 REST API 키를 붙여넣으세요: ").strip()
    if not rest_api_key:
        print("[fatal] REST API 키가 필요합니다.")
        return 1

    params = urllib.parse.urlencode({
        "client_id": rest_api_key,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
    })
    auth_url = f"{AUTHORIZE_URL}?{params}"

    print()
    print("1) 아래 주소를 브라우저에서 열고 카카오 로그인 + 동의를 진행하세요.")
    print("   (브라우저가 자동으로 열리지 않으면 직접 복사해 여세요)")
    print()
    print(f"   {auth_url}")
    print()
    print("2) 동의 후 주소창이 https://localhost/?code=XXXXX 로 바뀝니다.")
    print("   페이지는 '연결할 수 없음'으로 보이는 게 정상입니다 — 주소창의 code 값만 쓰면 됩니다.")
    print()

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    raw = input("3) code= 뒤의 값을 붙여넣으세요 (주소창 전체를 붙여넣어도 됩니다): ").strip()
    code = extract_code(raw)
    if not code:
        print("[fatal] code가 비었습니다.")
        return 1

    res = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": rest_api_key,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
        timeout=10,
    )

    if res.status_code != 200:
        print(f"[fatal] 토큰 발급 실패 (HTTP {res.status_code}): {res.text[:300]}")
        print("        code는 1회용이라 이미 썼다면 1)번부터 다시 하세요.")
        return 1

    payload = res.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        print(f"[fatal] 응답에 refresh_token이 없습니다: {payload}")
        return 1

    print("\n[검증] 발급받은 토큰이 실제로 동작하는지 확인합니다...")
    checked = verify_refresh_token(rest_api_key, refresh_token)
    if checked:
        print("[검증] 정상 — 이 토큰으로 access token을 받을 수 있습니다.")
        # 남은 기간이 1개월 미만이면 카카오가 여기서 새 refresh token을 준다.
        # 그 경우 방금 받은 것보다 이쪽이 최신이므로 이걸 등록해야 한다.
        if checked.get("refresh_token"):
            refresh_token = checked["refresh_token"]
            print("[검증] 카카오가 더 새로운 refresh token을 발급했습니다 — 아래 값이 그것입니다.")
    else:
        print("[검증] 확인에 실패했습니다. 아래 값을 등록하되, 등록 후 Actions에서 수동 실행으로 한 번 확인하세요.")

    days = payload.get("refresh_token_expires_in", 0) // 86400
    print()
    print("=" * 66)
    print("발급 완료. 아래 값을 GitHub Secrets에 등록하세요.")
    print("  https://github.com/JaewooAn0529/stock-briefing/settings/secrets/actions")
    print("=" * 66)
    print(f"KAKAO_REFRESH_TOKEN = {refresh_token}")
    print("=" * 66)
    print(f"(refresh token 유효기간: 약 {days}일 — 다음 갱신 시점을 달력에 적어두세요)")
    print()
    print("주의:")
    print("  · 이 값은 카카오톡 발송 권한 그 자체입니다 — 커밋하거나 공유하지 마세요.")
    print("  · 방금 브라우저에서 로그인한 계정의 토큰입니다. 가족용")
    print("    (KAKAO_REFRESH_TOKEN_2)을 발급하려면 시크릿 창에서 그 계정으로")
    print("    로그인한 뒤 이 스크립트를 다시 실행하세요.")
    print("  · 등록 후 Actions 탭 → daily-stock-briefing → Run workflow 로 확인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
