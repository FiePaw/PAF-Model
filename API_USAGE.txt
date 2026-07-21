# API_USAGE.md — PAF-Model REST API Reference

> Audience: this document is written to be understood **both by humans and by AI
> agents/LLMs** integrating with PAF-Model. It documents the *actual* runtime
> behavior of `PublicForward/ForVPS/vps_server.py` (the only public HTTP
> surface of this project), verified directly against the source code and the
> repo's own tests (`tests/test_http_e2e.py`, `tests/test_vps_smoke.py`) and
> example clients (`example/chat_deepseek.py`, `example/chat_qwen.py`,
> `example/foto_qwen.py`).
>
> For system architecture, install steps, and how to run the VPS/workers, see
> [`README.md`](./README.md). This file only covers **how to call the API**.

---

## 1. What you are talking to

```
YOU (HTTP client) ──POST /v1/chat/completions──▶ vps_server.py (FastAPI, public)
                                                       │  WebSocket /ws/worker
                                                       ▼
                                     public.py --backend deepseek|qwen (worker)
                                                       │  Playwright
                                                       ▼
                                        chat.deepseek.com  /  chat.qwen.ai
```

`vps_server.py` is a single OpenAI-Chat-Completions-**compatible** gateway
that fronts two different backends (DeepSeek and Qwen), each driven by a real
logged-in browser session on a "worker" machine. Your HTTP request never
touches the model provider directly — it is queued and dispatched to whichever
worker process is currently registered and idle for the backend you asked for.

There is **no streaming support**. `stream: true` is accepted in the request
body but is currently **ignored** by the gateway (the whole reply is returned
in one JSON response). Do not rely on SSE/chunked output.

---

## 2. Base URL & running instances

Default: `http://<VPS_HOST>:<PORT>` — port defaults to `8000` when you run
`python vps_server.py` with no `--port`, but deployments commonly use `9000`
(see `PublicForward/ForVPS/start.sh` and the example clients). **Always
confirm the port with whoever runs the VPS** — it is a CLI/env choice, not
fixed by the code.

There is no path prefix/versioning beyond `/v1/...` — this is intentionally
OpenAI-shaped so existing OpenAI SDK tooling mostly works if you point
`base_url` at this server, with the caveats in this document.

---

## 3. Authentication — read this carefully

There are **two, completely separate** notions of "token" in this project and
they are easy to confuse:

| Token | Protects | Where it's checked |
|---|---|---|
| `PAF_TOKEN` / `AUTH_TOKEN` (env `PAF_TOKEN`, default `"change-me"`) | The **WebSocket worker connection** (`/ws/worker`) — i.e. which `public.py` processes are allowed to register as a backend worker | Inside `worker_endpoint()`, comparing the `token` field in the worker's `register` message (or `?token=` query param) |
| — none — | The **public REST API** (`/v1/chat/completions`, `/v1/models`, `/health`, `/`) | **Not checked at all.** There is no `Authorization` header, no API key, no bearer-token check anywhere in the REST handlers. |

**Practical implication:** as shipped, anyone who can reach the VPS's HTTP
port can call `/v1/chat/completions` with no credentials. If you are exposing
this publicly, put it behind your own auth layer (reverse proxy, API gateway,
IP allowlist, etc.) — the app itself does not provide client-facing auth.
`PAF_TOKEN` only gates whether a *worker* (the machine with logged-in browser
sessions) may attach to the gateway, not whether a *caller* may use it.

---

## 4. Endpoints

