# 📊 매일 아침 7시 주식 브리핑 → 카카오톡 자동 발송

컴퓨터가 꺼져 있어도 GitHub Actions(클라우드)가 매일 아침 7시(KST)에
삼성전자·SK하이닉스의 **전일 종가 + 오늘자 뉴스**를 내 카카오톡("나와의 채팅")으로 보냅니다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `briefing.py` | 종가(pykrx) + 오늘 뉴스(네이버) 수집 → 카카오 발송 |
| `.github/workflows/daily-briefing.yml` | 매일 07:00 KST 자동 실행 예약 |
| `requirements.txt` | 필요 라이브러리 |
| `get_kakao_token.py` | (최초 1회) 카카오 refresh token 발급 도우미 |

## 설정 순서

### 1. 카카오 refresh token 발급 (최초 1회, 내 컴퓨터에서)
1. [developers.kakao.com](https://developers.kakao.com) → 앱 생성 → **REST API 키** 확인
2. 카카오 로그인 ON + Redirect URI에 `https://localhost` 등록
3. 동의항목에서 **카카오톡 메시지 전송** 켜기
4. 터미널에서:
   ```bash
   pip install requests
   python get_kakao_token.py
   ```
   안내에 따라 진행하면 `refresh_token`이 출력됩니다. 복사해 두세요.

### 2. GitHub 저장소 만들기
1. [github.com](https://github.com) 가입 → New repository (**Private** 권장)
2. 이 폴더의 파일 전체를 업로드
   - 웹에서: "uploading an existing file" 클릭 후 드래그
   - `.github/workflows/daily-briefing.yml` 경로가 유지되어야 합니다

### 3. Secrets 등록 (비밀값 4개)
저장소 → Settings → Secrets and variables → **Actions** → New repository secret

| 이름 | 값 |
|---|---|
| `NAVER_CLIENT_ID` | 네이버 개발자센터 Client ID |
| `NAVER_CLIENT_SECRET` | 네이버 개발자센터 Client Secret |
| `KAKAO_REST_API_KEY` | 카카오 REST API 키 |
| `KAKAO_REFRESH_TOKEN` | 1단계에서 발급한 refresh token |

### 4. 테스트
저장소 → **Actions** 탭 → `daily-stock-briefing` → **Run workflow** 버튼으로 수동 실행.
카카오톡 "나와의 채팅"에 메시지가 오면 성공! 이후 매일 아침 7시에 자동 발송됩니다.

## 참고사항
- **시간**: GitHub Actions는 UTC 기준이라 `cron: "0 22 * * *"` = 한국 아침 7시.
  부하에 따라 몇 분~수십 분 지연될 수 있습니다(무료 서비스 특성).
- **주말/휴장일**: 장이 없던 날도 "최근 영업일 종가"로 발송됩니다.
- **refresh token 만료**: 약 2개월. 만료로 발송이 멈추면 `get_kakao_token.py`를
  다시 실행해 새 토큰을 Secrets에 갱신하세요.
- **뉴스가 없는 날**: 아침 7시 기준 "오늘" 발행 기사가 아직 없으면
  가격 메시지만 발송될 수 있습니다.
