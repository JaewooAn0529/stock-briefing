Daily 7 AM Stock Briefing → Auto-sent to KakaoTalk

Even with your computer turned off, GitHub Actions (cloud) sends the previous day's closing prices for Samsung Electronics and SK Hynix, plus today's news, to your KakaoTalk ("Chat to Yourself") every morning at 7:00 AM KST.

File Structure
File	Role
briefing.py	Fetches closing prices (pykrx) + today's news (Naver) → sends via Kakao
.github/workflows/daily-briefing.yml	Scheduled to run automatically every day at 07:00 KST
requirements.txt	Required libraries
get_kakao_token.py	(One-time) Helper script to issue a Kakao refresh token
Setup Steps
1. Issue a Kakao refresh token (one-time, on your own computer)
Go to developers.kakao.com → create an app → check your REST API key
Turn Kakao Login ON + register https://localhost as a Redirect URI
In the consent items, enable Send KakaoTalk Message
In terminal:
   pip install requests
   python get_kakao_token.py

Follow the prompts, and a refresh_token will be printed. Save it.

2. Create a GitHub repository
Sign up at github.com → New repository (Private recommended)
Upload all files in this folder
On the web: click "uploading an existing file" and drag the files in
The .github/workflows/daily-briefing.yml path must be preserved
3. Register Secrets (4 values)

Repository → Settings → Secrets and variables → Actions → New repository secret

Name	Value
NAVER_CLIENT_ID	Naver Developers Client ID
NAVER_CLIENT_SECRET	Naver Developers Client Secret
KAKAO_REST_API_KEY	Kakao REST API key
KAKAO_REFRESH_TOKEN	The refresh token issued in Step 1
4. Test

Repository → Actions tab → daily-stock-briefing → click Run workflow to trigger it manually. If a message arrives in your KakaoTalk "Chat to Yourself," it's working! From then on, it will send automatically every morning at 7 AM.

Notes
Timing: GitHub Actions runs on UTC, so cron: "0 22 * * *" corresponds to 7 AM in Korea. Since this is a free service, actual runs may lag by a few minutes to tens of minutes depending on load.
Weekends/market holidays: On days when the market is closed, the briefing is sent using the "most recent trading day's closing price."
Refresh token expiration: Roughly 2 months. If the briefing stops arriving due to expiration, re-run get_kakao_token.py and update the new token in Secrets.
Days with no news: If no articles were published "today" as of 7 AM, only the price message may be sent.
