# Claude Code on Render → China-GPT

This service runs the current native Claude Code CLI and provides an
Anthropic-to-OpenAI adapter for the China-GPT gateway.

## Render environment variables

Required:

- `CHINA_GPT_BASE_URL=https://gpt-china.onrender.com`
- `CHINA_GPT_API_KEY=<your crk_live key>`
- `MODEL=dahl/MiniMaxAI/MiniMax-M2.7`
- `APP_API_KEY=<create your own long random secret>`

Optional:

- `REQUEST_TIMEOUT=300`

Never commit these secrets to GitHub.

## Endpoints

- `GET /health`
- `GET /claude/version` (requires `Authorization: Bearer APP_API_KEY`)
- `POST /claude/run` (requires `Authorization: Bearer APP_API_KEY`)
- `GET /v1/models` (requires `Authorization: Bearer APP_API_KEY`)
- `POST /v1/messages` (used internally by Claude Code)

Example request:

POST /claude/run
Authorization: Bearer YOUR_APP_API_KEY
Content-Type: application/json

{
  "prompt": "Inspect the project and explain what it does.",
  "model": "dahl/MiniMaxAI/MiniMax-M2.7"
}

Security note: `/claude/run` can execute commands through Claude Code.
Keep `APP_API_KEY` private and do not expose this endpoint without authentication.
