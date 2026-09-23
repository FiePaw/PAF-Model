"""Isolated functional test for the unified vps_server (no browsers/network).

Extended (Tahap G1) to cover the chatgpt backend:
  - resolve_backend_and_account() routing incl. chatgpt / chatgpt(account1)
  - dispatch envelope for chatgpt == deepseek-style (task_id + request)
  - chatgpt result shape normalises the same way deepseek's does

Note: this file previously referenced a `V.resolve_backend(...)` helper that
no longer exists in vps_server.py (the current gateway exposes
`resolve_backend_and_account(model) -> (backend, account_id)` instead). The
test below is synchronised to the current API.
"""
import asyncio
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "PublicForward", "ForVPS"))

import vps_server as V


class FakeWS:
    """Captures envelopes the VPS sends to a worker; lets the test push results."""
    def __init__(self):
        self.sent = []
    async def send_json(self, obj):
        self.sent.append(obj)


def test_resolve_backend_and_account():
    assert V.resolve_backend_and_account("deepseek") == ("deepseek", None)
    assert V.resolve_backend_and_account("qwen") == ("qwen", None)
    assert V.resolve_backend_and_account("chatgpt") == ("chatgpt", None)
    assert V.resolve_backend_and_account("deepseek(account1)") == ("deepseek", "account1")
    assert V.resolve_backend_and_account("qwen(account1)") == ("qwen", "account1")
    assert V.resolve_backend_and_account("chatgpt(account1)") == ("chatgpt", "account1")
    try:
        V.resolve_backend_and_account("gpt-4")
        assert False, "should raise"
    except V.HTTPException as e:
        assert e.status_code == 400
        assert "chatgpt" in e.detail
    print("resolve_backend_and_account: OK")


async def _run_backend(backend, model_result):
    mgr = V.WorkerManager()
    ws = FakeWS()
    wid = await mgr.register(ws, "host", 4, ["account1"], backend)
    assert mgr.workers[wid].backend == backend

    # pick_worker filters by backend
    assert await mgr._pick_worker(backend) == wid
    other = "qwen" if backend != "qwen" else "deepseek"
    assert await mgr._pick_worker(other) is None

    task_fields = {"session_id": "sess-x", "messages": [{"role": "user", "content": "hi"}]}

    async def feed():
        # wait until VPS has sent the task envelope, then push a result
        for _ in range(200):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        env = ws.sent[-1]
        tid = env.get("task_id") or env.get("request_id")
        if backend == "qwen":
            msg = {"type": "result", "request_id": tid, "data": model_result}
        else:  # deepseek | chatgpt (same deepseek-style envelope/result shape)
            msg = {"type": "result", "task_id": tid, "result": model_result}
        await mgr.handle_result(msg)

    feeder = asyncio.create_task(feed())
    result = await mgr.dispatch(backend=backend, task_fields=task_fields,
                                mode="new", session_id="sess-x")
    await feeder

    env = ws.sent[-1]
    if backend == "qwen":
        assert env["type"] == "task" and "request_id" in env and env["payload"] is task_fields, env
    else:  # deepseek | chatgpt
        assert env["type"] == "task" and "task_id" in env and env["request"] is task_fields, env
    payload_key = "payload" if backend == "qwen" else "request"
    print(f"{backend}: envelope OK ->",
          {k: (v if k != payload_key else "<fields>") for k, v in env.items()})
    return result


def test_dispatch_all_backends():
    # DeepSeek result shape
    ds_result = {"ok": True, "text": "hello from ds", "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
                 "account": "account1", "conversation_url": "https://chat.deepseek.com/x", "mode": "new"}
    r = asyncio.run(_run_backend("deepseek", ds_result))
    assert r["ok"] and r["text"] == "hello from ds"

    # Qwen result shape
    qw_result = {"success": True, "response": "halo dari qwen", "usage": {"prompt_tokens": 2, "completion_tokens": 5},
                 "cookie_file": "account1.json", "conversation_url": "https://chat.qwen.ai/y", "finish_reason": "stop"}
    r2 = asyncio.run(_run_backend("qwen", qw_result))
    assert r2["success"] and r2["response"] == "halo dari qwen"

    # ChatGPT result shape (deepseek-style: ok/text/account/conversation_url)
    gpt_result = {"ok": True, "text": "hello from chatgpt",
                  "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                  "account": "account1", "conversation_url": "https://chatgpt.com/c/abc", "mode": "new"}
    r3 = asyncio.run(_run_backend("chatgpt", gpt_result))
    assert r3["ok"] and r3["text"] == "hello from chatgpt"

    print("dispatch_all_backends: OK")


def test_stats_and_accounts():
    async def run():
        mgr = V.WorkerManager()
        await mgr.register(FakeWS(), "h1", 4, ["account1", "account2"], "deepseek")
        await mgr.register(FakeWS(), "h2", 2, [{"account": "qacc1"}, {"cookie_file": "qacc2.json"}], "qwen")
        await mgr.register(FakeWS(), "h3", 1, ["gptacc1"], "chatgpt")
        stats = mgr.get_stats()
        accts = mgr.list_all_accounts()
        return stats, accts
    stats, accts = asyncio.run(run())
    assert stats["total_workers"] == 3
    backends = {a["backend"] for a in accts}
    assert backends == {"deepseek", "qwen", "chatgpt"}, accts
    ids = {a["id"] for a in accts}
    assert "qacc1" in ids and "qacc2.json" in ids and "account1" in ids and "gptacc1" in ids, accts
    print("stats_and_accounts: OK ->", accts)


if __name__ == "__main__":
    test_resolve_backend_and_account()
    test_dispatch_all_backends()
    test_stats_and_accounts()
    print("\nALL VPS SMOKE TESTS PASSED")
