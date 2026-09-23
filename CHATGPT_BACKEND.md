# ChatGPT Backend — Deep Dive

> Companion reference to `README.md` / `CHANGELOG.md`. This document explains
> the ChatGPT backend end-to-end: how it's structured, how every piece
> works, the full story of the Cloudflare Turnstile problem and how it was
> solved, and exactly how the implementation is verified to work ("how it
> passes"). Written for anyone who needs to operate, debug, or extend this
> backend without re-reading the whole implementation history.

---

## 1. What this backend is

ChatGPT is the **third backend** in PAF-Model, alongside DeepSeek and Qwen.
It plugs into the exact same unified gateway (`vps_server.py`) using the
same `model` routing convention (`chatgpt` / `chatgpt(account1)`), the same
`X-Session-ID` continuation header, and the same OpenAI-compatible response
shape the other two backends already produce.

**v1 scope (deliberately limited, by design):**

| Capability | Status |
|---|---|
| Chat (new + continue) | ✅ |
| Code block extraction | ✅ |
| Attachments (images/files) | ✅ |
| Multi-account, rate-limit rotation | ✅ |
| Session persistence (no TTL, disk-backed) | ✅ |
| `think_mode` / model picker | ❌ not supported — ChatGPT UI's default model only |
| Tool/function calling execution | ❌ accepted in the request, not executed against the UI (v1.1) |
| SSO login (Google/Microsoft/Apple) | ❌ email+password only |
| Streaming responses | ❌ `stream: true` is ignored, same as DeepSeek |

---

## 2. File structure

```
config/chatgpt.py              CHATGPT_CONFIG (selectors, timeouts, browser_channel,
                                cdp_attach) + CHATGPT_AUTH_CONFIG (auth file, env
                                fallback, login_wait, fail_loud_on_captcha)

scrapers/base_chatgpt.py       BaseAIChatScraper: browser lifecycle (persistent
                                profile, driver selection, CDP attach), credential
                                resolution, account rotation, login-state detection,
                                generic response-wait polling, debug screenshots

scrapers/chatgpt_scraper.py    ChatGPTScraper(BaseAIChatScraper): the concrete
                                login flow (popup + same-tab handling, "Continue
                                with password"), chat flow (new/continue), DOM +
                                fallback response extraction, attachment upload

browser_pool_chatgpt.py        BrowserPool: 1 pre-warmed slot per account, acquire/
                                release, rate-limit rotation + retry-once, runtime
                                add_account, status/diagnostics

public_chatgpt.py              LocalWorker + SessionStore: WebSocket worker loop,
                                envelope handling, per-session locking, interactive
                                console (list accounts / add account / status /
                                cleanup sessions)

login_chatgpt.py               Turnstile workaround #1 (manual login helper) —
                                visible browser, same persistent profile, never
                                auto-closes

import_chatgpt_cookies.py      Turnstile workaround #2 (cookie import) — seed a
                                profile from cookies exported out of a normal
                                browser; zero automation touches the login flow

start_chatgpt_chrome.py        Turnstile workaround #3 (CDP attach, strongest) —
                                starts a REAL Chrome with --remote-debugging-port
                                bound to the same profile; the worker then attaches
                                to it instead of launching its own browser

tests/_manual_*.py             Ad-hoc verification scripts for all of the above
                                (see §7)
```

### How it plugs into the existing gateway (minimal touch)

| File | Change |
|---|---|
| `config/__init__.py` | Re-exports `CHATGPT_CONFIG`, `CHATGPT_AUTH_CONFIG` |
| `public.py` | `--backend` choices += `chatgpt` |
| `.env.example` | `CHATGPT_EMAIL` / `CHATGPT_PASSWORD` / `CHATGPT_BROWSER_CHANNEL` / `CHATGPT_BROWSER_DRIVER` / `CHATGPT_CDP_ATTACH` / `CHATGPT_CDP_URL` |
| `PublicForward/ForVPS/vps_server.py` | `MODEL_ID_RE` += `chatgpt`; a `backend == "chatgpt"` branch in `chat_completions()` builds `task_fields`; result normalization comment updated to `deepseek \| chatgpt` (same shape) |
| `tests/test_vps_smoke.py`, `tests/test_http_e2e.py` | Extended with `chatgpt` routing + envelope + e2e cases |

Nothing about DeepSeek's or Qwen's behavior changed — every touch point
above is additive (a new enum value, a new branch, a new env var block).

---

## 3. Request flow (unchanged gateway mechanics, chatgpt-specific fields)

```
CLIENT → VPS   POST /v1/chat/completions
               Header: X-Session-ID: sess-abc123   (optional — CONTINUE only)
               Body:   {"model": "chatgpt" | "chatgpt(account1)", "messages": [...]}

VPS: resolve_backend_and_account("chatgpt(account1)") → ("chatgpt", "account1")
     → dispatch(backend="chatgpt", ...) → picks a registered chatgpt worker
     → envelope: {"type":"task","task_id":"...","request": {...}}   (deepseek-style)

WORKER (public_chatgpt.py): receives task → BrowserPool.run_task(prompt, mode,
        attachments, continue_url, preferred_account) → ChatGPTScraper.scrape()
        → {"type":"result","task_id":"...","result": {"ok","text","account",
           "conversation_url","mode","usage",...}}

VPS → CLIENT   OpenAI chat.completion + headers (X-Session-ID, X-Backend: chatgpt,
               X-Account-Name, X-Conversation-URL) + x_meta.backend == "chatgpt"
```

`task_fields` for chatgpt is intentionally sparse compared to DeepSeek's
(no `model_tab`/`deep_think`/`web_search`/`tool_messages`/`system_prompt`):

```python
task_fields = {
    "prompt": req.last_user_message(),
    "mode": mode,                      # "new" | "continue"
    "session_id": session_id,
    "preferred_account": preferred_account,
    "attachments": attachments,
    "messages": messages_payload,
    "max_tokens": req.max_tokens,
}
```

---

## 4. Login flow

Login detection and login execution are two different concerns handled by
two different pieces of code — this separation matters for the Turnstile
discussion in §6.

### 4.1 Login-state detection — `_is_logged_in()` / `_page_is_logged_in(page)`

This is the single most important (and most bug-prone, see §6.3) piece of
logic in the whole backend. It answers: **"is this Page currently showing a
logged-in ChatGPT session?"**

```python
async def _page_is_logged_in(self, page) -> bool:
    if page is None:
        return False
    if "chatgpt.com" not in (page.url or ""):
        return False                      # not even on the app yet
    for sel in CHATGPT_CONFIG["selectors"]["login_button"]:
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            return False                  # "Log in" visible → not logged in
    # secondary confirmation the app shell actually rendered
    for sel in prompt_textarea_selectors + main_area_selectors:
        if await page.query_selector(sel):
            return True
    return False
```

Three conditions, all required:
1. **On the right domain** (`chatgpt.com`) — rules out auth-provider pages,
   Cloudflare interstitials, `about:blank`, mid-redirect states.
2. **"Log in" button absent** — the primary signal. Never check chat-input
   presence as the *only* signal: the logged-out homepage still renders the
   "Ask ChatGPT" input, so input-presence alone is not a valid login signal
   (this was called out explicitly in the original design doc).
3. **App shell actually rendered** (prompt textarea or main chat area
   present) — a secondary confirmation used only *after* condition 2
   passes, to rule out a page that's simply still loading (see §6.3 for the
   exact bug this fixes).

