"""
E2E test for the new session-deletion feature:
  - DELETE /v1/sessions/{session_id} on the VPS gateway
  - broadcasts a "delete_session" message to every connected worker
  - the worker replies "session_deleted" with found=True/False
  - the VPS aggregates and returns {"session_id","deleted","workers_checked"}
  - the VPS's own routing-affinity hint (_session_worker) is cleared too

Uses a real uvicorn instance + a real websockets worker client (same pattern
as tests/test_http_e2e.py).

Run: python3 tests/test_delete_session_e2e.py
"""
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

HOST, PORT = "127.0.0.1", 8098
BASE_HTTP = f"http://{HOST}:{PORT}"
BASE_WS = f"ws://{HOST}:{PORT}/ws/worker"


async def case_no_workers_connected():
    """DELETE with zero connected workers -> deleted=False, workers_checked=0."""
    async with httpx.AsyncClient(base_url=BASE_HTTP, timeout=10) as client:
        r = await client.delete("/v1/sessions/no-such-session")
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body == {
        "session_id": "no-such-session", "deleted": False, "workers_checked": 0,
    }, body
    print("[case_no_workers_connected] OK ->", body)


async def case_worker_connected_but_unknown_session():
    """Worker connected, but has never heard of this session_id -> deleted=False."""
    async with websockets.connect(BASE_WS) as ws:
        await ws.send(json.dumps({
            "type": "register", "backend": "deepseek", "token": "change-me",
            "hostname": "w1", "max_concurrent": 2, "accounts": ["account1"],
        }))
        ack = json.loads(await ws.recv())
        assert ack["type"] == "registered", ack

        async def worker_reply_loop():
            msg = json.loads(await ws.recv())
            assert msg["type"] == "delete_session", msg
            assert msg["session_id"] == "unknown-sess"
            await ws.send(json.dumps({
                "type": "session_deleted",
                "request_id": msg["request_id"],
                "session_id": msg["session_id"],
                "found": False,
            }))

        reply_task = asyncio.create_task(worker_reply_loop())
        async with httpx.AsyncClient(base_url=BASE_HTTP, timeout=10) as client:
            r = await client.delete("/v1/sessions/unknown-sess")
        await reply_task

    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["session_id"] == "unknown-sess"
    assert body["deleted"] is False
    assert body["workers_checked"] == 1
    print("[case_worker_connected_but_unknown_session] OK ->", body)


async def case_worker_finds_and_deletes_session():
    """Worker replies found=True -> aggregated deleted=True; affinity map cleared."""
    async with websockets.connect(BASE_WS) as ws:
        await ws.send(json.dumps({
            "type": "register", "backend": "deepseek", "token": "change-me",
            "hostname": "w2", "max_concurrent": 2, "accounts": ["account1"],
        }))
        ack = json.loads(await ws.recv())
        assert ack["type"] == "registered", ack
        worker_id = ack["worker_id"]

        # Seed a routing-affinity entry, as a real "continue" dispatch would.
        V.worker_mgr._session_worker["sess-real"] = worker_id
        assert "sess-real" in V.worker_mgr._session_worker

        async def worker_reply_loop():
            msg = json.loads(await ws.recv())
            assert msg["type"] == "delete_session", msg
            assert msg["session_id"] == "sess-real"
            await ws.send(json.dumps({
                "type": "session_deleted",
                "request_id": msg["request_id"],
                "session_id": msg["session_id"],
                "found": True,
            }))

        reply_task = asyncio.create_task(worker_reply_loop())
        async with httpx.AsyncClient(base_url=BASE_HTTP, timeout=10) as client:
            r = await client.delete("/v1/sessions/sess-real")
        await reply_task

    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body == {"session_id": "sess-real", "deleted": True, "workers_checked": 1}, body
    # The VPS-side routing-affinity hint must be cleared on delete.
    assert "sess-real" not in V.worker_mgr._session_worker
    print("[case_worker_finds_and_deletes_session] OK ->", body)


async def case_delete_is_idempotent():
    """Deleting an already-deleted / never-existed session again is not an error."""
    async with httpx.AsyncClient(base_url=BASE_HTTP, timeout=10) as client:
        r1 = await client.delete("/v1/sessions/sess-real")  # no workers connected now
    assert r1.status_code == 200
    print("[case_delete_is_idempotent] OK ->", r1.json())


async def main():
    await case_no_workers_connected()
    await case_worker_connected_but_unknown_session()
    await case_worker_finds_and_deletes_session()
    await case_delete_is_idempotent()
    print("\nALL DELETE-SESSION E2E TESTS PASSED")


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
