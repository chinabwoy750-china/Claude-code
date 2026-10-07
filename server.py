import os
import json
import uuid
import asyncio
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Claude Code + China-GPT")

BASE_URL = os.getenv("CHINA_GPT_BASE_URL", "https://gpt-china.onrender.com").rstrip("/")
CHINA_KEY = os.getenv("CHINA_GPT_API_KEY", "")
DEFAULT_MODEL = os.getenv("MODEL", "dahl/MiniMaxAI/MiniMax-M2.7")
APP_API_KEY = os.getenv("APP_API_KEY", "")
PORT = os.getenv("PORT", "10000")
TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "300"))

def gateway_headers():
    if not CHINA_KEY:
        raise RuntimeError("CHINA_GPT_API_KEY is not configured")
    return {
        "Authorization": f"Bearer {CHINA_KEY}",
        "Content-Type": "application/json",
    }

def require_app_key(authorization: str | None):
    if not APP_API_KEY:
        raise HTTPException(503, "APP_API_KEY is not configured")
    if authorization != f"Bearer {APP_API_KEY}":
        raise HTTPException(401, "Invalid API key")

def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            out.append(str(block.get("content", "")))
    return "\n".join(x for x in out if x)

def anthropic_to_openai(body: dict) -> dict:
    messages = []
    system = body.get("system")
    if system:
        if isinstance(system, list):
            system = "\n".join(
                x.get("text", "") for x in system if isinstance(x, dict)
            )
        messages.append({"role": "system", "content": str(system)})

    for m in body.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            messages.append({"role": "user", "content": content_to_text(content)})
        elif role == "assistant":
            messages.append({"role": "assistant", "content": content_to_text(content)})

    result = {
        "model": body.get("model") or DEFAULT_MODEL,
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }

    for key in ("max_tokens", "temperature", "top_p", "stop"):
        if body.get(key) is not None:
            result[key] = body[key]

    # Translate common Claude tool schemas to OpenAI function tools.
    if body.get("tools"):
        result["tools"] = [{
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object"}),
            }
        } for t in body["tools"]]

    return result

def openai_to_anthropic(data: dict, requested_model: str) -> dict:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks = []

    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except Exception:
            inp = {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:16]}"),
            "name": fn.get("name", ""),
            "input": inp,
        })

    finish = choice.get("finish_reason")
    stop_reason = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
    }.get(finish, "end_turn")

    usage = data.get("usage") or {}
    return {
        "id": data.get("id", f"msg_{uuid.uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": data.get("model", requested_model),
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }

@app.get("/")
async def root():
    return {
        "service": "Claude Code + China-GPT",
        "status": "ok",
        "claude_code": True,
        "adapter": True,
        "default_model": DEFAULT_MODEL,
    }

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "gateway": BASE_URL,
        "default_model": DEFAULT_MODEL,
        "claude_code": True,
    }

@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)):
    require_app_key(authorization)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{BASE_URL}/v1/models", headers=gateway_headers())
    return JSONResponse(status_code=r.status_code, content=r.json())

@app.post("/v1/messages")
async def messages(body: dict):
    payload = anthropic_to_openai(body)
    requested_model = payload["model"]

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        if not body.get("stream"):
            r = await client.post(
                f"{BASE_URL}/v1/chat/completions",
                headers=gateway_headers(),
                json=payload,
            )
            if r.status_code >= 400:
                try:
                    detail = r.json()
                except Exception:
                    detail = {"error": r.text}
                return JSONResponse(status_code=r.status_code, content=detail)
            return JSONResponse(
                content=openai_to_anthropic(r.json(), requested_model)
            )

    async def stream():
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{BASE_URL}/v1/chat/completions",
                headers=gateway_headers(),
                json=payload,
            ) as r:
                if r.status_code >= 400:
                    raw = await r.aread()
                    yield "event: error\n"
                    yield "data: " + json.dumps({
                        "type": "error",
                        "error": {"type": "api_error", "message": raw.decode(errors="replace")}
                    }) + "\n\n"
                    return

                msg_id = f"msg_{uuid.uuid4().hex}"
                yield "event: message_start\n"
                yield "data: " + json.dumps({
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": requested_model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    }
                }) + "\n\n"

                text_started = False
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(raw)
                    except Exception:
                        continue

                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}

                    if delta.get("content"):
                        if not text_started:
                            text_started = True
                            yield "event: content_block_start\n"
                            yield "data: " + json.dumps({
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {"type": "text", "text": ""},
                            }) + "\n\n"

                        yield "event: content_block_delta\n"
                        yield "data: " + json.dumps({
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {
                                "type": "text_delta",
                                "text": delta["content"],
                            },
                        }) + "\n\n"

                if text_started:
                    yield "event: content_block_stop\n"
                    yield "data: " + json.dumps({
                        "type": "content_block_stop", "index": 0
                    }) + "\n\n"

                yield "event: message_delta\n"
                yield "data: " + json.dumps({
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                }) + "\n\n"

                yield "event: message_stop\n"
                yield "data: " + json.dumps({"type": "message_stop"}) + "\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")

@app.get("/claude/version")
async def claude_version(authorization: str | None = Header(default=None)):
    require_app_key(authorization)
    proc = await asyncio.create_subprocess_exec(
        "claude", "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return {
        "returncode": proc.returncode,
        "stdout": stdout.decode().strip(),
        "stderr": stderr.decode().strip(),
    }

@app.post("/claude/run")
async def claude_run(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """
    Run Claude Code headlessly.

    JSON:
      {"prompt":"...", "model":"...", "permission_mode":"bypassPermissions"}
    """
    require_app_key(authorization)
    body = await request.json()

    prompt = body.get("prompt")
    if not prompt:
        raise HTTPException(400, "prompt is required")

    model = body.get("model") or DEFAULT_MODEL
    permission_mode = body.get("permission_mode", "bypassPermissions")

    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        model,
        "--permission-mode",
        permission_mode,
    ]

    env = os.environ.copy()
    # Claude Code talks to this same service; this endpoint then translates
    # its Anthropic requests to China-GPT.
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{PORT}"
    env["ANTHROPIC_API_KEY"] = "local-adapter"
    env["ANTHROPIC_MODEL"] = model
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd="/workspace",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await proc.communicate()

    return JSONResponse({
        "returncode": proc.returncode,
        "model": model,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    })

@app.post("/v1/messages/count_tokens")
async def count_tokens(body: dict):
    text = json.dumps(body.get("messages", [])) + json.dumps(body.get("system", ""))
    return {"input_tokens": max(1, len(text) // 4)}