`_is_logged_in()` is a thin wrapper: `_page_is_logged_in(self._page)`. The
`_page_is_logged_in(page)` form exists so callers that need to check a page
*other* than the scraper's own (e.g. a login popup — see §4.3) can reuse
the identical logic instead of duplicating it.

### 4.2 `ensure_authenticated()` — idempotent, always checked first

```
1. If no page yet → launch_browser(account)
2. goto chatgpt.com (if not already there) + settle
3. if _is_logged_in() → return True   ← session already valid, DONE
4. else → return await login()        ← only reached if step 3 fails
```

This ordering is why every Turnstile workaround in §6 works without
touching the rest of the codebase: whichever method you use to get a
profile into a logged-in state, `ensure_authenticated()` picks it up for
free on the next run, and the Cloudflare-prone `login()` path is simply
never reached.

### 4.3 `login()` — the automated flow (only runs when step 3 above fails)

```
0. goto chatgpt.com, confirm "Log in" button visible
1. Install a context.on("page") listener BEFORE clicking — "Log in" can
   open a POPUP window instead of redirecting the same tab; both are
   handled, whichever page appears becomes `auth = popup_page or self._page`
2. Fill email on `auth` → click "Continue"
   (_click_continue_button: exact-text match "Continue", explicitly
   rejects "Continue with phone number" / "Continue with Google" — see §6.1
   for why this needed a dedicated fix)
3. "Check your inbox" page → click "Continue with password"
   (the primary path — NEVER fills an email verification code; if this
   button is missing, checks whether the password field is already showing
   instead — some accounts skip the inbox step)
4. Fill password → click "Continue"
5. _wait_for_login_result(): poll up to login_wait=60s for one of:
     - Captcha/Turnstile visible → fail loud + debug screenshot
     - Inline error message → fail loud with the actual error text
     - URL leaves the auth pattern AND _is_logged_in() → success,
       write the cookies_seeded sentinel, save a cookie backup
     - timeout → fail loud + debug screenshot
```

