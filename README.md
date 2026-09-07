# 📊 주식 브리핑 + 수급 시그널 기록/검증

두 가지 일을 한다.

1. **아침 브리핑** — 매일 07:00(KST) 관심 종목의 전일 종가와 오늘 뉴스를 카카오톡("나와의 채팅")으로 보낸다. GitHub Actions에서 돌기 때문에 컴퓨터가 꺼져 있어도 발송된다.
2. **수급 시그널 기록/검증** — 매 거래일 "외국인 순매수 상위 20종목"을 기록해두고, 나중에 그 시그널이 실제로 수익이 났는지 사후 측정한다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `briefing.py` | 종가(pykrx) + 오늘 뉴스(네이버) 수집 → 카카오 발송 |
| `snapshot.py` | 일별 수급 스냅샷 수집·저장 공용 코드 (`daily_log`/`backfill`이 함께 쓴다) |
| `daily_log.py` | 하루치 스냅샷 기록 → `data/predictions.csv` |
| `backfill.py` | 과거 기간 스냅샷 일괄 기록 |
| `evaluate.py` | 기록된 시그널의 사후 수익률 측정 (T+1/T+5/T+20) |
| `get_kakao_token.py` | (최초 1회 / 만료 시) 카카오 refresh token 발급 도우미 |
| `scripts/run_daily_log.sh` | 맥에서 일일 기록을 돌리고 커밋하는 러너 |
| `scripts/com.jaewoo.stockbriefing.dailylog.plist` | 위 러너의 launchd 예약 |
| `.github/workflows/daily-briefing.yml` | 매일 07:00 KST 브리핑 발송 |
| `.github/workflows/daily_log_1.yml` | 수동 전용 예비 경로 (아래 "왜 맥에서 도는가" 참고) |

## 데이터 스키마 (`data/predictions.csv`)

| 컬럼 | 뜻 |
|---|---|
| `date` | 거래일 T (ISO `YYYY-MM-DD`) |
| `ticker` / `name` | 종목 |
| `rank` | 그날 외국인 순매수 순위 (1 = 1위) |
| `foreign_net_buy` | T일 외국인 순매수 거래대금 (원) |
| `close` | T일 종가 |
| `kospi_close` | T일 KOSPI 지수 종가 (벤치마크 기준선) |
| `news_count` | 당일 기사 수 (아래 주의) |
| `logged_at` | 기록 시각 |

**`news_count` 주의사항**

- 과거 날짜는 항상 비어 있다. 네이버 검색 API가 최신 기사만 색인하고 과거 아카이브를 지원하지 않는다.
- **1000은 "1000건"이 아니라 "1000건 이상"이다.** 네이버 검색 API는 `start` 파라미터를 1000까지만 허용해서 그 위로는 셀 수 없다. 삼성전자·SK하이닉스 같은 대형주는 대부분 여기 걸린다. 상한에 걸리면 실행 로그에 경고가 남는다.
- 예전에는 한 페이지(100건)만 받아서 셌기 때문에 기사가 많은 종목이 전부 정확히 `100`으로 기록됐다(기록된 39개 값 중 21개). 지금은 페이지를 넘겨가며 세므로 1000 미만은 실제 개수다. **2026-09-07 이전에 기록된 `100`은 신뢰할 수 없다.**

`daily_log.py`는 **T일 정보만** 기록하고 수익률은 계산하지 않는다. 외국인 순매수는 장 마감 후 확정되므로 T일 종가로 진입한 것처럼 계산하면 실제로는 알 수 없었던 정보를 쓴 셈이 된다. 수익률은 `evaluate.py`가 **T+1 시가 진입 → T+h 종가 청산**으로 따로 측정한다.

---

## 1. 아침 브리핑 설정

