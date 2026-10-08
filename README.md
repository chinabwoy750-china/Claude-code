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
