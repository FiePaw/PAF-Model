# Grok Backend — Deep Dive

> Companion reference to `README.md` / `CHANGELOG.md`. This document
> explains the Grok backend end-to-end: how it's structured, how every
> piece works, why its auth model differs from the retired ChatGPT
> backend, the Cloudflare/anti-bot mitigation ladder (REUSED unchanged
> from ChatGPT), and exactly how the implementation is verified to work
> ("how it passes"). Written for anyone who needs to operate, debug, or
> extend this backend without re-reading the whole migration history.
>
> **This backend REPLACES the ChatGPT backend entirely.** Every file that
> used to live under `*_chatgpt*` / `config/chatgpt.py` has been removed;
> the equivalent `*_grok*` / `config/grok.py` files take their place. See
> `CHANGELOG.md` for the migration entry.

---

## 1. What this backend is

Grok (grok.com) is the **third backend** in PAF-Model, alongside DeepSeek
and Qwen — replacing the ChatGPT backend that previously held that slot.
It plugs into the exact same unified gateway (`vps_server.py`) using the
same `model` routing convention (`grok` / `grok(account1)`), the same
`X-Session-ID` continuation header, and the same OpenAI-compatible response
shape the other two backends already produce.

**v1 scope (deliberately limited, ported from the owner-supplied reference
scraper — `grok.js` / `grokdebug.js` / `login-capture-grok.js`):**

| Capability | Status |
|---|---|
| Chat (new + continue) | ✅ |
| Code / canvas block extraction | ✅ |
| Attachments (images/files) | ⚠️ generic/best-effort — not covered by the reference scraper, unverified against a live account |
| Multi-account, rate-limit rotation | ✅ |
| Session persistence (no TTL, disk-backed) | ✅ |
| Response-choice A/B dialog ("Which response do you prefer?") | ✅ (Grok-specific UI quirk, not present on ChatGPT) |
| Automated email+password login | ❌ **not applicable** — grok.com is SSO-only (see §4) |
| `think_mode` / model picker | ❌ not supported — Grok UI's default model only |
| Tool/function calling execution | ❌ accepted in the request, not executed against the UI |
| Streaming responses | ❌ `stream: true` is ignored, same as DeepSeek/ChatGPT |

---

## 2. File structure

```
config/grok.py                 GROK_CONFIG (selectors, timeouts, browser_channel,
                                cdp_attach) + GROK_AUTH_CONFIG (auth file, env
                                fallback, login_wait, manual_login_required)

scrapers/base_grok.py          BaseAIChatScraper: browser lifecycle (persistent
                                profile, driver selection, CDP attach — REUSED
                                verbatim from base_chatgpt.py), account
                                rotation, login-state detection, generic
                                wait_for_response() polling.

scrapers/grok_scraper.py       GrokScraper(BaseAIChatScraper): chat flow
                                (send_prompt/scrape), response-choice A/B
                                dialog handling, response text cleaning,
                                canvas/code-block extraction — all ported
                                1:1 from grok.js.

browser_pool_grok.py           Pre-warmed pool, 1 slot/account, round-robin +
                                preferred_account acquire, respawn on crash.

public_grok.py                 Local worker: WS connection to the VPS,
                                SessionStore (disk-backed, no TTL), task
                                dispatch, interactive console.

login_grok.py                  ONE-TIME manual login helper (visible browser,
                                you complete SSO by hand). Mitigation layer 4.

import_grok_cookies.py         Zero-automation cookie import from a
                                Cookie-Editor export. Mitigation layer 5.

start_grok_chrome.py           Starts a real, self-launched Chrome with
                                --remote-debugging-port for CDP attach.
                                Mitigation layer 6 (strongest).

cookies/authgrok.json          Account name registry (email/password fields
                                are optional/reserved, not used to log in).
profiles/grok/<account>/       Persistent Playwright profile per account.
```

---

## 3. Why Grok replaced ChatGPT here

The owner supplied a working reference scraper for grok.com
(`grok.js`/`grokdebug.js`/`login-capture-grok.js`, built on Puppeteer) as
the specification for this backend. Studying it surfaced one structural
fact that shaped every design decision below: **the reference scraper
never authenticates**. It only ever loads cookies captured from a fully
manual login (`login-capture-grok.js`) and then drives grok.com with
those cookies already present. There is no email/password form filled
anywhere in the reference code, because grok.com doesn't have one — it
authenticates via SSO (Google / X (Twitter) / Apple / email-link).

