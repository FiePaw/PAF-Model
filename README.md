# PAF-Model

Unified repo merging **PAF-ModelDeepSeek** + **PAF-ModelQwen** + a **ChatGPT**
backend into one project with a single OpenAI-compatible VPS gateway. The
`model` field in each request selects the backend (`deepseek`, `qwen`, or
`chatgpt`); the `X-Session-ID` header drives multi-turn continuation for
**all three** backends.

## Architecture

This merge keeps each backend's battle-tested scraper/pool code **intact** as
parallel modules (safe merge — see "Design notes" below), while unifying the
config, shared utils, and the VPS gateway. ChatGPT is the third backend,
added on top of this pattern. **For a full deep-dive on the ChatGPT backend**
(architecture, login flow, the Cloudflare Turnstile mitigation ladder, and
how every piece is tested), **see [`CHATGPT_BACKEND.md`](./CHATGPT_BACKEND.md)**.

```
PAF-Model/
├── config/                      # unified config package (re-exports everything)
│   ├── common.py                #   shared paths, browser, rotation, output, logging
│   ├── deepseek.py              #   DEEPSEEK_CONFIG, AUTH_CONFIG, JSON_API_CONFIG
│   ├── qwen.py                  #   QWEN_CONFIG
│   ├── chatgpt.py               #   CHATGPT_CONFIG, CHATGPT_AUTH_CONFIG
│   └── __init__.py              #   `from config import X` still works everywhere
│
├── scrapers/
│   ├── utils.py                 # MERGED helpers (get_logger + setup_logger, etc.)
│   ├── base_deepseek.py         # DeepSeek base (account-name + email/password auth)
│   ├── base_qwen.py             # Qwen base (cookie-file auth)
│   ├── base_chatgpt.py          # ChatGPT base (persistent profile, driver selection, CDP attach)
│   ├── deepseek_scraper.py      # DeepSeekScraper(BaseAIChatScraper[deepseek])
│   ├── qwen_scraper.py          # QwenScraper(BaseAIChatScraper[qwen])
│   └── chatgpt_scraper.py       # ChatGPTScraper(BaseAIChatScraper[chatgpt])
│
├── browser_pool_deepseek.py     # DeepSeek pre-warmed pool (preferred_account)
├── browser_pool_qwen.py         # Qwen pre-warmed pool (cookie files)
├── browser_pool_chatgpt.py      # ChatGPT pre-warmed pool (1 slot/account)
│
├── public.py                    # unified worker entrypoint → dispatches by --backend
├── public_deepseek.py           # DeepSeek worker loop (registers backend="deepseek")
├── public_qwen.py               # Qwen worker loop     (registers backend="qwen")
├── public_chatgpt.py            # ChatGPT worker loop  (registers backend="chatgpt")
│
├── login_chatgpt.py            # ChatGPT: one-time MANUAL login helper (Turnstile workaround #1)
├── import_chatgpt_cookies.py   # ChatGPT: seed a profile from manually exported cookies (#2)
├── start_chatgpt_chrome.py     # ChatGPT: start a real Chrome for CDP attach (#3, strongest)
│
├── PublicForward/ForVPS/
│   ├── vps_server.py            # UNIFIED gateway: model routing + X-Session-ID
│   └── start.sh
│
├── cookies/  profiles/  dataSession/  logs/  debug/  output/
├── example/                     # chat_deepseek.py, chat_qwen.py, ...
├── tests/                       # offline functional/e2e tests for the gateway
├── requirements.txt             # worker side
├── requirements_api.txt         # VPS side
├── CHATGPT_BACKEND.md           # deep-dive: architecture, login flow, Turnstile mitigations, tests
└── .env.example
```

## How routing works

```
CLIENT → VPS   POST /v1/chat/completions
               Header: X-Session-ID: sess-abc123   (optional — CONTINUE only)
               Body:   { "model": "deepseek(account1)" | "qwen(account1)" | "chatgpt(account1)",
                         "messages": [...], "think_mode": "...", "tools": [...],
                         "attachments": [...] }

VPS (vps_server.py):
  1. resolve_backend_and_account(model) → ("deepseek"|"qwen"|"chatgpt", account_id|None)
  2. session_id = X-Session-ID header (else generate)   → mode = continue|new
  3. dispatch(backend=…, preferred_account=…) → pick a worker whose backend
     matches, filtered to the requested account when one was given
  4. Session affinity: a CONTINUE request routes back to the same worker
  5. Send the task in that worker's native wire protocol:
       deepseek → {"type":"task","task_id",  "request": {...}}
       chatgpt  → {"type":"task","task_id",  "request": {...}}   (same shape as deepseek)
       qwen     → {"type":"task","request_id","payload": {...}}

WORKER (public.py --backend X):  registers with "backend": "X"; only receives
  tasks for its backend. Runs DeepSeekScraper / QwenScraper / ChatGPTScraper
  as appropriate.

VPS → CLIENT   OpenAI chat.completion + x_meta.backend + headers
               X-Session-ID, X-Backend, X-Account-Name, X-Conversation-URL
```

Model ids accepted: a bare backend name — `deepseek`, `qwen`, or `chatgpt`
(routes to any available account for that backend) — or an account-specific
id in the form `<backend>(<account_id>)`, e.g. `deepseek(account1)`,
`qwen(account1)`, `chatgpt(account1)`. Call `GET /v1/models` to see the exact
ids for accounts currently connected via a worker.