### 1-1. 카카오 refresh token 발급 (최초 1회)
1. [developers.kakao.com](https://developers.kakao.com) → 앱 생성 → **REST API 키** 확인
2. 카카오 로그인 ON + Redirect URI에 `https://localhost` 등록
3. 동의항목에서 **카카오톡 메시지 전송(talk_message)** 켜기
4. 터미널에서:

```bash
python get_kakao_token.py
```

출력된 `refresh_token`을 복사해 둔다.

### 1-2. Secrets 등록
저장소 → Settings → Secrets and variables → **Actions**

| 이름 | 값 | 쓰이는 곳 |
|---|---|---|
| `NAVER_CLIENT_ID` | 네이버 개발자센터 Client ID | 브리핑·뉴스 건수 |
| `NAVER_CLIENT_SECRET` | 네이버 개발자센터 Client Secret | 브리핑·뉴스 건수 |
| `KAKAO_REST_API_KEY` | 카카오 REST API 키 | 브리핑 발송 |
| `KAKAO_REFRESH_TOKEN` | 1-1에서 발급한 값 | 본인 발송 |
| `KAKAO_REFRESH_TOKEN_2` | (선택) 가족 계정으로 발급한 값 | 가족 발송 |
| `KRX_ID` / `KRX_PW` | KRX 회원 계정 | 수급 데이터 조회 |

> `KRX_ID`/`KRX_PW`는 지우면 안 된다. pykrx 1.2.8부터 수급 데이터(외국인 순매수) 조회에 KRX 로그인이 **필수**다. 없으면 에러가 아니라 **빈 응답**이 와서 "휴장일"과 구분되지 않는다.

### 1-3. 테스트
Actions 탭 → `daily-stock-briefing` → **Run workflow**.

## 2. 수급 시그널 기록 (맥에서 실행)

```bash
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env    # KRX_ID / KRX_PW / NAVER_* 채우기
```

수동 실행:

```bash
./venv/bin/python daily_log.py
```

자동 예약(launchd) 설치:

```bash
cp scripts/com.jaewoo.stockbriefing.dailylog.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jaewoo.stockbriefing.dailylog.plist
```

로그는 `logs/daily_log.log`에 쌓인다.

### 왜 맥에서 도는가

원래는 GitHub Actions가 매 거래일 이 작업을 돌렸다. 그런데 pykrx가 수급 데이터에 KRX 로그인을 요구하게 되면서 클라우드 러너에서 이게 통하지 않게 됐고, **2026-08-21부터 8회 연속으로 조용히 실패**했다(그동안 데이터가 하루도 쌓이지 않았다). 같은 코드가 맥에서는 정상 동작하는 것을 확인했기 때문에, 일일 기록은 맥의 launchd로 옮기고 워크플로는 수동 전용 예비 경로로 남겼다.

매일 실패하는 예약을 켜둔 채로 두면 실패 알림이 무뎌져서 정작 진짜 고장을 놓치게 된다 — 그래서 예약 자체를 뗐다.

### 과거 데이터 백필

```bash
./venv/bin/python backfill.py --start 20230101 --end 20260901
```

중간에 끊겨도 다시 같은 명령을 실행하면 마지막 기록 다음날부터 이어받는다. KRX 요청 제한에 걸리면 점점 길게 쉬며 재시도하고, 그래도 안 되면 **멈춘다** — 남은 날짜를 "휴장일"로 잘못 기록하느니 미완으로 두는 편이 낫기 때문이다(그렇게 기록되면 데이터가 빠졌는데도 완결된 것처럼 보인다).

## 3. 시그널 검증

```bash
./venv/bin/python evaluate.py
```

- **진입 규칙**: T+1 시가 매수 → T+h 종가 매도 (h = 1, 5, 20 거래일)
- **벤치마크**: 같은 구간의 KOSPI 지수. 초과수익 = 종목수익 − 지수수익
- **t통계량**: (종목,날짜) 행 전체가 아니라 **날짜별 평균**으로 계산한다. 같은 날 상위 20종목은 그날의 시장 충격을 공유해 독립 표본이 아니어서, 행 단위로 검정하면 유의성이 크게 부풀려진다.

결과는 `data/evaluation.json`에도 저장된다. 가격 데이터는 `data/.price_cache/`에 캐시되므로 재실행은 빠르다(`--refresh`로 무시).

> 수수료·세금·슬리피지는 반영하지 않았다. 실제 성과는 출력값보다 낮다.

## 맥에서 푸시 설정

`run_daily_log.sh`는 기록을 로컬 커밋한 뒤 푸시를 시도한다. 이 맥에는 현재 GitHub 자격증명이 없어서 푸시 단계만 실패하고 넘어간다(로컬 커밋은 정상). 자동 푸시를 쓰려면 둘 중 하나를 설정한다.

SSH 키:

```bash
ssh-keygen -t ed25519 -C "jaewooan0529@gmail.com"
```

생성된 `~/.ssh/id_ed25519.pub`를 GitHub → Settings → SSH and GPG keys에 등록한 뒤:

```bash
git remote set-url origin git@github.com:JaewooAn0529/stock-briefing.git
```

또는 Personal Access Token(repo 권한)을 만들어 `git push` 시 비밀번호 자리에 입력하면 osxkeychain이 기억한다.

## 참고사항

- **브리핑 시각**: GitHub Actions는 UTC 기준이라 `cron: "0 22 * * *"` = 한국 아침 7시. 부하에 따라 몇 분~수십 분 지연될 수 있다(무료 서비스 특성).
- **refresh token 만료**: 약 2개월. 만료되면 `briefing.py`가 워크플로를 실패로 만들고 무엇을 해야 하는지 로그에 남긴다. 만료가 임박해 카카오가 새 토큰을 내려주면 경고로 알려준다.
- **주말/휴장일**: 브리핑은 장이 없던 날도 "최근 영업일 종가"로 발송된다. 수급 기록은 휴장일이면 아무것도 쓰지 않고 종료 코드 1로 끝난다.
- **맥의 타임존**: 현재 이 맥은 UTC+8이라 KST보다 1시간 느리다. launchd 예약은 로컬 시각 기준이라 이 차이를 감안해 걸려 있고, 스크립트 자체는 한국 기준으로 마지막 거래일을 찾으므로 타임존이 바뀌어도 잘못된 날을 기록하지 않는다.