This backend follows that same model faithfully rather than trying to
force ChatGPT's "email → Continue → password" pattern onto a site that
doesn't have it.

---

## 4. Auth model — the one load-bearing deviation from ChatGPT

| | ChatGPT (retired) | Grok |
|---|---|---|
| Login form | email → "Continue" → password | **none** — SSO only (Google/X/Apple/email-link) |
| Automated fill | Yes (`login()` in `chatgpt_scraper.py`) | **No** — `GrokScraper` has no `login()` method at all |
| `ensure_authenticated()` on invalid session | Falls back to automated `login()` | **Fails loud** with instructions to run `login_grok.py` or `import_grok_cookies.py` |
| `cookies/auth*.json` fields | `email`+`password` REQUIRED | `email`+`password` OPTIONAL/reserved — only `name` matters |
| `headless_during_login` | `True` (hard constraint) | N/A — there is no login() to run headless |

Everything else (persistent profile per account, `AuthStore` account
registry, account rotation on rate-limit, `_is_logged_in()` design
principle) is unchanged.

### Establishing a session (pick one)

1. **`login_grok.py --account account1`** — opens a VISIBLE browser bound
   to the exact profile the worker uses. You log in by hand (any SSO
   provider), solving any Cloudflare challenge yourself. Polls
   `_is_logged_in()` (popup-aware — an SSO click can open a new window)
   until it detects success, then never closes the browser without your
   confirmation.
2. **`import_grok_cookies.py --account account1 --cookies <export.json>`**
   — zero automation. Log into grok.com in your everyday browser, export
   cookies via the Cookie-Editor extension (Export → Export JSON —
   httpOnly cookies are required), then run this script to inject them
   into the profile and verify the session.

After either path, the headless worker (`public.py --backend grok`) reuses
the seeded profile and never touches a login page.

---

## 5. Chat flow (ported 1:1 from `grok.js`)

```
NEW:      goto https://grok.com/ → fill composer → send → handle A/B choice
          dialog (if shown) → wait_for_response() → extract + clean text
CONTINUE: goto saved conversation_url (SessionStore) → same as above
```