### 4.1 `GET /`
Root status/info endpoint. Returns service name, version, and worker stats
(same shape as `/health`'s `workers` field). Useful for a quick "is anything
connected" check.

### 4.2 `GET /health`
```json
{
  "status": "healthy",            // "healthy" if ≥1 worker connected, else "no_workers"
  "workers": {
    "total_workers": 2,
    "workers": [
      {
        "worker_id": "worker-win1-001",
        "backend": "deepseek",
        "accounts": ["account1", "account2"],
        "max_concurrent": 2,
        "in_flight": 0,
        "connected_at": 1730700000.123,
        "uptime_seconds": 42.1
      }
    ],
    "total_accounts": 2,
    "busy_slots": 0
  },
  "timestamp": 1730700042.5
}
```

### 4.3 `GET /v1/models`
Lists the model ids you may pass to `/v1/chat/completions`. Content is
**dynamic** — it reflects whichever workers are currently connected, so call
this before hard-coding a model id, especially per-account ids.

```json
{
  "object": "list",
  "data": [
    { "id": "deepseek", "object": "model", "owned_by": "PAF-ai", "x_backend": "deepseek" },
    { "id": "qwen",     "object": "model", "owned_by": "PAF-ai", "x_backend": "qwen" },
    { "id": "deepseek(account1)", "object": "model", "owned_by": "PAF-ai",
      "x_backend": "deepseek", "x_account": "account1" },
    { "id": "qwen(account1.json)", "object": "model", "owned_by": "PAF-ai",
      "x_backend": "qwen", "x_account": "account1.json" }
  ]
}
```
- A **bare backend id** (`"deepseek"` / `"qwen"`) always means "route me to
  any available account of that backend" and is listed even with zero
  connected accounts if a worker for that backend is connected.
- A **per-account id** `"<backend>(<account_id>)"` targets one specific
  logged-in account/cookie file. If no worker for a backend is connected at
  all, that backend won't appear here — but you can still try it in
  `/v1/chat/completions`, you'll just get a `504` (see §8).

### 4.4 `POST /v1/chat/completions`
The only endpoint that does real work. Details in §5–§7 below.

### 4.5 `WS /ws/worker` (not for API clients)
Internal protocol used by `public.py` workers to register and exchange tasks
with the gateway. You do not call this as an API consumer — documented in
`PublicForward/ForVPS/vps_server.py` / `public_deepseek.py` / `public_qwen.py`
for anyone extending the worker side.

---

## 5. `POST /v1/chat/completions` — Request

Content-Type: `application/json`.

### 5.1 Full request schema

| Field | Type | Default | Notes |
|---|---|---|---|
| `model` | string | `"deepseek"` | **Required in practice.** See §6 — selects backend (+ optional account). |
| `messages` | array of `{role, content, name?, tool_calls?, tool_call_id?}` | — | **Required.** Standard OpenAI chat message list. `content` may be a string, a list of `{type:"text", text:...}` parts, or `null` (for `tool_calls` assistant messages). Only the **last `user`** message text is actually sent as the prompt to DeepSeek; for Qwen the same extraction applies (see §5.2). `system` messages are concatenated and forwarded (DeepSeek only, see below). `tool` role messages carry tool-execution results back in (see §9). |
| `stream` | bool | `false` | Accepted but **ignored** — no streaming is implemented. |
| `temperature` | float | `null` | Accepted for OpenAI-schema compatibility; **not forwarded** to either backend scraper (these are browser-automation backends, not raw model APIs — sampling parameters aren't controllable). |
| `max_tokens` | int | `null` | Forwarded to the worker (`task_fields["max_tokens"]`) but browser-based scraping has no hard token cap enforcement; treat as advisory. |
| `tools` | array of `{type:"function", function:{name, description?, parameters?}}` | `null` | OpenAI function-calling tool definitions. See §9. |
| `tool_choice` | string | `"auto"` | Only meaningful for Qwen; forwarded as-is when `tools` is set. DeepSeek ignores it. |
| `attachments` | array of `{filename, data, mime_type}` | `null` | File/image attachments, `data` is base64 (raw or a full `data:<mime>;base64,...` URI both work in practice per `example/foto_qwen.py`). See §8. |
| `deep_think` | bool | `null` | **DeepSeek-only.** Enables "DeepThink" (reasoning) mode. Overridden by/overrides `think_mode` — see §7. |
| `web_search` | bool | `null` | **DeepSeek-only.** Enables live web search for the turn. |
| `model_tab` | string | `null` | **DeepSeek-only.** One of `"instant"` \| `"expert"` \| `"vision"` — which top-level UI tab to use. Defaults to `"instant"` if unresolved. |
| `think_mode` | string | `null` | Cross-backend convenience field — semantics differ by backend, see §7. |
| `session_id` | string | `null` | **Legacy/fallback.** Prefer the `X-Session-ID` **header** (§6.2) — the header takes priority if both are set. |
| `mode` | string | `null` | **Ignored by the gateway.** `new` vs `continue` is derived purely from whether a session id is present (header or body). Sending `mode` has no effect on gateway routing (kept in the schema for backward compatibility only). |
| `preferred_account` | string | `null` | Pin the request to a specific account/cookie-file **by name**, as an alternative to `model: "<backend>(<account>)"`. If both are given, the account named inside `model` wins. |
| `task_type` | string | `"chat"` | **Qwen-only.** One of `"chat"` \| `"create_image"` \| `"create_video"` \| `"web_search"`. See §10. |

### 5.2 How the prompt is extracted

`last_user_message()` walks `messages` in reverse and returns the **first
`user`-role message found from the end** (string content, or the joined
`text` parts if content is a list of parts). Everything before that in the
conversation is **not** replayed to the model on this call — multi-turn
context is instead carried by the **browser session itself** (the underlying
chat.deepseek.com / chat.qwen.ai conversation), addressed via
`X-Session-ID` + `mode: continue` (see §6.2), not by resending prior messages.

**Practical rule:** for a fresh question, send one `user` message. For a
follow-up in the same conversation, send the new `X-Session-ID` you got back
from turn 1 and just send the new `user` message — do not resend the whole
history, it will be ignored beyond the last user turn.

`system`-role messages: on **DeepSeek only**, all `system` message contents
are concatenated with `\n\n` and forwarded as `system_prompt`. Qwen does not
receive a distinct system prompt field from `messages` today (the example CLI
`example/chat_qwen.py --system "..."` wires this through a different, simpler
path — check that script if you need Qwen system prompts).

---

## 6. Model routing & sessions

### 6.1 The `model` field (backend + account routing)

Regex enforced by the gateway: `^(deepseek|qwen)(?:\(([^)]+)\))?$`

| `model` value | Meaning |
|---|---|
| `"deepseek"` | Any available DeepSeek worker/account |
| `"qwen"` | Any available Qwen worker/account |
| `"deepseek(account1)"` | Specifically the DeepSeek worker holding account `account1` |
| `"qwen(account1.json)"` | Specifically the Qwen worker holding cookie file `account1.json` |
| anything else (e.g. `"gpt-4"`, `"deepseek-chat"`\*) | **400 Bad Request** |

\* Note: the repo's own end-to-end test (`tests/test_http_e2e.py`) posts
`model: "deepseek-chat"` and `model: "qwen"` and gets `200` — that test talks
to a **stub worker** it registers itself, so it never actually exercises the
regex with `"deepseek-chat"` failing... in fact `"deepseek-chat"` **does not
match** `^(deepseek|qwen)(?:\(...\))?$` and would raise 400 against the real
`resolve_backend_and_account()`. **Use exactly `"deepseek"` or `"qwen"`** (or
the account-parenthesized form) — do not use OpenAI-style model names like
`deepseek-chat` / `deepseek-reasoner` / `qwen-max`, they will be rejected.

Call `GET /v1/models` to discover live per-account ids rather than guessing
account names.

### 6.2 Session continuity — `X-Session-ID`

This is the standard way to continue a conversation across multiple
`/v1/chat/completions` calls:

- **Turn 1 (new conversation):** don't send `X-Session-ID` (or send an empty
  one). The gateway generates `sess-<16 hex chars>` and returns it in the
  `X-Session-ID` response header **and** in `x_meta.session_id`.
- **Turn 2+ (continue):** send that same value back as the `X-Session-ID`
  **request header** on your next call. The gateway then:
  1. Sets `mode = "continue"` automatically (you don't need to send `mode` —
     it's ignored anyway, see §5.1).
  2. Routes the request back to the **same worker** that handled turn 1
     (session affinity, `WorkerManager._session_worker`), so the same
     underlying browser/conversation is reused.
  3. If that worker is no longer available for that backend, dispatch falls
     back to picking any available worker of the same backend (the original
     session pin is only honored if `mode == "continue"` **and** the pinned
     worker is currently registered).

Body field `session_id` is accepted as a **fallback only** for clients that
can't set headers — the header wins if both are present. There is no
mechanism to end/delete a session explicitly via the API; sessions expire
server-side per worker TTL settings (`--session-ttl` on `public.py`, not
configurable via this REST API).

### 6.3 Response headers

Every successful response includes:

| Header | Always present? | Meaning |
|---|---|---|
| `X-Session-ID` | Yes | The session id to reuse for the next turn. |
| `X-Backend` | Yes | `"deepseek"` or `"qwen"` — confirms which backend actually served you. |
| `X-Account-Name` | Only if the worker reported one | The account/cookie-file that served the request. |
| `X-Conversation-URL` | Only if the worker reported one | Direct URL to the underlying chat.deepseek.com / chat.qwen.ai conversation. |

---

## 7. `think_mode` — backend-specific semantics

`think_mode` is a **single convenience field** that means different things
per backend. It does **not** validate against a fixed enum in the request
schema (it's a free string) — invalid values are simply ignored per backend
rules below.

### DeepSeek
Resolved via a fixed alias table (`THINK_MODE_ALIASES` in `vps_server.py`)
into `(model_tab, deep_think, web_search)`:

| `think_mode` | `model_tab` | `deep_think` | `web_search` |
|---|---|---|---|
| `auto` / `instant` / `fast` | `instant` | `false` | `false` |
| `thinking` / `expert` / `reasoning` | `expert` | `true` | `false` |
| `vision` | `vision` | `false` | `false` |

Precedence: explicit `deep_think` / `web_search` / `model_tab` fields in the
request body **override** whatever `think_mode` resolves to. If neither is
set at all, `model_tab` defaults to `"instant"`.

### Qwen
`think_mode` is passed straight through, unresolved, to the Qwen worker as
one of `"auto"` \| `"thinking"` \| `"fast"` (matches `QWEN_CONFIG` labels in
`config/qwen.py`). There is no alias table on the Qwen side — send the exact
string the worker/scraper expects.

---

## 8. Attachments (images / files)

```json
{
  "model": "qwen",
  "messages": [{"role": "user", "content": "Apa yang ada di gambar ini?"}],
  "attachments": [
    {
      "filename": "foto.jpg",
      "data": "<base64 bytes, with or without the data: URI prefix>",
      "mime_type": "image/png"
    }
  ]
}
```
- Multiple attachments per request are supported (it's a list).
- For **DeepSeek**, image input requires `model_tab: "vision"` (or
  `think_mode: "vision"`) — plain `instant`/`expert` tabs do not accept image
  attachments in the underlying UI.
- For **Qwen**, no special tab/mode is required for images (per
  `example/foto_qwen.py`).
- `mime_type` is taken as given — the example above passes `image/png` while
  reading a `.jpg` file, and it still worked in testing; the field is mostly
  advisory for the client/browser upload step, but pass the correct value for
  your file to be safe.

---

## 9. Tool / function calling (both backends)

Send `tools` in the OpenAI function-calling shape:

```json
{
  "model": "deepseek",
  "messages": [{"role": "user", "content": "What's the weather in Jakarta?"}],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "parameters": {
          "type": "object",
          "properties": {"city": {"type": "string"}},
          "required": ["city"]
        }
      }
    }
  ]
}
```

Because both backends are **browser automation**, not native function-calling
APIs, tool calling is emulated via a strict JSON-only system prompt injected
by the scraper (see `scrapers/deepseek_scraper.py`): the model is told it is
"a pure JSON API endpoint" and must answer with exactly one of two JSON
shapes — a `tool_calls` request or a final `success` answer — which the
scraper parses back into OpenAI's schema for you.

### Turn A — model requests a tool call
Response:
```json
{
  "choices": [{
    "index": 0,
    "finish_reason": "tool_calls",
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {"name": "get_weather", "arguments": "{\"city\":\"Jakarta\"}"}
      }]
    }
  }],
  "x_meta": {"session_id": "sess-...", "backend": "deepseek", "mode": "new", ...}
}
```
`finish_reason` is set to `"tool_calls"` automatically whenever the worker
returns any `tool_calls` — you don't need to inspect a separate flag.

### Turn B — you send the tool result back
Continue the **same session** (`X-Session-ID` header from Turn A) and append
a `tool` role message with the result, plus (optionally) a new `user` message
to prompt the model to continue:
```json
{
  "model": "deepseek",
  "messages": [
    {"role": "user", "content": "What's the weather in Jakarta?"},
    {"role": "assistant", "tool_calls": [{"id": "call_abc123", "type": "function",
      "function": {"name": "get_weather", "arguments": "{\"city\":\"Jakarta\"}"}}]},
    {"role": "tool", "tool_call_id": "call_abc123", "name": "get_weather",
     "content": "{\"tempC\": 29, \"condition\": \"cloudy\"}"}
  ]
}
```
(Header `X-Session-ID: sess-...` from Turn A.) The gateway detects `mode ==
"continue"` (because a session id is present) and forwards `tool` messages
as `tool_messages` (DeepSeek) or leaves them in `messages` for the Qwen
worker's own tool-result branch (`scrape_with_tool_result`). The final answer
comes back as a normal `finish_reason: "stop"` message.

---

## 10. Qwen-only: `task_type` (media generation & search)

| `task_type` | Effect |
|---|---|
| `"chat"` (default) | Normal text chat. |
| `"create_image"` | Drives Qwen's "Create Image" button with your prompt. |
| `"create_video"` | Drives Qwen's "Create Video" button with your prompt. |
| `"web_search"` | Drives Qwen's "Web search" toggle for this turn. |

**⚠️ Known limitation (verified in code):** the Qwen worker (`public_qwen.py`)
returns generated media URLs under a top-level `urls` field
(`{"success": true, "response": "...", "urls": [...], "task_type": "create_image", ...}`),
but `vps_server.py`'s `/v1/chat/completions` handler **does not read or
surface `urls` or `task_type`** anywhere in the HTTP response it builds —
only `response`, `usage`, `tool_calls`/`finish_reason`, `cookie_file`, and
`conversation_url` are extracted from the Qwen worker's result. In practice,
today, **image/video URLs generated via `task_type` are dropped before
reaching the HTTP client.** If you need this feature, either (a) patch
`vps_server.py`'s qwen branch to fold `result.get("urls")` into `x_meta`, or
(b) fetch the conversation via `X-Conversation-URL` and inspect it
separately. Treat `task_type` as best-effort / not production-ready until
this is fixed upstream.

---

## 11. Response — success shape (both backends, normalized)

```json
{
  "id": "chatcmpl-3fa1...",
  "object": "chat.completion",
  "created": 1730700000,
  "model": "deepseek",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "The answer is ..."},
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 48,
    "total_tokens": 60
  },
  "x_meta": {
    "session_id": "sess-7f3a9c1d0e2b4f6a",
    "backend": "deepseek",
    "mode": "new",
    "mode_fallback": false,
    "account": "account1",
    "conversation_url": "https://chat.deepseek.com/c/abc123",
    "response_time": 4.82,
    "timestamp": 1730700004.82,
    "model_tab": "instant",
    "deep_think": false,
    "web_search": false
  }
}
```

Notes on fields you can rely on:
- `usage.*_tokens` are **estimated** (`len(text) // 4`, min 1) whenever the
  worker itself doesn't report real counts — treat as approximate, not
  billing-grade.
- `x_meta.model_tab` / `deep_think` / `web_search` only appear when
  `backend == "deepseek"`.
- `x_meta.mode_fallback: true` (DeepSeek only) signals the browser session
  behind your `X-Session-ID` was lost/expired and the worker silently started
  a **new** conversation instead of continuing — check this if a "continue"
  call seems to have forgotten context, and consider treating it as a signal
  to reset your client-side session id.
- Any additional keys under a worker's `x_metadata` are merged into `x_meta`
  too, but **never override** the keys the gateway already set
  (`x_meta.setdefault(...)`).

---

## 12. Error handling

| HTTP status | When | Body shape |
|---|---|---|
| `400` | `model` doesn't match `^(deepseek\|qwen)(?:\(...\))?$` | `{"detail": "Unknown model: '...'. Use 'deepseek', 'qwen', or '<backend>(<account_id>)' ..."}` |
| `422` | Request body fails Pydantic validation (e.g. missing `messages`) | Standard FastAPI validation error body |
| `500` | Worker reported `ok: false` (DeepSeek) / generic exception in the handler | `{"detail": "<error message from worker>"}` |
| `502` | Worker reported `success: false` (Qwen) | `{"detail": "<error message from worker>"}` |
| `504` | No worker of the requested backend became available within `WORKER_WAIT_TIMEOUT` (default 60s — env `WORKER_WAIT_TIMEOUT`) | `{"detail": "No available '<backend>' worker within timeout"}` |

There is also an overall **response wait timeout** of `PAF_REQUEST_TIMEOUT`
(default **330 seconds**, env-configurable) — if a worker accepted the task
but never returns a result in time, the connection will hang up to that long
before erroring. Set your HTTP client timeout accordingly (≥ 330s, or match
whatever `PAF_REQUEST_TIMEOUT` was configured to on your VPS).

All errors from this endpoint use FastAPI's standard `HTTPException` shape:
`{"detail": "<message>"}` — check `detail`, not `error.message` (this is
**not** identical to OpenAI's `{"error": {...}}` error envelope, despite the
success response being OpenAI-shaped).

---

## 13. Quick-start examples

### 13.1 curl — simple DeepSeek chat (new session)
```bash
curl -X POST http://VPS_HOST:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
        "model": "deepseek",
        "messages": [{"role": "user", "content": "Explain async/await in Python"}]
      }'
```
Read `X-Session-ID` from the response headers to continue the chat.

### 13.2 curl — continue that session
```bash
curl -X POST http://VPS_HOST:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: sess-7f3a9c1d0e2b4f6a" \
  -d '{
        "model": "deepseek",
        "messages": [{"role": "user", "content": "Now summarize that in one sentence"}]
      }'
```

### 13.3 curl — DeepSeek Expert + DeepThink + web search
```bash
curl -X POST http://VPS_HOST:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
        "model": "deepseek",
        "think_mode": "thinking",
        "web_search": true,
        "messages": [{"role": "user", "content": "Latest developments in fusion energy"}]
      }'
```

### 13.4 curl — Qwen chat targeting a specific account
```bash
curl -X POST http://VPS_HOST:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
        "model": "qwen(account1.json)",
        "think_mode": "thinking",
        "messages": [{"role": "user", "content": "Ringkas artikel ini"}]
      }'
```

### 13.5 Python — minimal client
```python
import requests

BASE_URL = "http://VPS_HOST:9000"
session = {"id": None}

def ask(message: str) -> str:
    headers = {}
    if session["id"]:
        headers["X-Session-ID"] = session["id"]

    r = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        json={"model": "deepseek", "messages": [{"role": "user", "content": message}]},
        headers=headers,
        timeout=330,
    )
    r.raise_for_status()
    session["id"] = r.headers.get("X-Session-ID", session["id"])
    return r.json()["choices"][0]["message"]["content"]

print(ask("Hi, who are you?"))
print(ask("What did I just ask you?"))   # continues the same session
```

### 13.6 Ready-made reference clients in this repo
- `example/chat_deepseek.py` — full interactive CLI for the DeepSeek path
  (`/new`, `/status`, `/think <mode>` commands); this file's own docstring
  explicitly says it mirrors the flow documented here.
- `example/chat_qwen.py` — interactive CLI for the Qwen path, with
  `--stream` flag for local pretty-printing (note: still non-streaming at the
  HTTP layer, see §1).
- `example/foto_qwen.py` — minimal image-attachment example (§8).
- `example/newChat_qwen.py` — forcing a brand-new Qwen session.

---

## 14. Field cheat-sheet for AI agents

If you are an LLM/agent constructing requests programmatically, the minimum
viable request is:

```json
{"model": "deepseek", "messages": [{"role": "user", "content": "<prompt>"}]}
```

Decision rules:
- **New conversation** → omit `X-Session-ID` header entirely.
- **Continue conversation** → send back the `X-Session-ID` you received last
  time; don't resend prior `messages`, only send the newest `user` turn (plus
  any `tool` result messages if you're in a tool-calling loop).
- **Want DeepSeek reasoning** → `"think_mode": "thinking"` (or
  `"deep_think": true` for finer control).
- **Want DeepSeek web search** → `"web_search": true`.
- **Want a specific backend account** → `"model": "deepseek(account1)"` or
  set `"preferred_account": "account1"` with `"model": "deepseek"`.
- **Sending an image** → `attachments: [{filename, data (base64), mime_type}]`,
  and for DeepSeek also set `"model_tab": "vision"`.
- **Calling a tool** → declare `tools`, read `finish_reason == "tool_calls"`
  and `message.tool_calls`, execute locally, then POST back a `tool` message
  with the same `X-Session-ID`.
- **Do not** send `model_tab`/`deep_think`/`web_search`/`task_type` to the
  backend that doesn't own them (they're backend-specific extensions, listed
  per-backend in §5.1) — the other backend simply ignores fields it doesn't
  understand, but keep requests clean.
- **Always** treat `usage.*` as approximate, `detail` (not `error.message`)
  as the error field, and `x_meta.mode_fallback` as a "session may have reset"
  signal.

---

## 15. Where this doc's claims come from (traceability)

For anyone auditing this document against the code:
- Endpoint behavior, schema, headers, error codes → `PublicForward/ForVPS/vps_server.py`
- DeepSeek worker task execution / result shape → `public_deepseek.py`, `scrapers/deepseek_scraper.py`
- Qwen worker task execution / result shape → `public_qwen.py`, `scrapers/qwen_scraper.py`
- Config values (`think_mode` labels, tabs, defaults) → `config/deepseek.py`, `config/qwen.py`
- Confirmed request/response wire examples → `tests/test_http_e2e.py`, `tests/test_vps_smoke.py`
- Client-side usage patterns → `example/chat_deepseek.py`, `example/chat_qwen.py`, `example/foto_qwen.py`
