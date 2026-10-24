# API_USAGE.md — PAF-Model REST API Reference

> Audience: this document is written to be understood **both by humans and by AI
> agents/LLMs** integrating with PAF-Model. It documents the *actual* runtime
> behavior of `PublicForward/ForVPS/vps_server.py` (the only public HTTP
> surface of this project), verified directly against the source code and the
> repo's own tests (`tests/test_http_e2e.py`, `tests/test_vps_smoke.py`,
> `tests/test_api_auth.py`) and
> example clients (`example/chat_deepseek.py`, `example/chat_qwen.py`,
> `example/foto_qwen.py`).
>
> For system architecture, install steps, and how to run the VPS/workers, see
> [`README.md`](./README.md). For a full deep-dive on the Grok backend
> specifically (auth model, the Cloudflare/anti-bot mitigation ladder, and
> how it's tested), see [`GROK_BACKEND.md`](./GROK_BACKEND.md). This
> file only covers **how to call the API**.

---

## 1. What you are talking to

```
YOU (HTTP client) ──POST /v1/chat/completions──▶ vps_server.py (FastAPI, public)
                                                       │  WebSocket /ws/worker
                                                       ▼
                              public.py --backend deepseek|qwen|grok (worker)
                                                       │  Playwright
                                                       ▼
                        chat.deepseek.com / chat.qwen.ai / grok.com
```

`vps_server.py` is a single OpenAI-Chat-Completions-**compatible** gateway
that fronts three different backends (DeepSeek, Qwen, and Grok), each
driven by a real logged-in browser session on a "worker" machine. Your HTTP
request never touches the model provider directly — it is queued and
dispatched to whichever worker process is currently registered and idle for
the backend you asked for.

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

## 3. Authentication — API token required

There are **two, completely separate** tokens in this project:

| Token | Protects | Where it's checked |
|---|---|---|
| **API token** (env `PAF_API_TOKEN`, or the file `.api_token` managed by `manage_token.py`) | The **entire public REST API** — every HTTP endpoint (`/`, `/health`, `/v1/models`, `/v1/chat/completions`, `DELETE /v1/sessions/{id}`, even unknown paths and `/docs`). **One shared token for all users.** | `ApiTokenMiddleware` in `vps_server.py`, before routing/body parsing |
| `PAF_TOKEN` (env `PAF_TOKEN`, default `"change-me"`) | The **WebSocket worker connection** (`/ws/worker`) — which `public.py` processes may register as a backend worker. Has nothing to do with API callers. | `worker_endpoint()` |

### 3.1 Calling the API (clients)

Send the API token on **every** request as a bearer token:

```
Authorization: Bearer <API_TOKEN>
```

```bash
curl http://VPS_HOST:9000/v1/models -H "Authorization: Bearer $PAF_API_TOKEN"
```

Ask the VPS operator for the token. Requests without a valid token are
rejected **before** anything else happens (a malformed body or an unknown
path does not bypass this):

| Situation | Status | Body |
|---|---|---|
| No `Authorization` header | `401` + `WWW-Authenticate: Bearer` | `{"detail": "Missing API token. Send 'Authorization: Bearer <token>'."}` |
| Wrong token / wrong scheme / empty token | `401` + `WWW-Authenticate: Bearer` | `{"detail": "Invalid API token."}` |
| Correct token but **past its expiry** (§3.2) | `401` + `WWW-Authenticate: Bearer` | `{"detail": "API token has expired. Ask the operator for a new token."}` |
| Server has no API token configured | `503` | `{"detail": "API token is not configured on the server."}` |

Notes:
- Only the `Authorization: Bearer` header is accepted (no `X-API-Key`, no
  query-string token). The scheme word is case-insensitive.
- `/docs`, `/redoc` and `/openapi.json` are protected too, so a plain browser
  visit gets a `401` — use `curl`/an HTTP client with the header instead.
- Browser (CORS) preflight `OPTIONS` requests are answered without a token;
  the actual request still needs it, and `401` responses carry CORS headers
  so browser clients can read the error.
- The OpenAI SDK sends exactly this header when you pass the token as
  `api_key`:
  ```python
  from openai import OpenAI
  client = OpenAI(base_url="http://VPS_HOST:9000/v1", api_key="<API_TOKEN>")
  ```
- Treat the token like a password; use HTTPS (reverse proxy such as
  nginx/Caddy) when exposing the VPS on the public internet, because a
  bearer token sent over plain HTTP can be sniffed.

### 3.2 Managing the token (VPS operator)

There is a **single token shared by every user**. The easiest way is the
**live console**: while `vps_server.py` is running in a terminal, just type
commands into that same terminal. Changes apply **immediately, with no
restart**, and are saved to the token file so they survive restarts.

```
$ python vps_server.py --port 9000
[vps] No client API token yet -> every HTTP request is rejected (503) until you create one.
[vps]   Type  token generate  in this console.
[console] Ready. Type 'help' for commands, e.g. 'token generate'.
token generate
New API token created.

  paf_Xk3v...

Active NOW (saved to .../.api_token). Clients send: Authorization: Bearer <token>
```

| Console command | Effect |
|---|---|
| `token generate [expiry]` | Create a random token (`paf_...`). Refuses if an *active* token already exists. |
| `token rotate [expiry]` | Replace it with a new random token. The old one stops working at once. |
| `token set <value> [expiry]` | Use your own token (min 16 chars, no spaces). |
| `token expire <expiry\|never>` | Change the expiry of the **current** token (same token string). |
| `token show` / `token reveal` | Show the token masked / in full, with its expiry status. |
| `token revoke` | Delete the token. Every request gets `503` until you create a new one. |
| `help` | List the commands. |

**Token expiry (optional).** Give a token a lifetime with `<expiry>` = a number
plus a unit — `s`, `m`, `h`, `d`, `w` — e.g. `30m`, `12h`, `7d`, `2w` (max 10
years). Omit it or use `never` for a token that never expires.

```
token generate 7d        # valid for 7 days
token rotate 12h         # new token, valid for 12 hours
token set my-secret-token-123456 2w
token expire 1d          # change the expiry of the current token to 1 day from now
token expire never       # remove the expiry
token show               # Status: expires 2026-10-26 14:03:00 UTC (in 6d 23h)
```

Once the time passes, the gateway rejects the token **automatically** (`401`,
body `API token has expired...`) — no restart, no action needed. Create a new
one with `token rotate [expiry]` (or `token generate`, which is also allowed
once the old token has expired). The expiry is stored in the token file and is
still enforced after a restart. Tokens supplied via the `--api-token` flag or
the `PAF_API_TOKEN` env var never expire (use the console/file for that).

Notes:
- The console is enabled automatically when the server runs in an
  **interactive terminal** (ssh session, `tmux`, `screen`, a foreground
  process). It is **not** available under `nohup`, `systemd`, or
  `docker run -d`, because there is no terminal to type in — use `tmux`/`screen`,
  or the offline methods below. Disable it with `--no-console`.
- Typed tokens stay in your terminal's scrollback; clear it if that matters.

**Offline alternatives** (server not running, or no terminal):

```bash
cd PublicForward/ForVPS
python vps_server.py --generate-token            # create + print + exit
python vps_server.py --generate-token --force    # rotate
python vps_server.py --generate-token --expires 7d       # token valid for 7 days
python manage_token.py generate [--force] [--expires 7d]
python manage_token.py set [TOKEN] [--expires 7d]
python manage_token.py expire 12h|never          # change expiry of the saved token
python manage_token.py show [--reveal] | revoke
```
These edit the same token file; a running server only picks it up after a restart
(or just use the live console instead).

- The token is stored in `PublicForward/ForVPS/.api_token` (permissions
  `600`, git-ignored): line 1 is the token, an optional line 2
  `expires_at=2026-10-26T14:03:00Z` (UTC) is the expiry. Minimum token length
  is 16 characters. A file without an expiry line (e.g. from an older version)
  simply never expires; an unreadable expiry line is treated as *expired*.
- Rotating (`token rotate`) immediately cuts off everyone using the old token,
  so tell your users the new one.
- Resolution order at startup: `--api-token` flag > `PAF_API_TOKEN` env var >
  token file. If the env var is set it overrides the file after a restart (the
  console warns about this). Prefer the file/env over the flag, since CLI args
  are visible in `ps`.
- **Fail closed:** with no token configured, every request gets `503` — never
  open access. In an interactive terminal the server still starts so you can
  type `token generate`; without a terminal (`nohup`, `systemd`, ...) it refuses
  to start (exit code 2) and `start.sh` stops instead of looping.
- A failed attempt is logged as `[vps] auth rejected (...)` with method, path
  and client IP (never the token).

> **Upgrading from a version without client auth:** existing clients (including
> the scripts in `example/`) will start getting `401` until they send the
> token. There is intentionally no "auth off" mode.

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
    { "id": "grok",  "object": "model", "owned_by": "PAF-ai", "x_backend": "grok" },
    { "id": "deepseek(account1)", "object": "model", "owned_by": "PAF-ai",
      "x_backend": "deepseek", "x_account": "account1" },
    { "id": "qwen(account1.json)", "object": "model", "owned_by": "PAF-ai",
      "x_backend": "qwen", "x_account": "account1.json" },
    { "id": "grok(account1)", "object": "model", "owned_by": "PAF-ai",
      "x_backend": "grok", "x_account": "account1" }
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

### 4.5 `DELETE /v1/sessions/{session_id}`
Explicitly deletes a session's server-side conversation mapping. Sessions
have no automatic TTL — this is the only way (besides an operator's manual
worker-console cleanup command) to make the gateway forget one. Full
details, response shape, and client-side TTL guidance in §6.2.1 below.

### 4.6 `WS /ws/worker` (not for API clients)
Internal protocol used by `public.py` workers to register and exchange tasks
with the gateway. You do not call this as an API consumer — documented in
`PublicForward/ForVPS/vps_server.py` / `public_deepseek.py` / `public_qwen.py`
/ `public_grok.py` for anyone extending the worker side.

---

## 5. `POST /v1/chat/completions` — Request

Content-Type: `application/json`.

### 5.1 Full request schema

| Field | Type | Default | Notes |
|---|---|---|---|
| `model` | string | `"deepseek"` | **Required in practice.** See §6 — selects backend (+ optional account). |
| `messages` | array of `{role, content, name?, tool_calls?, tool_call_id?}` | — | **Required.** Standard OpenAI chat message list. `content` may be a string, a list of `{type:"text", text:...}` parts, or `null` (for `tool_calls` assistant messages). Only the **last `user`** message text is actually sent as the prompt to DeepSeek; for Qwen the same extraction applies (see §5.2). `system`/`developer` messages are concatenated and forwarded to **all three backends** (see §5.2). `tool` role messages carry tool-execution results back in (see §9). |
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

`system`-role messages (**all backends: DeepSeek, Qwen, Grok**): every
`system` **and** `developer` message (string content, or the joined `text`
parts of a content list) is concatenated with `\n\n` by the gateway and
forwarded to the worker as `system_prompt`. The worker embeds it in the
`[SYSTEM CONTEXT]` block of the prompt it types into the chat UI — on **every**
turn (new, continue and tool-result turns), because the browser conversation
does not remember it as a real system role. If the request has no `system`
message, nothing extra is added. The system prompt is per-request: it is not
stored on the session, so resend it with each request (as with any OpenAI-style
client) if you want it to keep applying.

---

## 6. Model routing & sessions

### 6.1 The `model` field (backend + account routing)

Regex enforced by the gateway: `^(deepseek|qwen|grok)(?:\(([^)]+)\))?$`

| `model` value | Meaning |
|---|---|
| `"deepseek"` | Any available DeepSeek worker/account |
| `"qwen"` | Any available Qwen worker/account |
| `"grok"` | Any available Grok worker/account |
| `"deepseek(account1)"` | Specifically the DeepSeek worker holding account `account1` |
| `"qwen(account1.json)"` | Specifically the Qwen worker holding cookie file `account1.json` |
| `"grok(account1)"` | Specifically the Grok worker holding account `account1` |
| anything else (e.g. `"gpt-4"`, `"deepseek-chat"`) | **400 Bad Request** |

**Use exactly `"deepseek"`, `"qwen"`, or `"grok"`** (or the
account-parenthesized form) — do not use OpenAI-style model names like
`deepseek-chat` / `deepseek-reasoner` / `qwen-max` / `gpt-4`, they will be
rejected with a 400. This is fixed and verified by
`tests/test_vps_smoke.py::test_resolve_backend_and_account` and
`tests/test_http_e2e.py` for all three backends.

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
can't set headers — the header wins if both are present.

### 6.2.1 Sessions have NO automatic TTL — you manage their lifetime

**Design change:** sessions no longer expire automatically. A session
created via `X-Session-ID` lives **forever** (both in the worker's memory
and on disk under `dataSession/`) until one of these happens:

1. **You delete it explicitly** — `DELETE /v1/sessions/{session_id}` (new
   endpoint, see below).
2. **The operator runs a manual cleanup** on a worker's console:
   `cleanup sessions [max_age_seconds]` (DeepSeek) /
   `cleanupsessions [max_age_seconds]` (Qwen). This is opt-in and
   operator-triggered only — nothing calls it automatically.

If you want TTL-like behaviour (e.g. "forget this conversation after 1 hour
of inactivity"), **implement it on the client side**:

- Track your own `last_used` timestamp per `session_id` you hold.
- When it exceeds whatever age you consider "stale", call
  `DELETE /v1/sessions/{session_id}` yourself (or simply stop reusing that
  `X-Session-ID` — nothing forces you to; the next call without it, or with
  a session_id the server doesn't recognise, always starts a fresh
  conversation, see below).
- There's no server-side "TTL" setting exposed via this REST API on purpose:
  every deployment/integration has different idle-conversation semantics,
  so the gateway leaves that policy entirely up to the caller.

#### `DELETE /v1/sessions/{session_id}`

Deletes the server-side session → conversation mapping. The request is
broadcast to every connected worker (all three backends); whichever one (if
any) actually holds that `session_id` removes it from its store.

```bash
curl -X DELETE http://<VPS_HOST>:<PORT>/v1/sessions/sess-421a9c7e1b2c3d4f \
  -H "Authorization: Bearer $PAF_API_TOKEN"
```

Response:
```json
{"session_id": "sess-421a9c7e1b2c3d4f", "deleted": true, "workers_checked": 1}
```

- `deleted: false` just means the session wasn't found anywhere (already
  deleted, never existed, or the worker that held it is offline) — **not**
  an error. The call is idempotent; deleting twice is harmless.
- After a successful delete, sending that same `X-Session-ID` again on
  `/v1/chat/completions` does **not** fail — it is automatically treated as
  a brand-new conversation (`mode` silently falls back to `"new"`, surfaced
  via `x_meta.mode_fallback: true`), exactly like it already does today for
  any other unknown/never-seen `session_id`.
- Like every other REST endpoint, this requires the API token
  (`Authorization: Bearer <token>`, see §3).

### 6.3 Response headers

Every successful response includes:

| Header | Always present? | Meaning |
|---|---|---|
| `X-Session-ID` | Yes | The session id to reuse for the next turn. |
| `X-Backend` | Yes | `"deepseek"`, `"qwen"`, or `"grok"` — confirms which backend actually served you. |
| `X-Account-Name` | Only if the worker reported one | The account/cookie-file that served the request. |
| `X-Conversation-URL` | Only if the worker reported one | Direct URL to the underlying chat.deepseek.com / chat.qwen.ai / grok.com conversation. |

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

### Grok
**Not supported in v1.** Grok has no model picker / think_mode toggle —
every request uses the Grok UI's default model. Any `think_mode` value
sent with `model: "grok"` is silently ignored (no error).

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
- For **Grok**, attachments are uploaded via the hidden file input
  (`set_input_files`) and the worker waits for the attachment preview chip
  to appear before sending the prompt; a per-file timeout produces a clear
  error (no silent partial upload).
- `mime_type` is taken as given — the example above passes `image/png` while
  reading a `.jpg` file, and it still worked in testing; the field is mostly
  advisory for the client/browser upload step, but pass the correct value for
  your file to be safe.

---

## 9. Tool / function calling (deepseek, qwen, grok)

All three backends resolve tool calls end-to-end with the **same protocol**
(`scrapers/tool_protocol.py`). From a client's point of view nothing
backend-specific is needed: you send OpenAI `tools`, you get OpenAI
`tool_calls` back.

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

Because the backends are **browser automation**, not native function-calling
APIs, tool calling is emulated through the prompt. Every request is wrapped in a
`[SYSTEM CONTEXT]` / `[USER REQUEST]` envelope, and:

- **Plain chat (no `tools`)** — the model answers in **plain text**. There is no
  JSON envelope to validate, so long answers, code, quotes and backslashes can
  no longer produce "invalid JSON" failures, retries or account rotations.
- **With `tools`** — the model answers in plain text *unless* it needs a
  function, in which case it writes a marker block with **raw, unescaped**
  argument values:

  ````
  @@PAF-CALL:write_file@@
  @@PAF-ARG:path@@
  main.py
  @@PAF-ARG:content@@
  print("raw", 'text')
  @@PAF-END@@
  ````

  The worker parses the block and builds the OpenAI `tool_calls` JSON in code
  (`json.dumps`), so escaping is always correct. Argument values are converted
  to the type your `parameters` schema declares (`integer`, `number`,
  `boolean`, `object`, `array`); `function.arguments` is always a JSON
  **string** (OpenAI spec) on every backend.
- **Self-healing** — if the model emits a function-call block that cannot be
  parsed (unterminated, unknown function name, …) or an empty reply, the worker
  sends one short correction in the **same** conversation (up to 2×) before
  failing the request. Ordinary plain-text answers are never retried for format.
- **Backward compatible** — conversations that started under the old JSON
  protocol (`{"status":"success"|"tool_calls", ...}`) are still understood.

Notes:
- The `[SYSTEM CONTEXT]` is sent on **every** turn (it is short for plain chat),
  because clients may change `tools` between requests.
- A client-supplied `system` message is appended to `[SYSTEM CONTEXT]` on
  **all backends** — DeepSeek, Qwen and Grok (see §5.2).
- `max_tokens` is passed to the model as an advisory length hint, not enforced.
- Resend `tools` on every request, as in the OpenAI API. On the Turn-B request
  the worker can still parse a function call without them, but it can then
  neither validate function names nor coerce argument types.

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
as `tool_messages` (DeepSeek and Grok) or leaves them in `messages` for the Qwen
worker's own tool-result branch (`scrape_with_tool_result`). The model may
answer (`finish_reason: "stop"`) or chain another function call
(`finish_reason: "tool_calls"` again).

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

## 11. Response — success shape (all three backends, normalized)

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
  `backend == "deepseek"` (Grok and Qwen never set these — Grok has no
  model picker in v1).
- Grok's result shape is **identical** to DeepSeek's
  (`ok`/`text`/`account`/`conversation_url`/`mode`) — the gateway normalizes
  both through the same code path (`else: # deepseek | grok` branch in
  `vps_server.py`), so everything in this section applies to `grok`
  exactly as written for `deepseek`.
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
| `400` | `model` doesn't match `^(deepseek\|qwen\|grok)(?:\(...\))?$` | `{"detail": "Unknown model: '...'. Use 'deepseek', 'qwen', 'grok', or '<backend>(<account_id>)' ..."}` |
| `401` | **Missing/invalid/expired API token** (§3) — carries a `WWW-Authenticate: Bearer` header. Sent before the request is processed | `{"detail": "Missing API token. ..."}`, `{"detail": "Invalid API token."}` or `{"detail": "API token has expired. ..."}` |
| `401` | *(different cause)* Grok worker has no valid session for the account (grok.com is SSO-only — there is no automated login fallback; run `login_grok.py` or `import_grok_cookies.py` on the worker host) | `{"detail": "<auth error message>"}` (no `WWW-Authenticate` header — that is how you tell it apart from the API-token 401 above) — see [`GROK_BACKEND.md`](./GROK_BACKEND.md#6-cloudflare--anti-bot--the-mitigation-ladder) if this coincides with a Cloudflare/anti-bot challenge |
| `404` | Grok CONTINUE request whose `session_id` has no saved `conversation_url` (session was deleted, or never existed) | `{"detail": "Session tidak ditemukan atau conversation_url kosong — client harus membuat session baru"}` — start a new session (omit `X-Session-ID`) |
| `503` | The gateway has no API token configured (operator error) — every request is refused | `{"detail": "API token is not configured on the server."}` |
| `422` | Request body fails Pydantic validation (e.g. missing `messages`) | Standard FastAPI validation error body |
| `429` | Backend reported a rate limit / usage cap (all backends) | `{"detail": "<rate limit message>"}` — back off and retry later, or target a different account |
| `500` | Worker reported `ok: false` (DeepSeek / Grok) / generic exception in the handler | `{"detail": "<error message from worker>"}` |
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

> Every example below assumes `export PAF_API_TOKEN=<your API token>` (§3).

### 13.1 curl — simple DeepSeek chat (new session)
```bash
curl -X POST http://VPS_HOST:9000/v1/chat/completions \
  -H "Authorization: Bearer $PAF_API_TOKEN" \
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
  -H "Authorization: Bearer $PAF_API_TOKEN" \
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
  -H "Authorization: Bearer $PAF_API_TOKEN" \
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
  -H "Authorization: Bearer $PAF_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "qwen(account1.json)",
        "think_mode": "thinking",
        "messages": [{"role": "user", "content": "Ringkas artikel ini"}]
      }'
```

### 13.5 curl — Grok chat (new session, then continue)
```bash
# New session — omit X-Session-ID
curl -i -X POST http://VPS_HOST:9000/v1/chat/completions \
  -H "Authorization: Bearer $PAF_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "grok",
        "messages": [{"role": "user", "content": "Explain the CAP theorem simply"}]
      }'
# → read X-Session-ID and X-Conversation-URL from the response headers

# Continue — reuse the X-Session-ID from above
curl -X POST http://VPS_HOST:9000/v1/chat/completions \
  -H "Authorization: Bearer $PAF_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: sess-7f3a9c1d0e2b4f6a" \
  -d '{
        "model": "grok",
        "messages": [{"role": "user", "content": "Now give a one-sentence summary"}]
      }'
```
No `think_mode`/`model_tab`/`deep_think`/`web_search`/`task_type` fields for
Grok — it has no model picker or think-mode toggle (see §7 and §14). Function
calling with `tools` works the same as on the other backends (§9).

### 13.6 Python — minimal client
```python
import os
import requests

BASE_URL = "http://VPS_HOST:9000"
API_TOKEN = os.environ["PAF_API_TOKEN"]
session = {"id": None}

def ask(message: str) -> str:
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
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

### 13.7 Ready-made reference clients in this repo
All of them read the API token from `--api-token` or the `PAF_API_TOKEN`
environment variable and send it as a bearer token (§3).

- `example/chat_deepseek.py` — full interactive CLI for the DeepSeek path
  (`/new`, `/status`, `/think <mode>` commands); this file's own docstring
  explicitly says it mirrors the flow documented here.
- `example/chat_qwen.py` — interactive CLI for the Qwen path, with
  `--stream` flag for local pretty-printing (note: still non-streaming at the
  HTTP layer, see §1).
- `example/foto_qwen.py` — minimal image-attachment example (§8).
- `example/newChat_qwen.py` — forcing a brand-new Qwen session.
- There is no dedicated `example/chat_grok.py` yet in v1 — use §13.5/13.6
  above (a bare `"model": "grok"` request works with the same generic
  Python client in §13.6, just change the `model` value).

---

## 14. Field cheat-sheet for AI agents

If you are an LLM/agent constructing requests programmatically, the minimum
viable request is:

```json
{"model": "deepseek", "messages": [{"role": "user", "content": "<prompt>"}]}
```

Decision rules:
- **Always** send `Authorization: Bearer <API_TOKEN>` — without it every call
  fails with `401` (§3).
- **New conversation** → omit `X-Session-ID` header entirely.
- **Continue conversation** → send back the `X-Session-ID` you received last
  time; don't resend prior `messages`, only send the newest `user` turn (plus
  any `tool` result messages if you're in a tool-calling loop).
- **Want DeepSeek reasoning** → `"think_mode": "thinking"` (or
  `"deep_think": true` for finer control).
- **Want DeepSeek web search** → `"web_search": true`.
- **Want a specific backend account** → `"model": "deepseek(account1)"` or
  set `"preferred_account": "account1"` with `"model": "deepseek"` (works
  the same way for `grok(account1)` / `qwen(account1.json)`).
- **Sending an image** → `attachments: [{filename, data (base64), mime_type}]`,
  and for DeepSeek also set `"model_tab": "vision"` (Grok and Qwen need no
  extra field for image attachments).
- **Calling a tool** → declare `tools`, read `finish_reason == "tool_calls"`
  and `message.tool_calls`, execute locally, then POST back a `tool` message
  with the same `X-Session-ID`. Works the same on `deepseek`, `qwen` and
  `grok` (§9); resend `tools` on every request.
- **Do not** send `model_tab`/`deep_think`/`web_search`/`task_type`/
  `think_mode` to `grok` — Grok has no such controls and ignores all of these
  (no error, just no effect). Same rule as before for not sending a
  backend's fields to a *different* backend that doesn't own them.
- **Always** treat `usage.*` as approximate, `detail` (not `error.message`)
  as the error field, and `x_meta.mode_fallback` as a "session may have reset"
  signal.

---

## 15. Where this doc's claims come from (traceability)

For anyone auditing this document against the code:
- Endpoint behavior, schema, headers, error codes → `PublicForward/ForVPS/vps_server.py`
- API-token auth → `PublicForward/ForVPS/vps_server.py` (`ApiTokenMiddleware`),
  `PublicForward/ForVPS/paf_auth.py`, `PublicForward/ForVPS/manage_token.py`;
  verified by `tests/test_api_auth.py`
- DeepSeek worker task execution / result shape → `public_deepseek.py`, `scrapers/deepseek_scraper.py`
- Qwen worker task execution / result shape → `public_qwen.py`, `scrapers/qwen_scraper.py`
- Prompt wrapping, function-call protocol and reply parsing (all three backends) →
  `scrapers/tool_protocol.py`; verified by `tests/test_tool_protocol.py`,
  `tests/test_protocol_scrapers.py` and `tests/test_grok_tools_e2e.py`
- Grok worker task execution / result shape → `public_grok.py`,
  `scrapers/grok_scraper.py`, `scrapers/base_grok.py`,
  `browser_pool_grok.py`
- Grok architecture, auth model, and Cloudflare/anti-bot mitigations →
  [`GROK_BACKEND.md`](./GROK_BACKEND.md) (dedicated deep-dive)
- Config values (`think_mode` labels, tabs, defaults) → `config/deepseek.py`, `config/qwen.py`, `config/grok.py`
- Confirmed request/response wire examples → `tests/test_http_e2e.py`, `tests/test_vps_smoke.py`
- Grok-specific test verification → `tests/_manual_*.py` (see
  [`GROK_BACKEND.md` §7](./GROK_BACKEND.md#7-testing--how-this-passes))
- Client-side usage patterns → `example/chat_deepseek.py`, `example/chat_qwen.py`, `example/foto_qwen.py`