### 4.4 Chat flow

```
NEW:      goto new_chat_url → fill prompt_textarea → click send (or Enter)
CONTINUE: goto the saved conversation_url (from SessionStore) → same as above
          → if conversation_url is missing, raises a clear ValueError (the
            worker maps this to a 404 so the client knows to start a new
            session)

wait_for_response(): poll every 800ms —
  generating = stop_button visible
  done       = NOT generating AND text unchanged across 2 consecutive polls
  every 5th poll also re-checks _is_logged_in() (mid-chat logout) and
  is_rate_limited() — both raise a clear error, never hang silently

_extract_response(prompt):
  PRIMARY:  last [data-message-author-role="assistant"] node → inner_text()
  FALLBACK: innerText of <main>, anchored on the LAST occurrence of the
            prompt, cut at the next "You said:" boundary, "You said:"/
            "ChatGPT said:" labels stripped, UI button labels filtered
            (Copy/Share/Regenerate/...), consecutive duplicate lines removed
```

Attachments: `set_input_files()` on the hidden `input[type=file]`, then wait
up to 15s for the preview chip to appear — a per-file timeout produces a
clear, specific error rather than silently sending without the file.

---

## 5. Session & pool management

**`SessionStore`** (`public_chatgpt.py`) — one JSON file per session under
`dataSession/chatgpt/<session_id>.json`, holding `account`,
`conversation_url`, `turn_count`. **No automatic TTL** — matches the
decision already made for DeepSeek/Qwen (see CHANGELOG "sesi 5"): a session
lives until explicitly deleted via `DELETE /v1/sessions/{id}` (broadcast by
the VPS to every worker) or the manual `cleanup sessions [max_age_s]`
console command.

