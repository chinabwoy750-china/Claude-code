# Claude Code Web

Replace the current deployment files with this package.

## Keep existing Render variables
- CHINA_GPT_BASE_URL=https://gpt-china.onrender.com
- CHINA_GPT_API_KEY=your existing private gateway key
- MODEL=dahl/MiniMaxAI/MiniMax-M2.7
- APP_API_KEY=your existing private app key
- REQUEST_TIMEOUT=300 (optional)

## Add
- UI_PASSWORD=choose a password you will use to open the web UI
- SESSION_SECRET=long random secret

Do not put gateway or APP_API_KEY secrets into the browser.

## Render Start Command
`uvicorn server:app --app-dir /app --host 0.0.0.0 --port $PORT`

## Result
Open your Render URL and you get a login screen, then a chat screen with a model selector. The browser calls `/api/chat`; the server runs Claude Code; Claude Code calls the local Anthropic-compatible adapter; the adapter calls your gateway.

The custom model can still produce Claude Code's non-fatal `unrecognized_model` stderr warning; the selected model is still sent to the gateway.