- **Composer selectors**: grok.com has no stable `data-testid` on its
  input (unlike ChatGPT's `#prompt-textarea`) — `GROK_CONFIG` falls back
  through `textarea` → `[contenteditable="true"]` → `input[type="text"]`
  → `[role="textbox"]`, exactly as `grok.js`'s `inputSelectors` cascade
  does.
- **Send button**: no stable selector either — `_click_send_button()`
  tries the configured selectors first, then falls back to a text/
  aria-label scan over every `button`/`[role="button"]` for the word
  "send" (ported from `grok.js`).
- **Response-choice A/B dialog** ("Which response do you prefer?" /
  "Respons mana yang Anda pilih") — a real Grok UI quirk not present on
  ChatGPT. `_handle_response_choice()` waits for both candidates to
  finish "thinking", then randomly picks one (or clicks Skip/Lewati),
  ported from `grok.js`'s `handleResponseChoice()`.
- **"Still generating" detection**: richer than ChatGPT's stop-button-only
  check — Grok surfaces web-search progress text ("Membaca", "Menelusuri",
  "Thinking…", etc.) inside the response area while not yet finished.
  `_is_generating()` checks both the stop button AND this text (ported
  from `grok.js`'s `isGenerating()`/`searchStatusPatterns`).
- **Response cleaning**: `_clean_response_text()` ports `grok.js`'s full
  regex cascade — strips image-credit lines, search-status text,
  timestamps ("12s"), speed labels ("Fast"/"Slow"/"Auto"), copy/share
  button text, and suggested follow-up prompts.
- **Canvas/code extraction**: `_extract_canvas_blocks()` scans
  `pre, code, [class*="code"], [class*="canvas"]` (not just ``` fenced
  blocks in the plain-text response) and de-duplicates via SHA-256
  content hash — ported from `grok.js`'s `saveCanvasContent()`/
  `hashContent()`.

---

## 6. Cloudflare / anti-bot — the mitigation ladder

**REUSED VERBATIM from the ChatGPT backend** (only naming changed:
`CHATGPT_*` env vars → `GROK_*`, `chatgpt.com` → `grok.com`,
`profiles/chatgpt` → `profiles/grok`). The layers are cumulative and
complementary, not alternatives to pick exactly one from.

### 6.1 Layer 1 — `playwright-stealth` (JS-level patches, on by default)

Targets JS-visible automation signals: `navigator.webdriver`,
`navigator.plugins` shape, `navigator.permissions.query`, `iframe`
prototype mismatches, `chrome.csi`/`chrome.app`/`chrome.loadTimes`, WebGL
vendor, etc. Applied at the `BrowserContext` level via
`Stealth().apply_stealth_async(context)`. **Not sufficient on its own.**

### 6.2 Layer 2 — real Chrome channel

`GROK_BROWSER_CHANNEL=chrome` makes Playwright launch the real, installed
Google Chrome binary (`channel="chrome"`) instead of the bundled
Chromium. **Still launched by Playwright**, so process-level automation
fingerprints from the *launcher* itself are still present.

### 6.3 Layer 3 — Patchright driver

[Patchright](https://github.com/Kaliiiiiiiiii-Virtual-Company/patchright)
removes CDP-level automation leaks (`Runtime.enable` side effects,
`getPlaywright`-style bindings) that vanilla Playwright leaves behind
regardless of JS stealth patches or browser channel. Selected dynamically:

```python
GROK_BROWSER_DRIVER=auto        # default: patchright if installed, else playwright
GROK_BROWSER_DRIVER=patchright  # force; clear error if not installed
GROK_BROWSER_DRIVER=playwright  # force vanilla
```

When patchright is active, JS stealth injection is **skipped entirely**
(stacking JS patches on a driver that already hides those signals can add
new, detectable artifacts instead of helping). Verified in this repo's
sandbox (`tests/_manual_driver_check.py`): driver resolves to
`patchright`, full stub flow works under it.

### 6.4 Layer 4 — one-time manual login (`login_grok.py`)

Opens a visible browser on the exact profile the worker uses; the user
solves whatever SSO/Cloudflare challenge shows and logs in entirely by
hand. Never closes the browser on its own. Once the profile holds a valid
session, `ensure_authenticated()` reuses it — since Grok has **no**
automated login to fall back to, this is the primary establishment path.

### 6.5 Layer 5 — manual cookie import (`import_grok_cookies.py`)

For when even the *visible* manual login gets challenged: log into
grok.com in your everyday, non-automated browser (no Playwright/CDP
involved anywhere), export cookies via Cookie-Editor (Export → Export
JSON — httpOnly cookies required), then:

```bash
python import_grok_cookies.py --account account1 --cookies export.json
```

Converts via `cookie_editor_json_to_playwright()` (shared with
DeepSeek/Qwen/legacy-ChatGPT), injects into `profiles/grok/<account>/`,
reloads, verifies with `_page_is_logged_in()`. This is also exactly the
technique the owner-supplied `login-capture-grok.js` reference relies on.

### 6.6 Layer 6 — CDP attach to an already-running real Chrome (strongest)

`start_grok_chrome.py` starts a **real, self-started** Chrome process
(`--remote-debugging-port=9222`) bound to the same profile the worker
uses — this Chrome was never launched *by automation at all*. The
worker/login-helper then **attaches** to it instead of launching its own:

```bash
python start_grok_chrome.py --account account1     # terminal 1
# log in by hand in that window (Cloudflare essentially never appears here)
GROK_CDP_ATTACH=1 python public.py --backend grok ...   # terminal 2
```

`connect_over_cdp(GROK_CDP_URL)` replaces `launch_persistent_context()` in
`launch_browser()`. `close_browser()` in this mode only **disconnects** —
the real Chrome keeps running (stop it with `start_grok_chrome.py
--account account1 --stop`). Stealth injection is skipped here too.

### 6.7 Decision table

| Symptom | Next layer to try |
|---|---|
| Cloudflare only during *automated* headless access | §6.1 (already on) → §6.2 `channel=chrome` → §6.3 patchright |
| Cloudflare even during *visible* manual login | §6.4 `login_grok.py` won't help by itself — go straight to §6.5 or §6.6 |
| Cloudflare even just *browsing* grok.com under any Playwright-launched browser | §6.6 CDP attach — the browser must not be launched by automation at all |
| No interactive access to the worker machine at all | §6.5 cookie import (export cookies elsewhere, transfer the JSON file) |

---

## 7. Testing — how this "passes"

Two test categories: the **repo's real, permanent test suite** (offline,
no browser needed, covers `grok` alongside `deepseek`/`qwen`) and a set of
**ad-hoc verification scripts** (`tests/_manual_*.py`) written specifically
to prove the mitigation-ladder mechanics, using HTML stubs / a real
headless browser but never the actual Grok service.

### 7.1 Permanent suite (gateway-level, all 3 backends)

```bash
python tests/test_vps_smoke.py
python tests/test_http_e2e.py
python tests/test_delete_session_e2e.py
```

- `test_vps_smoke.py::test_resolve_backend_and_account` — `"grok"` →
  `("grok", None)`, `"grok(account1)"` → `("grok", "account1")`, unknown
  model → 400 mentioning `grok` in the error.
- `test_vps_smoke.py::test_dispatch_all_backends` — grok uses the
  deepseek-style envelope (`task_id`/`request`) and normalizes the same way.
- `test_vps_smoke.py::test_stats_and_accounts` — a grok worker's accounts
  show up correctly in `/v1/models`-style aggregation.
- `test_http_e2e.py` — a fake WebSocket worker registers with
  `backend="grok"`, a real `POST /v1/chat/completions` with `model: "grok"`
  and `model: "grok(account1)"` both round-trip correctly through real
  uvicorn + httpx, asserting `X-Backend: grok` and `x_meta.backend == "grok"`.

All three files run clean in this repo (verified during the migration —
`ALL VPS SMOKE TESTS PASSED`, `ALL HTTP E2E TESTS PASSED`,
`ALL DELETE-SESSION E2E TESTS PASSED`).

### 7.2 Ad-hoc Grok-specific verification scripts

All are self-contained (`python3 tests/_manual_X.py`), require no real
Grok credentials or network access, and print a clear PASS/FAIL per case.

| Script | What it proves |
|---|---|
| `_manual_login_detection_check.py` | `_is_logged_in()` on stub HTML: logged-out / logged-in / blank-loading / off-domain — same false-positive regression guard ChatGPT's fix established |
| `_manual_login_wait_check.py` | `login_grok.wait_for_manual_login()`: single-tab timeout + eventual success, and the SSO-**popup** scenario (login succeeds in a popup while the original tab shows stale markup) |
| `_manual_driver_check.py` | Driver resolution (`patchright`/`playwright`) + full stub flow (launch, route-intercept a fake grok.com page, verify login check) under the selected driver |
| `_manual_stealth_check.py` | `_apply_stealth()` doesn't throw and masks `navigator.webdriver` |
| `_manual_cookie_import_check.py` | `import_grok_cookies.load_and_convert_cookies()` / `_diagnose()`: valid export, wrapped `{"cookies":[...]}`, empty list, header-string export, missing file, and diagnostic hints (grok.com domain check + `sso` cookie presence check) |
| `_manual_confirm_close_check.py` | `login_grok.confirm_close()` always waits for an explicit answer, never auto-closes |

Note: the ChatGPT-era `_manual_continue_button_check.py` (which tested a
"Continue" button substring-match bug in ChatGPT's email/password login
form) has been **retired** — Grok has no such form, so the equivalent
method doesn't exist in `GrokScraper`.

---

## 8. Known limitations / v1.1 roadmap

- No `think_mode` / model picker — chat-only, default UI model.
- Tool/function calling is accepted in the request but not executed
  against the Grok UI.
- Attachments support is generic/best-effort and unverified against a
  live account (the reference scraper doesn't cover attachments).
- `rate_limited` text patterns in `config/grok.py` are best-effort
  placeholders — adjust once the exact grok.com rate-limit copy is
  confirmed against a live account.
- Cloudflare/anti-bot is a moving target on X.ai's side; the layered
  mitigations in §6 are the strongest available today but are not a
  permanent guarantee. If all six layers fail, that is worth reporting
  upstream (to this repo) rather than retrying blindly.
