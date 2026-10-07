# Claude Code → China-GPT adapter

This service translates Claude/Anthropic Messages API requests into the
OpenAI-compatible `/v1/chat/completions` API exposed by:

https://gpt-china.onrender.com

## Render environment variables

Set these in Render, not in GitHub:

- `CHINA_GPT_BASE_URL` = `https://gpt-china.onrender.com`
- `CHINA_GPT_API_KEY` = your `crk_live_...` key
- `MODEL` = your preferred model ID, for example `openai/gpt-5`

The model is not hard-coded. A model supplied by Claude Code in the request
is forwarded to the gateway, so model switching can be supported.

## Health check

`GET /health`

## Important

Do not commit your China-GPT API key to GitHub.
