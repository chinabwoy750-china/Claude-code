import json
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Claude Code → OpenAI-compatible adapter")

BASE_URL = os.getenv("CHINA_GPT_BASE_URL", "https://gpt-china.onrender.com").rstrip("/")
API_KEY = os.getenv("CHINA_GPT_API_KEY", "")
DEFAULT_MODEL = os.getenv("MODEL", "openai/gpt-5")
TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "300"))

def auth_headers():
    if not API_KEY:
        raise RuntimeError("CHINA_GPT_API_KEY is not configured")
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        typ = block.get("type")
        if typ == "text":
            out.append(block.get("text", ""))
        elif typ == "image":
            # Most OpenAI-compatible gateways accept image_url.
            src = block.get("source", {})
            if src.get("type") == "base64":
                media = src.get("media_type", "image/jpeg")
                data = src.get("data", "")
                out.append(f"[image: data:{media};base64,{data}]")
        elif typ == "tool_result":
            out.append(str(block.get("content", "")))
    return "\n".join(x for x in out if x)

def anthropic_to_openai(body: dict) -> dict:
    messages = []

    system = body.get("system")
    if system:
        if isinstance(system, list):
            system = "\n".join(x.get("text", "") for x in system if isinstance(x, dict))
        messages.append({"role": "system", "content": str(system)})

    for m in body.get("messages", []):
        role = m.get("role")
        content = m.get("content")

        if role == "user":
            if isinstance(content, list):
                tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                normal = [b for b in content if not (isinstance(b, dict) and b.get("type") == "tool_result")]
                if normal:
                    messages.append({"role": "user", "content": content_to_text(normal)})
                for tr in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": content_to_text(tr.get("content", "")),
                    })
            else:
                messages.append({"role": "user", "content": content_to_text(content)})

        elif role == "assistant":
            msg = {"role": "assistant", "content": content_to_text(content)}
            tool_calls = []
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_calls.append({
                            "id": b.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input", {}), separators=(",", ":")),
                            },
                        })
            if tool_calls:
                msg["tool_calls"] = tool_calls
                msg["content"] = None
            messages.append(msg)

    result = {
        "model": body.get("model") or DEFAULT_MODEL,
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }

    # Preserve common generation parameters when supplied.
    for key in ("max_tokens", "temperature", "top_p", "stop"):
        if key in body and body[key] is not None:
            result[key] = body[key]

    tools = body.get("tools")
    if tools:
        result["tools"] = [{
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object"}),
            }
        } for t in tools]

    return result

def openai_message_to_anthropic(data: dict, requested_model: str) -> dict:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks = []

    text = msg.get("content")
    if text:
        blocks.append({"type": "text", "text": text})

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except Exception:
            inp = {"_raw": fn.get("arguments", "")}
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

async def gateway_post(payload: dict):
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        return await client.post(
            f"{BASE_URL}/v1/chat/completions",
            headers=auth_headers(),
            json=payload,
        )

@app.get("/health")
async def health():
    return {"status": "ok", "gateway": BASE_URL, "default_model": DEFAULT_MODEL}

@app.get("/v1/models")
async def models():
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{BASE_URL}/v1/models", headers=auth_headers())
    return JSONResponse(status_code=r.status_code, content=r.json())

@app.post("/v1/messages")
async def messages(body: dict, request: Request):
    payload = anthropic_to_openai(body)
    requested_model = payload["model"]

    if not body.get("stream"):
        r = await gateway_post(payload)
        if r.status_code >= 400:
            return JSONResponse(status_code=r.status_code, content=r.json())
        return JSONResponse(content=openai_message_to_anthropic(r.json(), requested_model))

    async def stream():
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{BASE_URL}/v1/chat/completions",
                headers=auth_headers(),
                json=payload,
            ) as r:
                if r.status_code >= 400:
                    err = await r.aread()
                    yield f"event: error\ndata: {err.decode(errors='replace')}\n\n"
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
                index = 0
                tool_buffers = {}
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
                        if index == 0:
                            yield "event: content_block_start\n"
                            yield "data: " + json.dumps({
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {"type": "text", "text": ""}
                            }) + "\n\n"
                        yield "event: content_block_delta\n"
                        yield "data: " + json.dumps({
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": delta["content"]}
                        }) + "\n\n"
                        index += 1

                    for tc in delta.get("tool_calls") or []:
                        ti = tc.get("index", 0)
                        fn = tc.get("function") or {}
                        if ti not in tool_buffers:
                            tool_buffers[ti] = {
                                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                                "name": fn.get("name", ""),
                            }
                            yield "event: content_block_start\n"
                            yield "data: " + json.dumps({
                                "type": "content_block_start",
                                "index": ti + 1,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tool_buffers[ti]["id"],
                                    "name": tool_buffers[ti]["name"],
                                    "input": {}
                                }
                            }) + "\n\n"
                        args = fn.get("arguments")
                        if args:
                            yield "event: content_block_delta\n"
                            yield "data: " + json.dumps({
                                "type": "content_block_delta",
                                "index": ti + 1,
                                "delta": {"type": "input_json_delta", "partial_json": args}
                            }) + "\n\n"

                # Close any open blocks.
                if index:
                    yield "event: content_block_stop\ndata: " + json.dumps({
                        "type": "content_block_stop", "index": 0
                    }) + "\n\n"
                for ti in tool_buffers:
                    yield "event: content_block_stop\ndata: " + json.dumps({
                        "type": "content_block_stop", "index": ti + 1
                    }) + "\n\n"

                yield "event: message_delta\n"
                yield "data: " + json.dumps({
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 0}
                }) + "\n\n"
                yield "event: message_stop\n"
                yield "data: " + json.dumps({"type": "message_stop"}) + "\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")

@app.post("/v1/messages/count_tokens")
async def count_tokens(body: dict):
    # Approximate count; the gateway remains responsible for real tokenization.
    text = json.dumps(body.get("messages", [])) + json.dumps(body.get("system", ""))
    approx = max(1, len(text) // 4)
    return {"input_tokens": approx}

@app.get("/")
async def root():
    return {"service": "Claude Code adapter", "status": "ok"}
