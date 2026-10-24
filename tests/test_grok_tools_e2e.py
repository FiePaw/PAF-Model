"""
Loopback e2e: Grok function calling through the REAL gateway (uvicorn +
vps_server) with a fake Grok worker over WebSocket.

Verifies the gateway now forwards `tools` and (on the Turn-2 request)
`tool_messages` to the Grok worker, and maps a worker `tool_calls` result to an
OpenAI-style response (`finish_reason: "tool_calls"`, `content: null`).

Run: python3 tests/test_grok_tools_e2e.py
"""
import asyncio
import json
import os
import sys
import threading
import time

API_TOKEN = "paf_test_token_for_grok_tools_e2e_0123456789"
os.environ["PAF_API_TOKEN"] = API_TOKEN  # must be set BEFORE importing vps_server
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "PublicForward", "ForVPS"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402
import vps_server as V  # noqa: E402

HOST, PORT = "127.0.0.1", 8098
BASE_HTTP = f"http://{HOST}:{PORT}"
BASE_WS = f"ws://{HOST}:{PORT}/ws/worker"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}

TOOLS = [{"type": "function", "function": {
    "name": "write_file", "description": "Write a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}}}]

TOOL_CALLS = [{"id": "call_abc123", "type": "function",
               "function": {"name": "write_file",
                            "arguments": json.dumps({"path": "a.txt", "content": 'He said "hi"\nline2'})}}]


def fields_of(task: dict) -> dict:
    """The task fields may sit at the top level or under a sub-key."""
    for key in ("request", "payload", "fields", "task_fields"):
        if isinstance(task.get(key), dict):
            return {**task, **task[key]}
    return task


async def main() -> None:
    async with websockets.connect(BASE_WS) as ws:
        await ws.send(json.dumps({"type": "register", "backend": "grok", "token": "change-me",
                                  "hostname": "w", "max_concurrent": 1, "accounts": ["account1"]}))
        ack = json.loads(await ws.recv())
        assert ack["type"] == "registered", ack

        async with httpx.AsyncClient(base_url=BASE_HTTP, timeout=30, headers=AUTH) as client:
            # ── Turn A: client declares tools, model requests a function ──
            post = asyncio.create_task(client.post("/v1/chat/completions", json={
                "model": "grok",
                "messages": [{"role": "user", "content": "tulis file a.txt"}],
                "tools": TOOLS,
            }))
            task = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            f = fields_of(task)
            assert f.get("tools") and f["tools"][0]["function"]["name"] == "write_file", f"tools not forwarded: {task}"
            assert not f.get("tool_messages"), f
            assert f.get("mode") == "new", f
            tid = task.get("task_id") or task.get("request_id")
            await ws.send(json.dumps({"type": "result", "task_id": tid, "result": {
                "ok": True, "finish_reason": "tool_calls", "text": "", "tool_calls": TOOL_CALLS,
                "account": "account1", "conversation_url": "https://grok.com/c/abc",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}}))
            r = await post
            assert r.status_code == 200, (r.status_code, r.text)
            body = r.json()
            choice = body["choices"][0]
            assert choice["finish_reason"] == "tool_calls", body
            assert choice["message"]["content"] is None, body
            assert choice["message"]["tool_calls"] == TOOL_CALLS, body
            sid = r.headers["x-session-id"]
            print("ok   Turn A: tools forwarded; response is finish_reason=tool_calls with OpenAI tool_calls")

            # ── Turn B: client returns the function result in the same session ──
            post = asyncio.create_task(client.post(
                "/v1/chat/completions",
                headers={**AUTH, "X-Session-ID": sid},
                json={
                    "model": "grok",
                    "messages": [
                        {"role": "user", "content": "tulis file a.txt"},
                        {"role": "assistant", "content": None, "tool_calls": TOOL_CALLS},
                        {"role": "tool", "tool_call_id": "call_abc123", "name": "write_file",
                         "content": json.dumps({"ok": True})},
                    ],
                    "tools": TOOLS,
                }))
            task = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            f = fields_of(task)
            assert f.get("mode") == "continue", f
            tm = f.get("tool_messages")
            assert tm and tm[0]["tool_call_id"] == "call_abc123" and tm[0]["name"] == "write_file" \
                and json.loads(tm[0]["content"]) == {"ok": True}, f"tool_messages not forwarded: {f}"
            assert f.get("tools"), f
            tid = task.get("task_id") or task.get("request_id")
            await ws.send(json.dumps({"type": "result", "task_id": tid, "result": {
                "ok": True, "finish_reason": "stop", "text": "File a.txt sudah ditulis.",
                "account": "account1", "conversation_url": "https://grok.com/c/abc",
                "usage": {"prompt_tokens": 12, "completion_tokens": 6}}}))
            r = await post
            assert r.status_code == 200, (r.status_code, r.text)
            choice = r.json()["choices"][0]
            assert choice["finish_reason"] == "stop" and choice["message"]["content"] == "File a.txt sudah ditulis.", r.text
            print("ok   Turn B: tool_messages forwarded in CONTINUE mode; final answer is finish_reason=stop")

            # ── Plain chat (no tools) is unaffected ──
            post = asyncio.create_task(client.post("/v1/chat/completions", json={
                "model": "grok", "messages": [{"role": "user", "content": "halo"}]}))
            task = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            f = fields_of(task)
            assert not f.get("tools") and not f.get("tool_messages"), f
            tid = task.get("task_id") or task.get("request_id")
            await ws.send(json.dumps({"type": "result", "task_id": tid, "result": {
                "ok": True, "text": "halo juga", "account": "account1",
                "conversation_url": "https://grok.com/c/xyz", "usage": {}}}))
            r = await post
            choice = r.json()["choices"][0]
            assert choice["finish_reason"] == "stop" and choice["message"]["content"] == "halo juga", r.text
            print("ok   plain chat unaffected (no tools / tool_messages sent)")

    print("\nGROK TOOLS E2E PASSED")


if __name__ == "__main__":
    config = uvicorn.Config(V.app, host=HOST, port=PORT, log_level="error")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    asyncio.run(main())
