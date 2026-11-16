"""A scripted OpenAI-compatible endpoint for tests and local demos.

System prompt is the activity-feed prompt                        -> "did <tools used>"
A screenshot image is the last message                          -> "SAW_IMAGE ..."
User message contains `TOOL:{"name": ..., "arguments": {...}}`  -> answers with that tool call
User message is `RECALL <text>`                                 -> "yes"/"no": is <text> in the system prompt?
Last message is a tool result                                   -> "RESULT: <tool output>"
User message contains `SLOW <seconds>`                          -> waits that long first (to try a busy bot)
Anything else                                                   -> "ECHO[<model>]: <message>"
"""
import asyncio
import json
import re
import uuid

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI()
EXPECTED_KEY = "sk-test-key"
# TeamBots puts the current time in front of the newest user message of an agent turn.
STAMP = re.compile(r"^\[[A-Z][a-z]{2} \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}[^\]\n]*\]\n")
SEEN: dict = {"stamps": 0, "systems": []}


@app.post("/v1/chat/completions")
async def completions(request: Request, authorization: str = Header(default="")):
    if authorization != f"Bearer {EXPECTED_KEY}":
        raise HTTPException(401, "bad key")
    body = await request.json()
    msgs = body["messages"]
    system = next((m["content"] for m in msgs if m["role"] == "system"), "")
    last = msgs[-1]
    text = last.get("content") or ""
    image = None
    if isinstance(text, list):  # a screenshot arrives as a user message with an image part
        image = next((p["image_url"]["url"] for p in text if p["type"] == "image_url"), None)
        text = " ".join(p.get("text", "") for p in text if p["type"] == "text")
    if STAMP.match(text):
        SEEN["stamps"] += 1
        text = STAMP.sub("", text, count=1)
    SEEN["systems"] = (SEEN["systems"] + [system])[-20:]
    if system.startswith("An AI teammate is busy"):  # routing of a message sent while the bot works
        new = text.split("New message: ", 1)[1]
        msg = {"role": "assistant", "content": "QUEUE" if "later" in new else "STEER"}
    elif image:
        msg ={"role": "assistant", "content": f"SAW_IMAGE {image[:23]} ({len(image)} chars)"}
    elif system.startswith("You write one entry for a team's shared activity feed"):
        msg = {"role": "assistant", "content": "did " + text.split("Tools used: ", 1)[1].split("\n", 1)[0]}
    elif last["role"] == "tool":
        msg = {"role": "assistant", "content": f"RESULT: {text}"}
    elif any(t["function"]["name"] == "ping" for t in body.get("tools") or []):  # the connection test
        msg = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_ping", "type": "function", "function": {"name": "ping", "arguments": '{"ok": true}'}}]}
    elif "TOOL:" in text and body.get("tools"):
        call = json.loads(text.split("TOOL:", 1)[1])
        msg = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_" + uuid.uuid4().hex[:12], "type": "function",
            "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}))}}]}
    elif "RECALL " in text:
        needle = text.split("RECALL ", 1)[1].strip()
        msg = {"role": "assistant", "content": "yes" if needle in system else "no"}
    else:
        slow = re.search(r"SLOW (\d+)", text)
        if slow:
            await asyncio.sleep(min(int(slow[1]), 60))
        msg = {"role": "assistant", "content": f"ECHO[{body['model']}]: {text}"}
    return {"id": "x", "object": "chat.completion", "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                      "prompt_tokens_details": {"cached_tokens": 2},
                      "completion_tokens_details": {"reasoning_tokens": 1}}}


@app.api_route("/echo", methods=["GET", "POST"])
async def echo(request: Request):
    """Reflects the request so tests can see what http_request really sent."""
    return {"auth": request.headers.get("authorization", ""), "body": (await request.body()).decode()}
