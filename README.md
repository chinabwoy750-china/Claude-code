# Claude Code Web + Telegram

This single Render service provides the browser UI and a Telegram interface.

## Keep existing variables
- CHINA_GPT_BASE_URL=https://gpt-china.onrender.com
- CHINA_GPT_API_KEY=your existing private gateway key
- MODEL=dahl/MiniMaxAI/MiniMax-M2.7
- APP_API_KEY=your existing private API key

## Add web variables
- UI_PASSWORD=your private web password
- SESSION_SECRET=a long random secret
- REQUEST_TIMEOUT=300

## Add Telegram variables
- TELEGRAM_BOT_TOKEN=the token from BotFather
- TELEGRAM_ALLOWED_CHAT_ID=8543081950

Never put any of these secrets in the browser.

## Telegram commands
- /start
- /models
- /model
- /model MODEL_ID
- /clear

Normal messages are sent to Claude Code. Only the allowed chat ID is accepted.

## Render Start Command
uvicorn server:app --app-dir /app --host 0.0.0.0 --port $PORT


## Agent jobs
Telegram:
- 
- 
- 
-  

HTTP (UI session):
-  
-   

Jobs store under . Trading is paper/research only.


## Phase 1 agent expansion

Job types: social, affiliate, trade, yt_script, yt_assets, course_outline, job_search, job_packet, trade_live

Telegram:
- /job_yt niche=... topic=... minutes=8
- /job_yt_assets topic=...
- /job_course topic=... audience=... platform=gumroad
- /job_search roles=... location=remote
- /job_packet role=... company=...
- /job_trade_live symbols=BTCUSDT  (needs /approve; paper by default)
- /approve JOB_ID /reject JOB_ID
- /trade_halt /trade_resume /trade_status

Trading env (optional):
- TRADE_MODE=paper|live (default paper)
- TRADE_MAX_DOLLARS=10
- TRADE_MAX_RISK_USD=1
- TRADE_MAX_DAILY_LOSS_USD=2

Live exchange keys are NOT wired yet. trade_live only plans JSON. Faceless YT produces scripts/asset plans only — no auto-upload until OAuth + media APIs are added.
