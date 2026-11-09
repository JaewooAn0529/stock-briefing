"""token_store.py: 카카오 refresh token을 저장소 안에 암호화해 보관하고 자동 갱신한다.

왜 필요한가:
    카카오 refresh token은 약 2개월이면 만료된다. 예전에는 GitHub Secrets에만
    두었는데, Secrets는 워크플로가 스스로 고쳐 쓸 수 없다. 그래서 카카오가
    만료 1개월 전에 새 토큰을 내려줘도 버려졌고, 결국 2026-09-08부터
    두 달 동안 브리핑이 KOE322(expired_or_invalid_refresh_token)로 멈춰 있었다.

어떻게:
    data/kakao_tokens.enc 에 {환경변수 이름: 암호문} JSON을 둔다.
    암호화 키는 Secrets에 이미 있는 KAKAO_REST_API_KEY 에서 유도한다.
    외부 패키지 없이 표준 라이브러리만 쓴다: HMAC-SHA256 카운터 모드 키스트림으로
    암호화하고, 별도 키의 HMAC-SHA256 태그로 위변조를 검사한다(encrypt-then-MAC).
    refresh token은 같은 REST API 키(client_id)가 있어야만 쓸 수 있으므로,
    공개 저장소에 암호문이 있어도 키를 모르면 쓸모가 없다.
    briefing.py 가 새 refresh token을 받으면 이 파일을 갱신하고,
    워크플로가 커밋한다. 매일 돌기만 하면 토큰이 끊기지 않는다.

수동 등록 (토큰을 새로 발급했을 때):
    KAKAO_REST_API_KEY=... python token_store.py set KAKAO_REFRESH_TOKEN <refresh_token>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

STORE_PATH = Path(__file__).resolve().parent / "data" / "kakao_tokens.enc"
_KEY_CONTEXT = b"stock-briefing/kakao-refresh-token/v1:"


def _keys(rest_api_key: str) -> tuple[bytes, bytes]:
    base = hashlib.sha256(_KEY_CONTEXT + rest_api_key.encode()).digest()
    enc = hmac.new(base, b"enc", hashlib.sha256).digest()
    mac = hmac.new(base, b"mac", hashlib.sha256).digest()
    return enc, mac


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def _encrypt(rest_api_key: str, env_name: str, plaintext: bytes) -> bytes:
    enc, mac = _keys(rest_api_key)
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(plaintext, _keystream(enc, nonce, len(plaintext))))
    tag = hmac.new(mac, env_name.encode() + b"|" + nonce + ct, hashlib.sha256).digest()
    return nonce + ct + tag


def _decrypt(rest_api_key: str, env_name: str, blob: bytes) -> bytes:
    enc, mac = _keys(rest_api_key)
    nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
    expected = hmac.new(mac, env_name.encode() + b"|" + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("tag mismatch")
    return bytes(a ^ b for a, b in zip(ct, _keystream(enc, nonce, len(ct))))


def _read() -> dict:
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(STORE_PATH.read_text())
    except Exception:
        return {}


def load(env_name: str, rest_api_key: str) -> str | None:
    """저장된 토큰을 복호화해 돌려준다. 없거나 못 풀면 None."""
    blob = _read().get(env_name)
    if not blob:
        return None
    try:
        return _decrypt(rest_api_key, env_name, base64.b64decode(blob)).decode()
    except Exception:
        return None


def save(env_name: str, rest_api_key: str, refresh_token: str) -> None:
    data = _read()
    data[env_name] = base64.b64encode(_encrypt(rest_api_key, env_name, refresh_token.encode())).decode()
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "set":
        print("usage: KAKAO_REST_API_KEY=... python token_store.py set <ENV_NAME> <refresh_token>")
        raise SystemExit(2)
    key = os.environ.get("KAKAO_REST_API_KEY")
    if not key:
        print("KAKAO_REST_API_KEY 환경변수가 필요합니다.")
        raise SystemExit(2)
    save(sys.argv[2], key, sys.argv[3])
    assert load(sys.argv[2], key) == sys.argv[3]
    print(f"saved {sys.argv[2]} -> {STORE_PATH}")
