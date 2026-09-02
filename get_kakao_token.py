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
import sys
import urllib.parse
import webbrowser

import requests

REDIRECT_URI = "https://localhost"
AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SCOPE = "talk_message"


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

    code = input("3) code= 뒤의 값을 붙여넣으세요: ").strip()
    if not code:
        print("[fatal] code가 비었습니다.")
        return 1
    # 주소창을 통째로 붙여넣는 실수가 잦아서 code만 뽑아준다.
    if "code=" in code:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code", [code])[0]

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

    print()
    print("=" * 66)
    print("발급 완료. 아래 값을 GitHub Secrets에 등록하세요.")
    print("  저장소 → Settings → Secrets and variables → Actions")
    print("=" * 66)
    print(f"KAKAO_REFRESH_TOKEN = {refresh_token}")
    print("=" * 66)
    print(f"(refresh token 유효기간: 약 {payload.get('refresh_token_expires_in', 0) // 86400}일)")
    print()
    print("이 값은 카카오톡 발송 권한 그 자체입니다 — 커밋하거나 공유하지 마세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
