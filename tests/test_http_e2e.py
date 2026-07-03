"""True loopback integration test: uvicorn + real WS worker + httpx POST."""
import asyncio
import json
import sys
import threading
import time
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "PublicForward", "ForVPS"))

import httpx
import uvicorn
import websockets
import vps_server as V

HOST, PORT = "127.0.0.1", 8099
BASE_HTTP = f"http://{HOST}:{PORT}"
BASE_WS = f"ws://{HOST}:{PORT}/ws/worker"


async def run_case(backend, model, register_msg, make_result, expect_text, use_query_token=False):
    q = "?token=change-me" if use_query_token else ""
    async with websockets.connect(BASE_WS + q) as ws:
        await ws.send(json.dumps(register_msg))
        ack = json.loads(await ws.recv())
        assert ack["type"] == "registered", ack

        async with httpx.AsyncClient(base_url=BASE_HTTP, timeout=30) as client:
            post = asyncio.create_task(client.post("/v1/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
            }))
            task = json.loads(await ws.recv())          # VPS → worker task
            assert task["type"] == "task", task
            tid = task.get("task_id") or task.get("request_id")
            await ws.send(json.dumps(make_result(tid)))  # worker → VPS result
            r = await post

            # /v1/models while the worker is still connected
            models = (await client.get("/v1/models")).json()

    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["choices"][0]["message"]["content"] == expect_text, body
    assert body["x_meta"]["backend"] == backend, body["x_meta"]
    assert r.headers.get("x-backend") == backend
    assert r.headers.get("x-session-id")

    ids = {m["id"] for m in models["data"]}
    assert backend in ids, models
    print(f"[{backend}] e2e OK — content={body['choices'][0]['message']['content']!r} "
          f"session={r.headers['x-session-id'][:14]} backend_hdr={r.headers['x-backend']}")


async def main():
    await run_case(
        backend="deepseek", model="deepseek-chat",
        register_msg={"type": "register", "backend": "deepseek", "token": "change-me",
                      "hostname": "win1", "max_concurrent": 2, "accounts": ["account1"]},
        make_result=lambda tid: {"type": "result", "task_id": tid,
                                 "result": {"ok": True, "text": "pong-ds", "account": "account1",
                                            "conversation_url": "https://chat.deepseek.com/c/1",
                                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}},
        expect_text="pong-ds",
    )
    await run_case(
        backend="qwen", model="qwen",
        register_msg={"type": "register", "backend": "qwen",
                      "max_concurrent": 2, "accounts": [{"cookie_file": "account1.json"}]},
        make_result=lambda tid: {"type": "result", "request_id": tid,
                                 "data": {"success": True, "response": "pong-qwen",
                                          "cookie_file": "account1.json",
                                          "conversation_url": "https://chat.qwen.ai/c/2",
                                          "finish_reason": "stop",
                                          "usage": {"prompt_tokens": 1, "completion_tokens": 1}}},
        expect_text="pong-qwen",
        use_query_token=True,
    )
    print("\nALL HTTP E2E TESTS PASSED")


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