**`BrowserPool`** (`browser_pool_chatgpt.py`) — one pre-warmed slot per
account from `authchatgpt.json`. `run_task()` acquires a slot (pinned to
`preferred_account` for CONTINUE requests so the same browser/conversation
is reused), calls `scraper.scrape()`, releases. On a rate-limit error in
`mode="new"`, it rotates to the next account and retries once; in
`mode="continue"` it fails loud instead (rotating would lose the
conversation's context, same policy as DeepSeek/Qwen).

Navigation for CONTINUE mode is **not** the pool's job here (unlike
DeepSeek's more elaborate skip-goto optimization) — `ChatGPTScraper.scrape()`
handles the `continue_url` goto itself, so the pool stays simple: acquire →
scrape → release.

---

## 6. Cloudflare Turnstile — the mitigation ladder

This is the part of the implementation that evolved the most after initial
delivery, based on real-world testing. Every layer below was added because
the previous one wasn't sufficient in the actual deployment environment —
they are **cumulative and complementary**, not alternatives to pick exactly
one from.

### 6.1 Selector-level bug: wrong "Continue" button clicked

**Symptom:** email filled correctly, but "Continue with phone number" got
clicked instead of "Continue".
**Cause:** `button:has-text("Continue")` is a *substring* match in
Playwright — it also matches "Continue with phone number" / "... Google" /
"... Apple".
**Fix:** `button:text-is("Continue")` (exact match) as the primary
selector, `_click_continue_button()` additionally verifies the resolved
element's own text is exactly `"continue"` before clicking, skipping to the
next candidate otherwise. This is a correctness bug fix, not a Turnstile
mitigation, but it had to be fixed before Turnstile investigation could
even begin (you can't diagnose a challenge page if you never reach it
because you clicked the wrong button first).

### 6.2 Layer 1 — `playwright-stealth` (JS-level patches, on by default)

Targets JS-visible automation signals: `navigator.webdriver`,
`navigator.plugins` shape, `navigator.permissions.query`, `iframe`
prototype mismatches, `chrome.csi`/`chrome.app`/`chrome.loadTimes`, WebGL
vendor, etc. Applied at the `BrowserContext` level via
`Stealth().apply_stealth_async(context)`. **Not sufficient on its own** —
Turnstile also inspects signals below the JS layer.

### 6.3 Bug found while building the manual-login workaround: false "already logged in"

While testing layer 1's limits, a **separate, unrelated bug** surfaced:
`login_chatgpt.py` reported "✅ Login detected" within ~9 seconds of
opening the browser, before the user had done anything.
**Cause:** `_is_logged_in()` at the time only checked *absence* of the
"Log in" button — which is trivially true on ANY page that isn't the
rendered app yet (blank page right after `goto()`, a page mid-navigation,
or — critically — the *original* tab sitting in the background showing
stale app markup while a **popup window** opened by clicking "Log in"
shows the actual Cloudflare challenge). Two compounding issues:
  - No domain/app-shell confirmation (fixed — see §4.1's three conditions).
  - No popup awareness: `login_chatgpt.py` only ever checked
    `scraper._page` (the original tab), never the popup where the real
    flow was happening.
**Fix:** `_page_is_logged_in(page)` refactor (any page, not just
`self._page`) + `login_chatgpt._find_logged_in_page(scraper)`, which
enumerates **every** currently open tab/popup in the context and returns
whichever one is actually `chatgpt.com` + logged in — self-healing whether
a popup appears, stays open, or closes. `wait_for_manual_login()` also adds
a 2-consecutive-poll debounce as a second line of defense.
Verified by `tests/_manual_login_wait_check.py::scenario_popup` — a test
that reproduces the exact stale-background-tab + separate-popup situation.

### 6.4 Layer 2 — real Chrome channel

`CHATGPT_BROWSER_CHANNEL=chrome` makes Playwright launch the real, installed
Google Chrome binary (`channel="chrome"`) instead of the bundled Chromium.
Real Chrome is generally trusted more than a bundled/headless Chromium
binary. **Still launched by Playwright**, so process-level automation
fingerprints from the *launcher* itself are still present — this layer
alone did not resolve the reported challenge.

### 6.5 Layer 3 — Patchright driver

[Patchright](https://github.com/Kaliiiiiiiiii-Virtual-Company/patchright)
is a patched fork of Playwright that removes CDP-level automation leaks
(`Runtime.enable` side effects, `getPlaywright`-style bindings) that
vanilla Playwright leaves behind regardless of JS stealth patches or which
browser channel is used. `base_chatgpt.py` selects the driver dynamically:

```python
CHATGPT_BROWSER_DRIVER=auto        # default: patchright if installed, else playwright
CHATGPT_BROWSER_DRIVER=patchright  # force; clear error if not installed
CHATGPT_BROWSER_DRIVER=playwright  # force vanilla
```

When patchright is active, JS stealth injection is **skipped entirely** —
stacking JS patches on a driver that already hides those signals can add
new, detectable artifacts (non-native property getters) instead of helping.
Verified live in the sandbox (`tests/_manual_driver_check.py`): driver
resolves to `patchright`, full stub login-flow works under it.
**Turnstile still reappeared for the owner even under this configuration**,
including in a fully *visible* (non-headless) manual login — meaning the
remaining signal wasn't about headless mode or JS/CDP patches at all.

### 6.6 Layer 4 — one-time manual login (`login_chatgpt.py`)

Opens a visible browser on the exact profile the worker uses; the user
solves whatever Turnstile shows and logs in entirely by hand. The script
never decides to close the browser on its own — see §6.8. Once the profile
holds a valid session, `ensure_authenticated()` never calls `login()` again
until that session expires, so the headless worker never revisits the
challenged page.

### 6.7 Layer 5 — manual cookie import (`import_chatgpt_cookies.py`)

For when even the *visible* manual login gets challenged: log into
chatgpt.com in your everyday, non-automated browser (no Playwright/CDP
involved anywhere), export cookies via the Cookie-Editor extension (Export
→ Export JSON — the httpOnly cookies are required, "Export Header string"
is not enough), then:

```bash
python import_chatgpt_cookies.py --account account1 --cookies export.json
```

The script converts the export via the existing
`cookie_editor_json_to_playwright()` helper (same one the legacy Qwen
cookie-file path already used), injects the cookies into
`profiles/chatgpt/<account>/`, reloads, and verifies with
`_page_is_logged_in()` before declaring success. Login is never executed at
all in this path — nothing for Cloudflare to challenge.

### 6.8 Layer 6 — CDP attach to an already-running real Chrome (strongest)

The final and strongest layer. `start_chatgpt_chrome.py` starts a **real,
self-started** Chrome process (`--remote-debugging-port=9222`) bound to the
same profile the worker uses — this Chrome was never launched *by
automation at all*, so none of the process-level fingerprints that
Turnstile can detect at launch time exist in the first place. The
worker/login-helper then **attaches** to it instead of launching its own:

```bash
python start_chatgpt_chrome.py --account account1     # terminal 1
# log in by hand in that window (Turnstile essentially never appears here)
CHATGPT_CDP_ATTACH=1 python public.py --backend chatgpt ...   # terminal 2
```

`connect_over_cdp(CHATGPT_CDP_URL)` replaces `launch_persistent_context()`
in `launch_browser()`. `close_browser()` in this mode only **disconnects**
— the real Chrome keeps running (its lifecycle belongs to the user, not the
worker; stop it explicitly with `start_chatgpt_chrome.py --account
account1 --stop`). Stealth injection is skipped here too, for the same
reason as Patchright mode. Verified live in the sandbox: a real chromium
process started manually with `--remote-debugging-port`, then
`ChatGPTScraper` with `CHATGPT_CDP_ATTACH=1` successfully attached, read
the page, and disconnected cleanly without killing the browser.

### 6.9 Decision table

| Symptom | Next layer to try |
|---|---|
| Login works but wrong button gets clicked | Already fixed (§6.1) — update if you see it again |
| Turnstile only during *automated* headless login | §6.2 (already on) → §6.4 `channel=chrome` → §6.5 patchright |
| Turnstile even during *visible* manual login | §6.6 `login_chatgpt.py` won't help by itself — go straight to §6.7 or §6.8 |
| Turnstile even just *browsing* chatgpt.com under any Playwright-launched browser | §6.8 CDP attach — the browser must not be launched by automation at all |
| You have no interactive access to the worker machine at all | §6.7 cookie import (export cookies elsewhere, transfer the JSON file) |

---

## 7. Testing — how this "passes"

Two test categories exist: the **repo's real, permanent test suite**
(offline, no browser needed, extended for chatgpt) and a set of **ad-hoc
verification scripts** (`tests/_manual_*.py`) written specifically to prove
each ChatGPT-specific fix, using HTML stubs / a real headless browser but
never the actual ChatGPT service.

### 7.1 Permanent suite (gateway-level, all 3 backends)

```bash
python tests/test_vps_smoke.py
python tests/test_http_e2e.py
python tests/test_delete_session_e2e.py
```

- `test_vps_smoke.py::test_resolve_backend_and_account` — `"chatgpt"` →
  `("chatgpt", None)`, `"chatgpt(account1)"` → `("chatgpt", "account1")`,
  unknown model → 400 mentioning `chatgpt` in the error.
- `test_vps_smoke.py::test_dispatch_all_backends` — chatgpt uses the
  deepseek-style envelope (`task_id`/`request`) and normalizes the same way.
- `test_vps_smoke.py::test_stats_and_accounts` — a chatgpt worker's
  accounts show up correctly in `/v1/models`-style aggregation.
- `test_http_e2e.py` — a fake WebSocket worker registers with
  `backend="chatgpt"`, a real `POST /v1/chat/completions` with
  `model: "chatgpt"` and `model: "chatgpt(account1)"` both round-trip
  correctly through real uvicorn + httpx, asserting `X-Backend: chatgpt`
  and `x_meta.backend == "chatgpt"`.
- (Regression bonus: fixed a pre-existing, unrelated bug in this same file
  — `model: "deepseek-chat"` no longer matches the current `MODEL_ID_RE`
  and used to hang the test forever waiting on a task envelope that was
  never dispatched. Fixed to use `"deepseek"` + an explicit `recv()`
  timeout.)

### 7.2 Ad-hoc ChatGPT-specific verification scripts

All are self-contained (`python3 tests/_manual_X.py`), require no real
ChatGPT credentials or network access, and print a clear PASS/FAIL per case.

| Script | What it proves |
|---|---|
| `_manual_login_detection_check.py` | `_is_logged_in()` on stub HTML: correct for logged-out / logged-in, **and** the two false-positive regressions (blank/loading page on chatgpt.com, off-domain page) both correctly report "not logged in" |
| `_manual_login_wait_check.py` | `wait_for_manual_login()`: times out correctly when nobody logs in; detects a real login; **`scenario_popup`** reproduces the exact reported bug (stale background tab + separate popup) and proves detection happens via the popup, with `scraper._page` correctly repointed to it |
| `_manual_continue_button_check.py` | `_click_continue_button()` picks the real "Continue" button even when "Continue with phone number" sits earlier in the DOM |
| `_manual_confirm_close_check.py` | `confirm_close()` never closes the browser without an explicit "y"/"yes"/Enter-with-default-yes answer; Ctrl+C/EOF/`"n"` all correctly leave it open |
| `_manual_stealth_check.py` | `playwright-stealth` integration applies without error; `navigator.webdriver` is masked afterwards |
| `_manual_driver_check.py` | Driver auto-selection resolves to `patchright` when installed; the full stub login-flow (persistent context + login-state check) works correctly under whichever driver is active |
| `_manual_cookie_import_check.py` | Cookie-Editor export parsing/conversion: plain list form, wrapped `{"cookies":[...]}` form, and three distinct error paths (empty list, non-JSON/header-string export, missing file) each produce a clear, actionable message |

### 7.3 Full verification command (what was actually run before every delivery)

```bash
python tests/_manual_driver_check.py
python tests/_manual_cookie_import_check.py
python tests/_manual_login_detection_check.py
python tests/_manual_login_wait_check.py
python tests/_manual_continue_button_check.py
python tests/_manual_confirm_close_check.py
python tests/_manual_stealth_check.py
python tests/test_vps_smoke.py
python tests/test_http_e2e.py
python tests/test_delete_session_e2e.py
PYTHONPATH=. python tests/test_stuck_signal_regression.py   # deepseek regression guard
```

All eleven pass with exit code 0 as of this document. The last one is not
chatgpt-specific — it's run every time as a regression guard to confirm
none of the shared-repo touch points (config, vps_server.py, public.py)
accidentally changed DeepSeek/Qwen behavior.

### 7.4 What is explicitly NOT covered by automated tests

Real ChatGPT login and chat cannot be exercised in CI/sandbox (no real
account, and doing so would risk tripping Cloudflare against a shared IP).
These require a manual checklist with real credentials:

1. Fill `cookies/authchatgpt.json` with one real account.
2. `python public.py --backend chatgpt --headless` and confirm the pool
   warms up (or use one of the §6 workarounds if Turnstile blocks it).
3. `POST /v1/chat/completions` with `model: "chatgpt"`, confirm a real
   answer + `X-Conversation-URL` populated.
4. Repeat with the same `X-Session-ID` → confirm the conversation continues
   in-context.
5. Send an image attachment → confirm the preview + a relevant answer.
6. Restart the worker → confirm CONTINUE still works (SessionStore survives
   on disk).
7. Confirm DeepSeek and Qwen workers are unaffected (their own smoke/e2e
   tests still pass, which is enforced automatically above).

---

## 8. Known limitations / v1.1 roadmap

- No `think_mode` / model picker — chat-only, default UI model.
- Tool/function calling is accepted in the request but not executed
  against the ChatGPT UI.
- No IMAP-based auto email verification — if an account is ever forced
  through an email-code verification path (no "Continue with password"
  option), the automated flow fails loud rather than attempting to read
  the code.
- Cloudflare Turnstile is a moving target on OpenAI's side; the layered
  mitigations in §6 are the strongest available today but are not a
  permanent guarantee. If all six layers fail, that is worth reporting
  upstream (to this repo) rather than retrying blindly.
