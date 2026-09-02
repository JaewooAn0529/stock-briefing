#!/bin/bash
# run_daily_log.sh — 맥에서 하루치 수급 스냅샷을 기록하고 커밋한다.
#
# 왜 GitHub Actions가 아니라 맥인가:
#   pykrx 1.2.8+ 는 수급 데이터에 KRX 로그인을 요구하는데, 클라우드 러너에서는
#   이게 통하지 않아 daily-log 워크플로가 2026-08-21부터 8회 연속 실패했다.
#   같은 코드가 이 맥에서는 정상 동작하는 것을 확인했다.
#
# 멱등성: 같은 날을 여러 번 실행해도 (date, ticker) 기준으로 덮어쓰므로 안전하다.
# 그래서 아래 launchd 스케줄은 넉넉하게 하루 두 번 걸어둔다 — 맥이 자고 있었거나
# 한 번 실패해도 그날 안에 만회된다.

set -uo pipefail

REPO="/Users/jaewooan/AIstocktrading/stock-briefing"
PYTHON="$REPO/venv/bin/python"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/daily_log.log"

mkdir -p "$LOG_DIR"
cd "$REPO" || exit 2

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

  if [ ! -x "$PYTHON" ]; then
    echo "[fatal] venv가 없습니다: $PYTHON"
    echo "        python3.11 -m venv venv && ./venv/bin/pip install -r requirements.txt"
    exit 2
  fi

  "$PYTHON" daily_log.py
  status=$?

  case $status in
    0) echo "[ok] 기록 완료" ;;
    1) echo "[skip] 휴장일 — 기록할 것이 없습니다"; exit 0 ;;
    *) echo "[fatal] daily_log.py 실패 (exit=$status)"; exit $status ;;
  esac

  # 변경이 있을 때만 커밋한다.
  if [ -n "$(git status --porcelain data/predictions.csv)" ]; then
    git add data/predictions.csv
    git commit -m "chore: daily log $(TZ=Asia/Seoul date '+%Y-%m-%d')" || {
      echo "[warn] 커밋 실패"; exit 0;
    }
    echo "[ok] 커밋 완료"

    # 푸시는 자격증명이 있을 때만. 없다고 해서 로컬 기록까지 실패시키지 않는다.
    if git push origin main 2>&1; then
      echo "[ok] 푸시 완료"
    else
      echo "[warn] 푸시 실패 — 로컬 커밋은 남아 있습니다."
      echo "       GitHub 인증이 설정되지 않았다면 README의 '맥에서 푸시 설정' 항목을 보세요."
    fi
  else
    echo "[info] 변경 없음 — 커밋 생략"
  fi
} >> "$LOG" 2>&1