`think_mode`:
- **deepseek** → resolved to `(model_tab, deep_think, web_search)` via aliases.
- **qwen** → passed through as-is (`auto`|`thinking`|`fast`).
- **chatgpt** → not supported in v1 — chat-only, default model in the UI.

## Running

On the **VPS**:

```bash
pip install -r requirements_api.txt
export PAF_TOKEN=your-secret            # optional; "change-me" disables auth
python PublicForward/ForVPS/vps_server.py --port 9000
```

On each **worker host** (run two processes, one per backend):

```bash
pip install -r requirements.txt
playwright install chromium

python public.py --backend deepseek --vps ws://VPS_IP:9000/ws/worker --workers 2 --token your-secret
python public.py --backend qwen     --vps ws://VPS_IP:9000/ws/worker --workers 2 --token your-secret
python public.py --backend chatgpt  --vps ws://VPS_IP:9000/ws/worker --workers 2 --token your-secret
```

Any flags after `--backend` are passed straight to the selected backend worker.
See backend-specific flags with e.g. `python public.py --backend qwen --help`.

## Auth / accounts

- **DeepSeek**: `cookies/auth.json` (email+password per account) → persistent
  profile per account name in `profiles/`. Env fallback: `DEEPSEEK_EMAIL` /
  `DEEPSEEK_PASSWORD`.
- **Qwen**: cookie files `cookies/account1.json`, `cookies/account2.json`, …
  → one profile per cookie-file stem.
- **ChatGPT**: `cookies/authchatgpt.json` (email+password per account, same
  format as `auth.json`) → persistent profile per account name in
  `profiles/chatgpt/<account>/`. Env fallback: `CHATGPT_EMAIL` /
  `CHATGPT_PASSWORD`. Login is automatic (email → "Continue with password" →
  password — no SSO, no email verification code). Example
  `cookies/authchatgpt.json`:
  ```json
  [
    {"name": "account1", "email": "you@yourmail.com", "password": "secret"}
  ]
  ```

  **Cloudflare Turnstile on `auth.openai.com` / `chatgpt.com` can block any
  of the steps above.** This is a known, actively-mitigated risk — see
  **[`CHATGPT_BACKEND.md`](./CHATGPT_BACKEND.md#cloudflare-turnstile-the-mitigation-ladder)**
  for the full story. Short version — try these in order, from easiest to
  strongest:

  | # | Workaround | Command | When to use |
  |---|---|---|---|
  | 1 | `playwright-stealth` (default, automatic) | — | Always on by default when the plain `playwright` driver is used |
  | 2 | Real Chrome channel | `CHATGPT_BROWSER_CHANNEL=chrome` | Bundled Chromium gets challenged |
  | 3 | Patchright driver (patched Playwright, no CDP leaks) | `pip install patchright && python -m patchright install chromium` (auto-detected) | Automated login itself gets challenged |
  | 4 | One-time manual login | `python login_chatgpt.py --account account1` | You're willing to solve the challenge by hand once |
  | 5 | Import cookies from your everyday browser | `python import_chatgpt_cookies.py --account account1 --cookies <export.json>` | Zero automation during login — most reliable |
  | 6 | Attach to an already-running real Chrome via CDP | `python start_chatgpt_chrome.py --account account1` then `CHATGPT_CDP_ATTACH=1` | Strongest — the browser never gets launched by automation at all |

  Whichever workaround gets you logged in, the **headless worker afterwards
  behaves identically** — `ensure_authenticated()` always checks whether
  the session is already valid first, and only ever falls back to the
  automated (Cloudflare-prone) login flow if it isn't.

The VPS accepts a worker's token from the register body (DeepSeek worker) or the
`?token=` query param (Qwen worker); enforcement is skipped when `PAF_TOKEN` is
the default `change-me`.

## Tests (offline, no browser / no accounts needed)

```bash
python tests/test_vps_smoke.py     # WorkerManager: routing, envelopes, result shapes
python tests/test_http_e2e.py      # real uvicorn + WS worker + httpx POST (all 3 backends)
```

ChatGPT-specific logic (login-state detection, popup handling, driver
selection, cookie import, stealth) is covered by a set of ad-hoc scripts
under `tests/_manual_*.py` that use HTML stubs / a real headless browser but
never touch the real ChatGPT service — see
**[`CHATGPT_BACKEND.md`](./CHATGPT_BACKEND.md#testing--how-this-passes)**
for what each one verifies and how to run the full suite.

## Design notes (deviation from the original merge plan)

The original plan proposed a single `base_scraper.py` and single
`browser_pool.py`. In practice the two backends had diverged structurally
(DeepSeek uses an **account-name + email/password** model; Qwen uses a
**cookie-file** model), so forcing them into one class hierarchy would have
required a risky, untested rewrite of the ~2000-line Qwen scraper.

Instead each backend keeps its own proven base/pool as **parallel modules**,
and `public.py --backend` selects the right one. This achieves every functional
goal of the plan — one repo, one `vps_server.py` with model routing +
`X-Session-ID` standard, workers registering with a `backend` field, and
per-backend session affinity — with minimal risk to the existing code.

Migration notes (session state, cookies/profiles, `config` import compat, etc.)
from the plan still apply; sessions from the old separate repos are not
compatible and `dataSession/` can be emptied.
