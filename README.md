# PAF-Model

Unified repo merging **PAF-ModelDeepSeek** + **PAF-ModelQwen** into one project
with a single OpenAI-compatible VPS gateway. The `model` field in each request
selects the backend (`deepseek` or `qwen`); the `X-Session-ID` header drives
multi-turn continuation for **both** backends.

## Architecture

This merge keeps each backend's battle-tested scraper/pool code **intact** as
parallel modules (safe merge — see "Design notes" below), while unifying the
config, shared utils, and the VPS gateway.

```
PAF-Model/
├── config/                      # unified config package (re-exports everything)
│   ├── common.py                #   shared paths, browser, rotation, output, logging
│   ├── deepseek.py              #   DEEPSEEK_CONFIG, AUTH_CONFIG, JSON_API_CONFIG
│   ├── qwen.py                  #   QWEN_CONFIG
│   └── __init__.py              #   `from config import X` still works everywhere
│
├── scrapers/
│   ├── utils.py                 # MERGED helpers (get_logger + setup_logger, etc.)
│   ├── base_deepseek.py         # DeepSeek base (account-name + email/password auth)
│   ├── base_qwen.py             # Qwen base (cookie-file auth)
│   ├── deepseek_scraper.py      # DeepSeekScraper(BaseAIChatScraper[deepseek])
│   └── qwen_scraper.py          # QwenScraper(BaseAIChatScraper[qwen])
│
├── browser_pool_deepseek.py     # DeepSeek pre-warmed pool (preferred_account)
├── browser_pool_qwen.py         # Qwen pre-warmed pool (cookie files)
│
├── public.py                    # unified worker entrypoint → dispatches by --backend
├── public_deepseek.py           # DeepSeek worker loop (registers backend="deepseek")
├── public_qwen.py               # Qwen worker loop     (registers backend="qwen")
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
└── .env.example
```

## How routing works

```
CLIENT → VPS   POST /v1/chat/completions
               Header: X-Session-ID: sess-abc123   (optional — CONTINUE only)
               Body:   { "model": "deepseek(account1)" | "qwen(account1)", "messages": [...],
                         "think_mode": "...", "tools": [...], "attachments": [...] }

VPS (vps_server.py):
  1. resolve_backend_and_account(model) → ("deepseek"|"qwen", account_id|None)
  2. session_id = X-Session-ID header (else generate)   → mode = continue|new
  3. dispatch(backend=…, preferred_account=…) → pick a worker whose backend
     matches, filtered to the requested account when one was given
  4. Session affinity: a CONTINUE request routes back to the same worker
  5. Send the task in that worker's native wire protocol:
       deepseek → {"type":"task","task_id",  "request": {...}}
       qwen     → {"type":"task","request_id","payload": {...}}

WORKER (public.py --backend X):  registers with "backend": "X"; only receives
  tasks for its backend. Runs DeepSeekScraper / QwenScraper as before.

VPS → CLIENT   OpenAI chat.completion + x_meta.backend + headers
               X-Session-ID, X-Backend, X-Account-Name, X-Conversation-URL
```

Model ids accepted: a bare backend name — `deepseek` or `qwen` (routes to
any available account for that backend) — or an account-specific id in the
form `<backend>(<account_id>)`, e.g. `deepseek(account1)`, `qwen(account1)`.
Call `GET /v1/models` to see the exact ids for accounts currently connected
via a worker.

`think_mode`:
- **deepseek** → resolved to `(model_tab, deep_think, web_search)` via aliases.
- **qwen** → passed through as-is (`auto`|`thinking`|`fast`).

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
```

Any flags after `--backend` are passed straight to the selected backend worker.
See backend-specific flags with e.g. `python public.py --backend qwen --help`.

## Auth / accounts

- **DeepSeek**: `cookies/auth.json` (email+password per account) → persistent
  profile per account name in `profiles/`. Env fallback: `DEEPSEEK_EMAIL` /
  `DEEPSEEK_PASSWORD`.
- **Qwen**: cookie files `cookies/account1.json`, `cookies/account2.json`, …
  → one profile per cookie-file stem.

The VPS accepts a worker's token from the register body (DeepSeek worker) or the
`?token=` query param (Qwen worker); enforcement is skipped when `PAF_TOKEN` is
the default `change-me`.

## Tests (offline, no browser / no accounts needed)

```bash
python tests/test_vps_smoke.py     # WorkerManager: routing, envelopes, result shapes
python tests/test_http_e2e.py      # real uvicorn + WS worker + httpx POST (both backends)
```

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
