# CHANGELOG

Semua perubahan penting pada proyek PAF-Model didokumentasikan di sini.  
Format mengikuti [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased] — Semua Backend: Protokol API Mode Bersama (Teks Biasa + Tool Calling Berbasis Penanda) dan Tool Calling untuk Grok

> **Perubahan lintas backend (DeepSeek, Qwen, Grok).** Sebelumnya setiap balasan dipaksa berbentuk envelope JSON sehingga model harus meng-escape sendiri tanda kutip, backslash, newline, dan kode di dalam string JSON — sumber utama error "invalid JSON", retry, dan rotasi akun. Kini jawaban biasa berupa **teks polos**, dan hanya pemanggilan fungsi yang memakai blok bertanda berisi nilai argumen **mentah**; JSON `tool_calls` disusun oleh kode (`json.dumps`), bukan oleh model. System prompt `[SYSTEM CONTEXT]` / `[USER REQUEST]` tetap dipertahankan.

### 1. Modul Protokol Bersama (`scrapers/tool_protocol.py`)
- **File baru** tanpa dependensi browser/jaringan, dipakai ketiga backend: pembangun prompt (`build_wrapped_prompt`, `build_tool_result_prompt`, `build_corrective_prompt`) dan parser balasan (`parse_model_output`).
- **Chat biasa (tanpa `tools`):** jawaban = teks polos, tidak ada yang divalidasi.
- **Dengan `tools`:** jawaban teks polos, kecuali model perlu memanggil fungsi → blok:
  `@@PAF-CALL:<fungsi>@@` / `@@PAF-ARG:<argumen>@@ <nilai mentah>` / `@@PAF-END@@`. Penanda sengaja tanpa `<`/`>` dan tanpa karakter khusus markdown agar selamat dari renderer; model diminta membungkus blok dalam fenced code block 4-backtick agar baris baru dan indentasi tetap utuh pada `innerText`. Parser tetap bekerja dengan/tanpa fence dan mengabaikan teks UI sebelum blok.
- **Konversi tipe** nilai argumen mengikuti skema `parameters` (`integer`, `number`, `boolean`, `object`, `array`). `function.arguments` kini **selalu string JSON** (sesuai spesifikasi OpenAI dan `API_USAGE.md`); sebelumnya Qwen mengembalikan objek.
- **Self-healing:** blok pemanggilan yang rusak (tidak ditutup, nama fungsi tidak dikenal, dst.) atau balasan kosong memicu satu pesan koreksi singkat di session yang sama (maks. 2×). Jawaban teks biasa tidak pernah di-retry karena format.
- **Kompatibel mundur:** envelope JSON lama (`{"status":"success"|"tool_calls",...}`) tetap dipahami, sehingga session yang dimulai sebelum update ini tetap berjalan. Envelope sukses yang rusak tidak pernah menggagalkan chat biasa; hanya `tool_calls` lama yang rusak yang di-retry.
- `[SYSTEM CONTEXT]` dikirim pada **setiap** turn (singkat untuk chat biasa) karena klien boleh mengganti `tools` antar request dan session lama masih berisi instruksi JSON.

### 2. Qwen
- `_build_wrapped_prompt()` / `_build_tool_result_prompt()` memakai modul bersama (kata "tool" tetap dihindari dalam instruksi; dipakai "function").
- `base_qwen.scrape()`: validasi JSON diganti `parse_model_output()`; logika retry/rotasi akun/hard-cap tetap, namun pemicunya kini hanya blok pemanggilan rusak atau balasan kosong.
- `scrape_with_tool_result()` kini punya putaran koreksi (sebelumnya langsung gagal).

### 3. DeepSeek
- Wrapper dan hasil-tool memakai modul bersama; client `system` message tetap ditambahkan ke `[SYSTEM CONTEXT]`.
- **Perbaikan state:** `send_prompt()` tidak lagi menimpa `_tools`/`_max_tokens`/`_system_prompt` pada pengiriman verbatim (koreksi / hasil tool), sehingga validasi nama fungsi dan konversi tipe tetap berlaku.
- `tools`/`max_tokens`/`system_prompt` kini diteruskan sampai Turn 2 (worker → pool → `scrape_with_tool_result`).
- `JSON_API_CONFIG`: kunci `chat_system_instruction` dihapus (sumber instruksi tunggal kini `tool_protocol.py`); `enabled` dan `max_corrective_retries` dipertahankan.

### 4. Grok — Tool Calling Kini Didukung
- `GrokScraper.scrape()` menerima `tools`, `max_tokens`, `tool_messages`, `next_user_msg`; prompt dibungkus dan balasan di-parse dengan modul bersama.
- Pemanggilan fungsi di-parse dari **teks mentah**; pembersih UI (`_clean_response_text`) sengaja tidak dijalankan pada tool call karena regex-nya membuang baris kosong dan memangkas indentasi sehingga merusak nilai argumen. Jawaban biasa tetap melalui pembersih seperti sebelumnya.
- Gateway (`vps_server.py`) kini meneruskan `tools` dan `tool_messages` ke worker Grok; `browser_pool_grok.run_task()` dan `public_grok.py` meneruskannya ke scraper.

### 5. Dokumentasi & Test
- `API_USAGE.md` §9 ditulis ulang (protokol baru, Grok didukung), plus perbaikan §13.5, §14, §15; `README.md` dan `GROK_BACKEND.md` diperbarui.
- Test offline baru: `tests/test_tool_protocol.py` (parser/builder), `tests/test_protocol_scrapers.py` (loop `scrape()` asli ketiga backend dengan browser stub), `tests/test_grok_tools_e2e.py` (gateway + worker palsu, dua giliran).
- **Catatan:** test ini membuktikan plumbing di sekitar balasan model, bukan bagaimana situs live (chat.qwen.ai, chat.deepseek.com, grok.com) merender blok bertanda pada `innerText`. Perlu verifikasi manual dengan akun nyata. `tests/test_http_e2e.py` bawaan sudah usang terhadap autentikasi API token (gagal juga pada kode sebelum perubahan ini) dan tidak diubah.

---

## [Unreleased] — Qwen Backend: Diagnostik Prompt Page (I/O), Pembaruan Selector UI qwen-chat-v2, dan Optimasi Latency CONTINUE

> **Pembaruan Backend Qwen.** NEW! Penambahan tool diagnostik mandiri untuk halaman percakapan prompt (`debug_prompt_qwen.py`) yang menguji alur I/O secara end-to-end pada **NEW mode** dan **CONTINUE mode**, identifikasi migrasi antarmuka Qwen Web ke sistem komponen `qwen-chat-v2`, pembaruan selector I/O, serta optimasi latensi respon mode CONTINUE yang memulihkan kecepatan respon dari ~8 detik kembali ke ~5 detik.

### 1. Tool Diagnostik Mandiri Prompt Page (`debug_prompt_qwen.py`)
- **Script Diagnostik Menyeluruh:** File baru [`debug_prompt_qwen.py`](./debug_prompt_qwen.py) untuk menguji, membedah, dan memvalidasi seluruh elemen DOM dan selector I/O pada halaman percakapan Qwen (`chat.qwen.ai`).
  - Mendukung pengujian **NEW mode** (buka chat baru, fill prompt, send, tangkap turn 1) dan **CONTINUE mode** (kirim prompt ke-2 di thread yang sama, verifikasi stabilitas respon turn 2).
  - Mode **No-Headless** (default) dan **Headless** (`--headless`).
  - Opsi `--auto-login`: otomatis login jika sesi akun belum aktif sebelum memulai pengetesan prompt.
  - Mode interaktif (`--interactive`) untuk inspeksi manual via DevTools (F12).
  - Perekaman artefak: screenshot `.png` dan HTML dump `.html` di setiap tahap ke `debug/`.
  - Generator summary otomatis ke `debug/summary_prompt/` ([`summary.md`](./debug/summary_prompt/summary.md) & [`summary.json`](./debug/summary_prompt/summary.json)).

### 2. Pembaruan Selector DOM UI Qwen Web (`qwen-chat-v2`)
- **Think Mode Dropdown (`qwen-chat-v2`):**
  - Menggantikan selector usang `.qwen-select-thinking-label` dengan komponen generasi baru Qwen:
    - Container/Trigger: `.qwen-thinking-selector`, `.qwen-chat-v2-dropdown-menu-trigger`
    - Selected Label: `.qwen-chat-v2-dropdown-menu-select-label`
    - Options: `.qwen-chat-v2-dropdown-menu li`, `.qwen-chat-v2-dropdown-menu-item`, `[role="option"]`
  - Diperbarui pada [`config/qwen.py`](./config/qwen.py) dan [`scrapers/qwen_scraper.py`](./scrapers/qwen_scraper.py).
- **Send Button & Status Disabled:**
  - Menambahkan selector spesifik tombol kirim Qwen: `button.send-button:not(.disabled)` dan `button[aria-label="Send"]:not([disabled])`.
- **Input Textarea:**
  - Menambahkan class spesifik `textarea.message-input-textarea` sebagai prioritas pertama sebelum fallback `textarea[placeholder]`.
- **Response Container & Markdown:**
  - Memprioritaskan `.qwen-markdown` dan `.chat-response-message` pada strategi ekstraksi teks respon.
- **Tombol New Chat:**
  - Menyesuaikan selector tombol chat baru dengan elemen DOM terkini: `[aria-label="New Chat"]`, `div.sidebar-entry-fixed-list-content`.

### 3. Optimasi Latency Respon Mode CONTINUE (Pemulihan dari ~8s ke ~5s)
- **Eliminasi Pencarian Think-Mode di Mode CONTINUE:**
  - UI Qwen Web menyembunyikan trigger think-mode pada halaman percakapan yang sedang berjalan (`/c/<id>`).
  - Memperbaiki kondisi guard di `send_prompt()` ([`scrapers/qwen_scraper.py`](./scrapers/qwen_scraper.py)) agar hanya mengeksekusi `_set_think_mode()` pada mode `new` (`mode != "continue"` dan `not self._think_mode_applied`).
- **Penghapusan Pemanggilan Diagnostik Otomatis di Jalur Produksi:**
  - Menghapus pemanggilan otomatis `self.debug_think_mode_selectors()` saat trigger think-mode tidak ditemukan di `send_prompt()`. Ini mencegah eksekusi 2x full document `createTreeWalker` via Playwright CDP yang sebelumnya membuang 2.5–3.5 detik per pesan.
  - Method `debug_think_mode_selectors()` tetap dipertahankan di class scraper dan dapat dipanggil secara manual saat dibutuhkan.
- **Pencegahan Reset Flag `_think_mode_applied` pada Mode CONTINUE di Worker:**
  - Pada [`public_qwen.py`](./public_qwen.py), flag `_think_mode_applied` kini hanya di-reset ke `False` jika `mode != "continue"`.
- **Prioritaskan Selector Input Cepat di Worker Readiness Check:**
  - Menambahkan `textarea.message-input-textarea` pada posisi teratas `_INPUT_SELECTORS` di [`public_qwen.py`](./public_qwen.py).

### 4. Deteksi Login Otomatis, Pre-Flight Auth Guard & Penanganan Modal Umur
- **Pre-Flight Authentication Check Sebelum Pengiriman Prompt:**
  - Menambahkan pengecekan `if await self.is_session_expired():` pada `scrape()` di [`scrapers/base_qwen.py`](./scrapers/base_qwen.py) sebelum prompt dikirim. Jika akun belum login / sesi habis, scraper otomatis menjalankan `ensure_authenticated()` atau merotasi ke akun lain.
- **Deteksi Unauthenticated Komprehensif (< 10ms):**
  - Memperbarui `_is_unauthenticated()` pada [`scrapers/qwen_scraper.py`](./scrapers/qwen_scraper.py) dengan evaluasi JS cepat memeriksa container auth header (`.auth-buttons`, `.header-right-auth-button`), keberadaan tombol "Log in" / "Sign up", dan URL `/auth`.
- **Dukungan Form Login 2-Tahap (Password Switch):**
  - Pada `login()` di [`scrapers/qwen_scraper.py`](./scrapers/qwen_scraper.py), ditambahkan klik otomatis pada tombol switch `button:has-text("Log in with a password")` agar field password dimunculkan sebelum pengisian form.
- **Auto-Dismiss Modal Verifikasi Umur ("Confirm your age to continue"):**
  - Menambahkan helper `_dismiss_age_confirmation_modal()` di [`scrapers/qwen_scraper.py`](./scrapers/qwen_scraper.py) yang otomatis mengklik `Continue` jika pop-up verifikasi usia muncul, mencegah browser membeku / macet saat sesi guest aktif.
- **Recovery Cerdas pada `TimeoutError`:**
  - Memperbarui blok `except TimeoutError` di [`scrapers/base_qwen.py`](./scrapers/base_qwen.py) untuk mengecek apakah timeout disebabkan oleh sesi yang logout/expired. Jika ya, scraper segera me-reautentikasi atau merotasi akun tanpa membuang waktu mengulang percobaan pada sesi yang rusak.

### 5. Eliminasi False (Guest): Evaluasi Ganda Positif-Negatif & Penghapusan Pemindaian Frasa Body
- **Penghapusan Pemindaian Teks Body `session_expired_phrases`:**
  - Menghapus pemindaian string bebas `contains_any(body, ...)` pada `is_session_expired()` di [`scrapers/qwen_scraper.py`](./scrapers/qwen_scraper.py). Sebelumnya, kata "log in", "sign in", atau "unauthorized" di dalam pesan chat atau contoh kode memicu deteksi sesi expired secara keliru.
- **Logika Evaluasi Ganda (Positive & Negative Guard):**
  - Pada `_is_unauthenticated()` di [`scrapers/qwen_scraper.py`](./scrapers/qwen_scraper.py), ditambahkan deteksi sinyal positif akun login (`.temporary-chat-entry`, `.sidebar-wrapper`, `.sidebar-entry-fixed-list-content`). Jika sinyal positif ada dan header auth tidak ada, halaman dipastikan berstatus LOGIN (`False`).
- **Pengecekan Header Terisolasi (#qwen-chat-header-right):**
  - Selector tombol login pada [`config/qwen.py`](./config/qwen.py) dikunci hanya pada header kanan (`#qwen-chat-header-right .auth-buttons`), mencegah false positive dari tombol demo atau teks di area konten utama.
- **Pengecekan Modal Usia Khusus Kontainer Dialog:**
  - Evaluasi teks verifikasi umur kini dibatasi hanya pada elemen `[role="dialog"]` / `.ant-modal`, bukan seluruh teks dokumen.
- **Jeda Hidrasi Anti-Race-Condition:**
  - Menambahkan verifikasi ulang singkat (400ms) pada `_ensure_page_ready()` jika status unauthenticated dicurigai saat cold-load, memberi waktu React SPA memvalidasi sesi akun sebelum memicu login otomatis.

---

## [Unreleased] — Qwen Backend: Diagnostik Login Otomatis, Pemetaan Selector Baru, dan Isolasi Profil Akun

> **Pembaruan Backend Qwen.** Penambahan tool diagnostik auth mandiri (`debug_login_qwen.py`) dengan auto-summary generator, identifikasi perubahan alur login Qwen Web (v0.3.20), pemetaan selector baru, serta pengetatan isolasi persistent browser profile akun ke `profiles/qwen/<account>/`.

### 1. Tool Diagnostik Mandiri & Auto-Summary (`debug_login_qwen.py`)
- **Script Diagnostik Komprehensif:** File baru [`debug_login_qwen.py`](./debug_login_qwen.py) untuk menguji, membedah, dan mendiagnosis seluruh tahapan login Qwen (`chat.qwen.ai`) secara live.
  - Mendukung mode **No-Headless** (default, browser GUI tampil) dan mode **Headless** (`--headless`).
  - Mode interaktif (`--interactive`): menahan jendela browser terbuka untuk inspeksi elemen langsung via Chrome DevTools (F12).
  - Mode profil bersih (`--clean-profile`): pengujian first-time login cold-start tanpa terpengaruh cache/sesi lama.
  - Opsi `--skip-fill` dan `--skip-submit` untuk inspeksi selektif tanpa mengubah status form.
- **Generator Summary Otomatis:** Script secara otomatis menganalisis hasil pengujian dan menyimpannya ke direktori `debug/summary/`:
  - [`debug/summary/summary.md`](./debug/summary/summary.md): Ringkasan lengkap format Markdown (tabel evaluasi selector, temuan alur baru, dan rekomendasi kode).
  - [`debug/summary/summary.json`](./debug/summary/summary.json): Data ringkasan terstruktur JSON untuk kemudahan parsing terotomasi.
  - Arsip riwayat pengujian ber-timestamp (`summary_YYYYMMDD_HHMMSS.md` & `.json`).
- **Perekaman Artefak Lengkap:** Tangkapan layar otomatis (`.png`) dan dump DOM HTML penuh (`.html`) tersimpan pada folder `debug/` di setiap tahap (landing page, auth page, form terisi, dan respon pasca-submit).

### 2. Temuan Alur UI Baru Qwen Web (`qwen-chat-fe/0.3.20`)
- **Alur Login Dua Tahap (Two-Step Flow):**
  - Halaman `/auth` kini secara default menampilkan alur kode verifikasi / SSO. Field password **disembunyikan** hingga tombol `Log in with a password` diklik.
  - Ditambahkan dukungan deteksi dan interaksi tombol switch: `button:has-text("Log in with a password")`.
- **Perubahan Tipe Input Email:**
  - Input email diubah oleh Qwen menjadi `type="text"` dengan `name="email"` (sebelumnya `type="email"`).
  - Pembaruan selector email rekomendasi: `input[name="email"]` dan `input[placeholder*="Email" i]`.
- **Input Password & Submit Button:**
  - Input password menggunakan `name="password"` dan `type="password"`.
  - Tombol submit formulir login menggunakan class khusus `.qwenchat-auth-pc-submit-button` dengan teks "Log in".
- **Koreksi Deteksi Sesi Guest:**
  - Halaman landing `chat.qwen.ai` merender textarea input chat untuk pengguna tamu (guest), sehingga keberadaan `#chat-input` tidak lagi dijadikan acuan tunggal status login. Scraper memverifikasi ketiadaan tombol "Log in" / "Sign up" untuk memastikan sesi terautentikasi.

### 3. Pengetatan Isolasi Persistent Profile (`profiles/qwen/<account>/`)
- **Isolasi Profil Konsisten:**
  - Scraper Qwen kini secara ketat memetakan seluruh sesi akun ke subfolder namespace khusus: `profiles/qwen/<account>/` (misal `profiles/qwen/account1/`, `profiles/qwen/account2/`), mencegah konflik SingletonLock dengan backend DeepSeek (`profiles/deepseek/`) dan Grok (`profiles/grok/`).
- **Perbaikan Browser Restart:**
  - Memperbaiki method `restart_browser()` pada `scrapers/base_qwen.py`: sebelumnya restart browser tanpa argumen menyebabkan profil terlempar ke `profiles/qwen/default`. Sekarang `restart_browser()` mempertahankan `self.account` dan membuka kembali direktori profil akun yang sama.
- **Dukungan Rotasi Multi-Akun Email+Password:**
  - Memperbarui `_rotate_account()` pada `scrapers/base_qwen.py` agar mendukung rotasi akun dari `cookies/authqwen.json` (`account1` → `account2` → `account3` → `account4`).
  - Setiap rotasi menutup browser context aktif dan meluncurkan persistent context baru ke profil akun berikutnya.
- **Koreksi Identitas Akun pada Prompt Wrapper:**
  - Memperbaiki `_build_wrapped_prompt` dan `_build_tool_result_prompt` pada `scrapers/qwen_scraper.py` agar field `"model"` menggunakan `self.account` (misal `"model": "account1"`), bukan fallback statis `"qwen"`.
- **Pembersihan Dokumentasi Browser Pool:**
  - Menyelaraskan seluruh docstring dan komentar di `browser_pool_qwen.py` agar secara eksplisit merujuk ke `profiles/qwen/<account>/`.

---

## [Unreleased] — MAJOR: client API token (auth) for the VPS gateway

> **Breaking change.** Every HTTP endpoint of `vps_server.py` now requires
> `Authorization: Bearer <API_TOKEN>`. Existing clients without the token get
> `401`. There is intentionally **no** "auth off" / transition mode.

**Why:** until now the public REST API had no client authentication at all —
anyone who could reach the VPS port could use it. `PAF_TOKEN` only protected
the worker WebSocket (`/ws/worker`), not callers.

**Design (single shared token, CLI-managed):**
- **One token for all users**, sent as `Authorization: Bearer <token>`.
  Separate from `PAF_TOKEN` (worker auth), which is unchanged.
- Enforced by a pure-ASGI `ApiTokenMiddleware` on **all** HTTP routes
  (`/`, `/health`, `/v1/models`, `/v1/chat/completions`,
  `DELETE /v1/sessions/{id}`, unknown paths, `/docs`, `/openapi.json`), before
  routing/body parsing. WebSocket scopes pass through (worker auth unchanged).
- Responses: `401` + `WWW-Authenticate: Bearer` for missing/invalid token;
  `503` if the server has no token configured (**fail closed**).
- Constant-time comparison (`hmac.compare_digest`); non-ASCII credentials are
  rejected cleanly. Rejections are logged (method, path, client IP — never the
  token).
- Middleware order keeps **CORS outermost**: browser preflight `OPTIONS` works
  without credentials and `401` responses carry CORS headers.
- No token configured → every request is `503`. Without an interactive
  terminal `python vps_server.py` **refuses to start** (exit code 2) and
  `start.sh` stops instead of looping on that config error.

**Optional token expiry:** `token generate 7d`, `token rotate 12h`,
`token set <value> 2w`, `token expire <expiry|never>` (units `s/m/h/d/w`, max
10 years; default = never expires). An expired token is rejected automatically
with `401` ("API token has expired...") — no restart, enforced on every request
and again after a restart. Stored as an optional `expires_at=<UTC ISO>` second
line of the token file (files without it keep working and never expire; an
unparsable expiry line fails closed = expired). Tokens from `--api-token` /
`PAF_API_TOKEN` never expire. Offline: `--generate-token --expires 7d`,
`manage_token.py generate|set --expires ...`, `manage_token.py expire <expiry|never>`.

**Managing the token — live console:** while `vps_server.py` runs in a terminal,
type `token generate | rotate | set <value> | expire | show | reveal | revoke | help`
right in that console. Changes apply to the running server **immediately (no
restart)** and are saved to the token file. Enabled automatically when stdin is
a TTY (disable with `--no-console`); not available under nohup/systemd. In an
interactive terminal the server may start with no token (every request → 503)
so you can create it from the console; without a terminal it refuses to start.
Offline alternatives: `python vps_server.py --generate-token [--force]` and the
new `PublicForward/ForVPS/manage_token.py`
(`generate [--force]`, `set`, `show [--reveal]`, `revoke`). Stored in
`PublicForward/ForVPS/.api_token` (chmod 600, git-ignored). Resolution order:
`--api-token` flag > env `PAF_API_TOKEN` > token file. Console changes are
live; the offline CLI/flag only take effect on a running server after a restart.

**New files:** `PublicForward/ForVPS/paf_auth.py`,
`PublicForward/ForVPS/manage_token.py`, `tests/test_api_auth.py`.

**Changed files:** `PublicForward/ForVPS/vps_server.py`,
`PublicForward/ForVPS/start.sh`, `PublicForward/start.txt`, `.env.example`,
`.gitignore`, `API_USAGE.md` (§3 rewritten, error table, all examples),
`README.md`, `tests/test_http_e2e.py`, `tests/test_delete_session_e2e.py`
(now send the Bearer token), and the example clients in `example/`
(`--api-token` / env `PAF_API_TOKEN`).

**Upgrade steps:** start the gateway in a terminal → type `token generate` in
its console (no restart needed) → give the token to your users.

---

## [Unreleased] — Grok backend replaces ChatGPT backend entirely (migration)

> **Full architecture + operational deep-dive:** see
> **[`GROK_BACKEND.md`](./GROK_BACKEND.md)**. `CHATGPT_BACKEND.md` has been
> removed — its content is superseded by `GROK_BACKEND.md`.

**Summary:** the ChatGPT backend (third backend slot, chatgpt.com) has been
**fully replaced** by a Grok backend (grok.com), built from an
owner-supplied reference scraper (`grok.js` / `grokdebug.js` /
`login-capture-grok.js`, Puppeteer-based). The proven Cloudflare/anti-bot
CDP mitigation ladder built for ChatGPT (see the retired entries below)
is **reused verbatim** — only naming changed (`CHATGPT_*` → `GROK_*` env
vars, `chatgpt.com` → `grok.com`, `profiles/chatgpt` → `profiles/grok`).

**Removed files:** `config/chatgpt.py`, `scrapers/base_chatgpt.py`,
`scrapers/chatgpt_scraper.py`, `browser_pool_chatgpt.py`,
`public_chatgpt.py`, `login_chatgpt.py`, `import_chatgpt_cookies.py`,
`start_chatgpt_chrome.py`, `CHATGPT_BACKEND.md`,
`tests/_manual_continue_button_check.py` (tested a ChatGPT-only login-form
bug that has no Grok equivalent — see below).

**New files:** `config/grok.py`, `scrapers/base_grok.py`,
`scrapers/grok_scraper.py`, `browser_pool_grok.py`, `public_grok.py`,
`login_grok.py`, `import_grok_cookies.py`, `start_grok_chrome.py`,
`GROK_BACKEND.md`.

**Load-bearing deviation from ChatGPT — auth model:** grok.com
authenticates via SSO (Google / X / Apple / email-link), not
email+password. The owner-supplied reference scraper never automates a
login either — it only ever loads cookies captured from a fully manual
login. `GrokScraper` therefore has **no automated `login()` method at
all**: `ensure_authenticated()` only checks whether the persisted profile
already holds a valid session, and fails loud (with instructions to run
`login_grok.py` or `import_grok_cookies.py`) otherwise. `cookies/
authgrok.json`'s `email`/`password` fields are optional/reserved, not
required.

**Chat-flow logic ported 1:1 from `grok.js`:**
- Composer/send-button selector cascades (grok.com has no stable
  `data-testid`, unlike ChatGPT's `#prompt-textarea` / `[data-testid=
  "send-button"]`) — falls back through generic element types and a
  text/aria-label scan.
- Response-choice A/B dialog handling ("Which response do you prefer?") —
  a Grok-specific UI quirk with no ChatGPT equivalent.
- Richer "still generating" detection (stop button + web-search progress
  text like "Membaca"/"Menelusuri"/"Thinking…", not just a stop button).
- Response-text cleaning regex cascade (strips image credits, search
  status text, timestamps, speed labels, copy/share button text).
- Canvas/code-block extraction (`pre, code, [class*="code"],
  [class*="canvas"]` scan with SHA-256 dedup), in addition to fenced
  ``` code blocks in the plain-text response.

**Gateway (`vps_server.py`) changes:** `MODEL_ID_RE` now accepts
`grok`/`grok(account1)` instead of `chatgpt`/`chatgpt(account1)`; the
`backend == "chatgpt"` dispatch branch renamed to `backend == "grok"`
(same deepseek-style envelope/result shape, unchanged). `public.py
--backend` choices updated to `deepseek`/`qwen`/`grok`.

**Tests updated (all passing after migration):**
- `tests/test_vps_smoke.py`, `tests/test_http_e2e.py` — every
  `chatgpt`/`chatgpt(account1)` case replaced with the `grok` equivalent;
  `test_delete_session_e2e.py` required no changes (backend-agnostic).
- `tests/_manual_login_detection_check.py`,
  `tests/_manual_login_wait_check.py`, `tests/_manual_driver_check.py`,
  `tests/_manual_stealth_check.py`, `tests/_manual_cookie_import_check.py`,
  `tests/_manual_confirm_close_check.py` — all re-pointed at
  `GrokScraper`/`login_grok`/`import_grok_cookies`, HTML stub domains
  changed to `grok.com`, cookie-diagnostic essential-cookie check changed
  from ChatGPT's `__Secure-next-auth.session-token` to Grok's `sso` cookie
  (confirmed from a real grok.com Cookie-Editor export). All ran clean.
- `tests/_manual_continue_button_check.py` **removed** — it tested a
  ChatGPT-only bug (`button:has-text("Continue")` substring-matching
  "Continue with phone number"), which cannot occur in Grok's SSO-only,
  no-login-form model.

**Docs updated:** `README.md`, `API_USAGE.md` — every ChatGPT reference
replaced with the Grok equivalent (routing tables, curl examples, auth
section, error-code table, cross-references to `GROK_BACKEND.md`).
`requirements.txt` comments (`playwright-stealth`/`patchright` — "ChatGPT
backend only" → "Grok backend only").

**Known v1 limitations (see `GROK_BACKEND.md` §8):** no `think_mode`;
attachments support is generic/best-effort (the reference scraper doesn't
cover attachments); `rate_limited` text patterns are best-effort
placeholders pending confirmation against a live grok.com account.

---

## [Retired] — ChatGPT backend (major update, SUPERSEDED BY GROK ABOVE)

> **This backend no longer exists in the codebase.** The entries below are
> kept as the historical engineering record explaining *why* the
> Cloudflare/CDP mitigation ladder — now reused by the Grok backend — was
> built the way it was. For current behavior, see the migration entry
> above and [`GROK_BACKEND.md`](./GROK_BACKEND.md).

**Index of entries below (newest first):**
1. Feature — CDP attach ke Chrome asli yang sudah berjalan (workaround Turnstile terkuat)
2. Feature — Dua jalur baru mengatasi Cloudflare Turnstile: driver Patchright + import cookies manual
3. Fix — `_is_logged_in()` false positive saat login manual belum selesai (popup handling)
4. Feature — `login_chatgpt.py`: helper login manual satu-kali
5. Feature — `playwright-stealth` untuk mask fingerprint CDP controller
6. Feature — Kontingensi Cloudflare Turnstile: opsi `channel="chrome"`
7. Fix — Login ChatGPT salah klik "Continue with phone number" alih-alih "Continue"
8. Fix — Tes gateway yang sudah usang disinkronkan ke API saat ini
9. Feature — Backend ChatGPT ketiga (chat-only + code blocks + attachments) — initial implementation

### Feature — CDP attach ke Chrome asli yang sudah berjalan (workaround Turnstile terkuat)

**File baru:** `start_chatgpt_chrome.py`
**File diubah:** `scrapers/base_chatgpt.py`, `config/chatgpt.py`, `.env.example`
**Latar belakang:** Setelah seluruh jalur sebelumnya (stealth JS →
`channel="chrome"` → patchright → login manual visible → import cookies),
owner melaporkan Turnstile MUNCUL BAHKAN saat mengakses `chatgpt.com`
biasa. Yang tersisa untuk dideteksi oleh Cloudflare adalah jejak
otomasi level-proses yang ditanamkan saat browser DILUNCURKAN oleh
Playwright/patchright (flag `--enable-automation` internal, argumen
`--remote-debugging-pipe`, environment variable, dsb.) — hal yang tidak
bisa dihilangkan dari sisi aplikasi.
**Fix — CDP attach (mode `CHATGPT_CDP_ATTACH=1`):** alih-alih meluncurkan
browser, worker/login helper sekarang bisa ATTACH ke Chrome asli yang
SUDAH BERJALAN lewat `connect_over_cdp()`. Browser-nya adalah Chrome
sungguhan yang memulai dirinya sendiri (di-start via
`start_chatgpt_chrome.py --account account1`, yang meluncurkannya dengan
persistent profile `profiles/chatgpt/<account>/` yang SAMA dengan yang
dipakai worker + `--remote-debugging-port`) — jadi tidak ada fingerprint
peluncuran otomasi yang pernah ada sejak awal. Playwright hanya membaca
DOM / klik lewat koneksi CDP. Mode ini juga otomatis melewati injeksi
stealth JS (browser sudah bersih; patch JS justru menambah artefak).
`close_browser()` dalam mode ini hanya DISCONNECT — Chrome asli tetap
berjalan (lifecycle-nya milik user, dihentikan via
`start_chatgpt_chrome.py --account account1 --stop`).
**Cara pakai:**
```
python start_chatgpt_chrome.py --account account1      # terminal 1 (login manual sekali)
set CHATGPT_CDP_ATTACH=1                               # terminal 2 (Windows)
python public.py --backend chatgpt --vps ws://... --token ...
```
**Verifikasi:** live test di sandbox — chromium asli di-start manual dengan
`--remote-debugging-port`, lalu `ChatGPTScraper` dengan
`CHATGPT_CDP_ATTACH=1` berhasil attach (`connect_over_cdp`), membaca
halaman, dan disconnect bersih tanpa mematikan browser-nya. Semua test
lain (driver, cookie-import, login-detection, popup-wait, continue-button,
confirm-close, smoke, e2e, delete-session) tetap PASS.

### Feature — Dua jalur baru mengatasi Cloudflare Turnstile: driver Patchright + import cookies manual

**File baru:** `import_chatgpt_cookies.py`, `tests/_manual_driver_check.py`,
`tests/_manual_cookie_import_check.py`
**File diubah:** `scrapers/base_chatgpt.py`, `login_chatgpt.py`,
`requirements.txt`, `.env.example`
**Latar belakang:** Turnstile pada `auth.openai.com` muncul TERUS-menerus —
bahkan di browser VISIBLE saat login manual. Ini berarti yang terdeteksi
bukan stealth JS (yang sudah dipakai sejak versi sebelumnya), melainkan
**controller CDP-nya sendiri**: vanilla Playwright meninggalkan jejak
otomasi level-driver (`Runtime.enable` side effects, binding
`getPlaywright`, dsb.) yang TIDAK bisa dihilangkan oleh patch JavaScript
apapun — patch JS justru menambah artefak baru (getter non-native).
**Fix 1 — Patchright (jalur utama):** `base_chatgpt.py` sekarang memilih
driver Playwright-compatible via `_load_async_api()` + env
`CHATGPT_BROWSER_DRIVER` (`auto` default = patchright jika terinstall,
`playwright` untuk memaksa yang lama). [Patchright](https://github.com/Kaliiiiiiiiii-Virtual-Company/patchright)
adalah fork Playwright yang di-patch DI LEVEL DRIVER untuk menghapus
kebocoran CDP tadi — API-nya identik, jadi seluruh backend (scraper, pool,
worker, login helper) memakainya tanpa perubahan kode. Saat driver
patchright aktif, injeksi stealth JS otomatis di-skip (patch driver-level
bisa rusak kalau ditumpuk patch JS). Bisa dikombinasikan dengan
`CHATGPT_BROWSER_CHANNEL=chrome` untuk konfigurasi paling stealth.
Cara pakai: `pip install patchright` + `python -m patchright install chromium`,
lalu jalankan worker/login helper seperti biasa.
**Fix 2 — Import cookies manual (jalur zero-automation):**
`import_chatgpt_cookies.py` men-seed persistent profile dari cookies yang
di-export MANUAL dari browser sehari-hari (ekstensi Cookie-Editor → Export
JSON). Proses login sama sekali tidak menyentuh Playwright/CDP sehingga
tidak ada yang bisa dideteksi Cloudflare. Script meng-inject cookies ke
`profiles/chatgpt/<account>/` (pola yang sama dengan seeding cookie legacy
Qwen, via `cookie_editor_json_to_playwright()`), verifikasi session benar
valid (`_page_is_logged_in()`), menulis sentinel, dan selalu bertanya
sebelum menutup browser. Pesan error actionable untuk semua bentuk export
yang salah (header string, export saat belum login, file hilang).
**Verifikasi:** test driver memastikan patchright ter-resolve dan seluruh
alur stub (login-state check + persistent context) jalan di bawahnya; test
cookie-import mencakup konversi Cookie-Editor (termasuk varian wrapper
`{"cookies": [...]}`) dan pesan error yang jelas untuk export tidak valid.
Semua test lain tetap PASS.

### Fix — `_is_logged_in()` false positive saat login manual belum selesai

**File:** `scrapers/base_chatgpt.py`, `login_chatgpt.py`,
`tests/_manual_login_detection_check.py`, `tests/_manual_login_wait_check.py`
**Bug (dilaporkan langsung dari `login_chatgpt.py`):** setelah menjalankan
`login_chatgpt.py`, script langsung menandai "✅ Login detected" hanya ~9
detik setelah browser terbuka — padahal user belum sempat menyelesaikan
login sama sekali.
**Root cause:** `_is_logged_in()` HANYA mengecek absennya tombol "Log in"
di DOM. Itu benar untuk homepage ChatGPT yang sudah ter-render sepenuhnya,
tapi jadi *false positive* pada halaman APAPUN yang belum/bukan app
ChatGPT — halaman kosong/masih loading tepat setelah `page.goto()`, tab
yang masih di tengah redirect, atau halaman interstitial Cloudflare di
domain lain — karena semua itu juga "tidak punya tombol Log in" (bukan
karena sudah login, tapi karena bukan halaman ChatGPT sama sekali / belum
selesai render).
**Fix (dua lapis):**
1. `_is_logged_in()` sekarang mensyaratkan TIGA hal: (a) URL memuat
   `chatgpt.com`, (b) tombol "Log in" tidak terlihat, DAN (c) konfirmasi
   positif bahwa app shell benar-benar sudah render (`prompt_textarea` atau
   `main_area` ada di DOM) — bukan sekadar absennya tombol Log in. Poin (c)
   dipakai HANYA sebagai konfirmasi sekunder setelah poin (b) lolos, BUKAN
   sebagai sinyal utama menggantikan absennya tombol Log in (tetap
   konsisten dengan catatan desain: homepage logged-out juga menampilkan
   input chat, jadi keberadaan input TIDAK BOLEH jadi satu-satunya sinyal).
2. `login_chatgpt.wait_for_manual_login()` menambah debounce: butuh 2 poll
   berturut-turut ber-hasil True sebelum dianggap sukses (pola "stability
   check" yang sama dipakai `wait_for_response()` di tempat lain pada
   codebase ini), sebagai lapisan pertahanan tambahan terhadap race
   sesaat.
**Verifikasi:** test HTML stub diperluas untuk menyajikan halaman via
`page.route()` pada URL nyata `https://chatgpt.com/` (karena cek domain
baru butuh URL asli, bukan `about:blank` dari `page.set_content()`), plus
2 kasus regresi baru: halaman kosong/loading pada domain chatgpt.com HARUS
tetap dianggap belum-login, dan halaman di luar domain chatgpt.com (mis.
`about:blank`, mid-redirect) juga HARUS tetap dianggap belum-login.

### Feature — `login_chatgpt.py`: helper login manual satu-kali (rekomendasi utama untuk Turnstile)

**File baru:** `login_chatgpt.py`, `tests/_manual_login_wait_check.py`
**Latar belakang:** Setelah `channel="chrome"` dan `playwright-stealth`
tetap belum cukup mengatasi Cloudflare Turnstile untuk sebagian
lingkungan/akun, jalan yang paling andal adalah menghindari flow login
otomatis sama sekali untuk kasus yang terblokir: login **manual satu kali**
di browser visible yang terikat ke persistent profile yang SAMA dengan
yang dipakai worker/pool produksi.
**Cara kerja:** `python login_chatgpt.py --account account1` membuka
browser visible (headless=False) di `profiles/chatgpt/account1/`, lalu
polling `_is_logged_in()` (fungsi yang sama dipakai di semua tempat lain di
backend ini) sampai user selesai login secara manual (termasuk
menyelesaikan checkbox Turnstile sendiri, mengisi email/password, klik
"Continue with password"). Setelah terdeteksi login, sentinel
`cookies_seeded` ditulis dan cookies di-backup (best-effort).
**Mengapa ini menghilangkan masalah Turnstile untuk run berikutnya:**
`ChatGPTScraper.ensure_authenticated()` SELALU mengecek `_is_logged_in()`
LEBIH DULU sebelum pernah memanggil `login()` — jika profile sudah berisi
session valid (dari login manual ini), worker headless berikutnya tidak
pernah menyentuh flow login otomatis sama sekali, sehingga tidak pernah
lagi mengunjungi halaman yang di-challenge Cloudflare.
**Catatan:** akun tetap harus terdaftar namanya di
`cookies/authchatgpt.json` (email/password boleh dikosongkan) agar
`BrowserPool` tahu harus membuat slot untuk akun tersebut — kredensial di
file itu hanya jadi fallback bila session manual ini nanti expired.

### Feature — `playwright-stealth` untuk mask fingerprint CDP controller (bukan cuma UA/headless)

**File:** `scrapers/base_chatgpt.py`, `requirements.txt`
**Latar belakang:** Setelah kontingensi `channel="chrome"` diaktifkan, owner
melaporkan Cloudflare Turnstile **masih** muncul di `auth.openai.com`
meski IP reputasinya bersih dan browser sudah Chrome asli. Root cause:
Cloudflare tidak hanya mengecek User-Agent/headless — ia mendeteksi
**Playwright/CDP sebagai automation controller** lewat sinyal-sinyal lain
(leak `Runtime.enable`, getter properti hasil override yang bukan native,
bentuk `PluginArray` yang salah, mismatch prototype `iframe.contentWindow`,
vendor WebGL, dsb.) yang TIDAK hilang hanya dengan berganti ke Chrome asli
atau patch JS sederhana seperti `_apply_stealth` versi awal.
**Fix:** `_apply_stealth()` sekarang memakai paket
[`playwright-stealth`](https://pypi.org/project/playwright-stealth/)
(port Python dari `puppeteer-extra-plugin-stealth`, ~15 evasion terarah:
`navigator.webdriver`, `navigator.plugins`, `navigator.permissions`,
`navigator.languages`, `iframe.contentWindow`, `chrome.csi`/`chrome.app`/
`chrome.loadTimes`, `webgl_vendor`, `error_prototype`, `sec_ch_ua`, dll.)
sebagai jalur utama — diterapkan di level `BrowserContext` sehingga berlaku
untuk semua page yang dibuat dari context tersebut. Jika paket belum
terinstall (dependency opsional), otomatis fallback ke script kustom
sebelumnya (masih ada, tidak dihapus) agar backend tetap berfungsi.
**Cara pakai:** `pip install -r requirements.txt` sudah mencakup
`playwright-stealth>=2.0.0` — tidak perlu langkah tambahan.
**Catatan realistis:** tidak ada kombinasi stealth-script yang dapat
menjamin 100% lolos dari Cloudflare Turnstile (ini "managed challenge"
yang terus diperbarui providernya) — kombinasi `channel="chrome"` +
`playwright-stealth` + profile persisten + IP bersih adalah best-effort
terbaik yang tersedia untuk automation berbasis CDP. Jika Turnstile tetap
muncul setelah semua ini, satu-satunya jalan pasti adalah menyelesaikan
challenge sekali secara manual dengan `--no-headless` — profile akan
mengingat session tersebut untuk run headless berikutnya.

### Feature — Kontingensi Cloudflare Turnstile: opsi `channel="chrome"` (v1.1, diaktifkan lebih awal)

**File:** `config/chatgpt.py`, `scrapers/base_chatgpt.py`, `.env.example`
**Latar belakang:** Risiko "Cloudflare mendeteksi Chromium headless" yang
sudah didokumentasikan di `design_chatgpt_backend.md` §11 / `implementation.md`
§3 terjadi di deployment nyata — login stuck di halaman
`auth.openai.com` "Performing security verification" (Cloudflare Turnstile
checkbox), bukan di happy-path "Continue with password".
**Fix:** Kontingensi v1.1 yang sudah direncanakan diaktifkan lebih awal:
`launch_browser()` sekarang membaca `CHATGPT_BROWSER_CHANNEL` (env var) atau
`CHATGPT_CONFIG["browser_channel"]` (default `None` = bundled Chromium,
perilaku tidak berubah). Jika di-set ke `"chrome"`, Playwright meluncurkan
**Google Chrome asli** yang terinstall di mesin (`channel="chrome"`) — satu
baris config, tanpa perlu ubah kode lain — karena Chrome asli umumnya lebih
dipercaya Cloudflare Turnstile dibanding binary Chromium headless bawaan.
Stealth script (`_apply_stealth`) juga diperluas: tambah patch
`navigator.permissions.query` untuk notifications (pola stealth umum),
selain patch `webdriver`/`languages`/`plugins` yang sudah ada.
**Cara pakai:** `export CHATGPT_BROWSER_CHANNEL=chrome` di worker host (perlu
`playwright install chrome` atau Chrome sistem sudah terinstall), lalu
jalankan ulang `python public.py --backend chatgpt ...`.
**Catatan tambahan jika Turnstile masih muncul:** Cloudflare juga menilai
reputasi IP (datacenter/VPS IP sering memicu challenge terlepas dari
headless atau tidak) — jika `channel="chrome"` belum cukup, coba jalankan
worker sekali dengan `--no-headless` untuk menyelesaikan checkbox secara
manual (profile akan mengingat session untuk run berikutnya), atau
pertimbangkan menjalankan worker dari IP residensial/non-datacenter.

### Fix — Login ChatGPT salah klik "Continue with phone number" alih-alih "Continue"

**File:** `config/chatgpt.py`, `scrapers/chatgpt_scraper.py`,
`tests/_manual_continue_button_check.py` (baru)
**Root cause:** selector `continue_button` sebelumnya
`button:has-text("Continue")` — di Playwright, `:has-text()` melakukan
**substring match**, jadi ikut match tombol sekunder seperti "Continue with
phone number" / "Continue with Google" / "Continue with Apple". Ditemukan
langsung dari laporan pengguna: email berhasil terisi, tapi yang diklik
malah "Continue with phone number".
**Fix (dua lapis):**
1. Selector diganti ke `button:text-is("Continue")` (exact match,
   whitespace-normalized) sebagai kandidat utama, dengan fallback yang
   secara eksplisit mengecualikan varian "... with ...":
   `button:has-text("Continue"):not(:has-text("with"))`.
2. `_click_continue_button()` (baru, menggantikan `_click_first()` generik
   untuk tombol ini) menambahkan verifikasi runtime: teks elemen yang
   ter-resolve harus PERSIS `"continue"` (case-insensitive, trimmed)
   sebelum diklik — kandidat yang gagal verifikasi dilewati, bukan diklik
   membabi-buta.
**Verifikasi:** `tests/_manual_continue_button_check.py` mereproduksi
skenario persis (tombol "Continue with phone number" diletakkan SEBELUM
tombol "Continue" yang benar di DOM) dan memastikan tombol yang benar yang
diklik.

### Fix — Tes gateway yang sudah usang disinkronkan ke API saat ini

`tests/test_vps_smoke.py` sebelumnya memanggil `V.resolve_backend(...)`, yang
sudah tidak ada di `vps_server.py` (API saat ini hanya punya
`resolve_backend_and_account(model) -> (backend, account_id)`). Diganti
dengan `test_resolve_backend_and_account()` yang sesuai API saat ini +
kasus `chatgpt`/`chatgpt(account1)`.

`tests/test_http_e2e.py` sebelumnya mengirim `model: "deepseek-chat"` untuk
kasus DeepSeek — string ini **tidak match** `MODEL_ID_RE` saat ini
(`^(deepseek|qwen|chatgpt)(?:\(([^)]+)\))?$`), sehingga `chat_completions()`
langsung melempar 400 SEBELUM pernah memanggil `dispatch()`, sementara test
tetap menunggu `ws.recv()` untuk sebuah task envelope yang tidak akan pernah
dikirim — membuat test **hang tanpa batas waktu**. Diperbaiki: model diganti
ke `"deepseek"`, `ws.recv()` diberi timeout eksplisit, dan ditambahkan dua
kasus baru untuk backend `chatgpt` (`"chatgpt"` dan `"chatgpt(account1)"`).

---

### Feature — Backend ChatGPT ketiga (chat-only + code blocks + attachments)

**File baru:** `config/chatgpt.py`, `scrapers/base_chatgpt.py`,
`scrapers/chatgpt_scraper.py`, `browser_pool_chatgpt.py`, `public_chatgpt.py`,
`tests/_manual_login_detection_check.py`
**File diubah (minimal touch):** `config/__init__.py` (re-export),
`public.py` (`--backend` += `chatgpt`), `.env.example` (blok
`CHATGPT_EMAIL`/`CHATGPT_PASSWORD`), `PublicForward/ForVPS/vps_server.py`
(`MODEL_ID_RE` += `chatgpt`, branch `task_fields` untuk `backend=="chatgpt"`,
komentar normalisasi hasil `deepseek | chatgpt`), `tests/test_vps_smoke.py`,
`tests/test_http_e2e.py`, `README.md`, `API_USAGE.md`.

Mengikuti keputusan FINAL di `design_chatgpt_backend.md` /
`implementation.md`:

- **Port penuh ke Python + Playwright**, satu stack dengan deepseek/qwen.
  ChatGPT mengikuti pola persistent-profile email+password milik Qwen
  (`base_qwen.py`), bukan pola cookie-file legacy.
- **Login otomatis email+password** via `cookies/authchatgpt.json` (format
  identik `auth.json`/`authqwen.json`, di-reuse langsung oleh `AuthStore`).
  Tanpa SSO. Flow sesuai screenshot owner: klik "Log in" (POPUP *atau*
  redirect same-tab — keduanya ditangani via listener `context.on("page")`)
  → isi email → klik "Continue" → halaman "Check your inbox" → **klik
  "Continue with password"** (jalur utama, BUKAN mengisi kode verifikasi
  email) → isi password → klik "Continue" → tunggu tombol "Log in" hilang.
- **Deteksi login HANYA via absennya tombol "Log in"** di DOM — BUKAN
  keberadaan input chat, karena homepage logged-out ChatGPT tetap
  menampilkan input "Ask ChatGPT" (poin kritis dari screenshot owner, cermin
  `detectSignOut` di referensi openai.js).
- **Login WAJIB headless=true** (constraint owner, diverifikasi manual
  terhadap flow "Continue with password" yang live).
- **Ekstraksi response**: DOM selector utama
  `[data-message-author-role="assistant"]` (elemen terakhir) → fallback
  innerText `<main>` yang di-anchor pada occurrence TERAKHIR dari prompt,
  dibersihkan (buang label "You said:"/"ChatGPT said:", baris UI seperti
  Copy/Share/Regenerate, dedupe baris berturutan) — port 1:1 strategi
  referensi openai.js.
- **Profile terisolasi**: `profiles/chatgpt/<account>/`, terpisah dari
  `profiles/deepseek/<account>/` dan `profiles/qwen/<account>/` — mencegah
  Chrome `SingletonLock` collision yang sudah pernah diperbaiki untuk
  deepseek/qwen di entri CHANGELOG sebelumnya.
- **Attachments**: `set_input_files` pada `input[type=file]` tersembunyi,
  tunggu preview chip muncul (timeout 15s) sebelum mengirim prompt.
- **Rate limit & rotasi**: deteksi frasa "you've reached"/"usage cap"/"limit
  reached"; mode `new` → rotasi akun via `authchatgpt.json` lalu retry 1x;
  mode `continue` → fail jelas (session terikat akun, rotasi akan
  menghilangkan konteks percakapan — konsisten dengan deepseek/qwen).
- **Gateway**: `model` valid `chatgpt` \| `chatgpt(account1)`. Worker
  register dengan `"backend": "chatgpt"` — routing least-loaded, session
  affinity, `/v1/models`, `x_meta`, dan `DELETE /v1/sessions/{id}` semua
  bekerja otomatis tanpa perubahan tambahan (generik lewat field `backend`).
  Task envelope memakai format deepseek (`{"type":"task","task_id","request"}`)
  karena ini worker baru tanpa protokol lama yang harus dijaga kompatibel.
- **v1 scope**: chat + code blocks + attachments saja. TIDAK ada
  think_mode/model picker (default model UI). Tool calling **diterima**
  di request body tapi **belum dieksekusi** ke UI ChatGPT (ditandai untuk
  v1.1). `stream: true` diabaikan (non-streaming, konsisten dengan deepseek).
- **Regresi**: perilaku backend deepseek & qwen TIDAK berubah. Semua
  perubahan pada file existing bersifat minimal-touch (§F implementation.md).

---

## [Unreleased] — 2026-09-03 (sesi 5)

### Change — Session tidak lagi punya TTL otomatis; dihapus lewat API/console command secara eksplisit

**File:** `public_deepseek.py`, `public_qwen.py`, `PublicForward/ForVPS/vps_server.py`,
`API_USAGE.md`
**Latar belakang:** Sebelumnya session (`X-Session-ID` → conversation_url +
account pin) otomatis dihapus dari memory & disk setelah tidak dipakai
selama `ttl` detik (default 3600s), lewat cek di `SessionStore.get()` +
background loop tiap 60 detik. Tidak ada cara untuk menghapus session
secara eksplisit lewat API — satu-satunya jalan adalah menunggu TTL habis.
Perubahan ini mengganti model tersebut menjadi: **session hidup selamanya
sampai dihapus secara eksplisit**, baik lewat API maupun command manual.

#### 1. TTL otomatis dihapus total dari kedua backend

`SessionStore.get()` (DeepSeek & Qwen) tidak lagi mengecek `is_expired()`
dan menghapus session diam-diam saat diakses. `load_from_disk()` me-restore
SEMUA file session di `dataSession/` tanpa syarat, tidak ada lagi yang
di-skip sebagai "expired". Background loop (`_cleanup_loop` di DeepSeek,
`_status_reporter` di Qwen) tidak lagi memanggil pembersihan session sama
sekali — hanya membersihkan lock idle (housekeeping memory, tidak
menyentuh data session).

```
Sebelum: get(sid) → cek is_expired(ttl) → auto-hapus jika lewat TTL   ❌
         (juga terjadi otomatis tiap 60s lewat background loop)
Sesudah: get(sid) → langsung return dari dict, TIDAK ADA pengecekan
         umur sama sekali. Session hanya hilang lewat aksi eksplisit.  ✅
```

`cleanup_expired()` di kedua `SessionStore` diganti menjadi
`cleanup_older_than(max_age=None)` — method yang sama, tapi sekarang
HARUS dipanggil manual (tidak ada lagi yang memanggilnya otomatis di
manapun). `cleanup_expired()` dipertahankan sebagai alias deprecated
(memanggil `cleanup_older_than()` tanpa argumen) untuk backward-compat.

#### 2. Command console manual "cleanup sessions" / "cleanupsessions"

Karena TTL otomatis dihapus, operator butuh cara manual untuk membersihkan
session yang sudah lama tidak dipakai (mencegah `dataSession/` tumbuh tanpa
batas jika client tidak pernah memanggil delete). Ditambahkan:

- **DeepSeek** (`public_deepseek.py`, console REPL lokal): perintah
  `cleanup sessions [max_age_s]`. Tanpa argumen, pakai nilai `--session-ttl`
  saat start worker sebagai default (nilai itu SEKARANG hanya default untuk
  command manual ini, bukan lagi TTL otomatis).
- **Qwen** (`public_qwen.py`, console REPL lokal): perintah
  `cleanupsessions [max_age_s]` dengan semantik yang sama. Diperlukan
  perubahan signature `_run_console_command(pool, line)` →
  `_run_console_command(pool, worker, line)` agar command ini bisa
  mengakses `worker.processor.sessions`.

#### 3. Endpoint baru: `DELETE /v1/sessions/{session_id}`

**File:** `PublicForward/ForVPS/vps_server.py`

Session sesungguhnya hidup di WORKER (SessionStore per-worker), bukan di
VPS — VPS hanya menyimpan hint routing (`_session_worker`) yang bisa basi
(misal setelah worker restart). Karena itu, `WorkerManager.delete_session()`
**broadcast** pesan `{"type":"delete_session","request_id","session_id"}`
ke SEMUA worker yang terhubung (kedua backend), menunggu balasan
`{"type":"session_deleted","request_id","found"}` dari masing-masing
(future per-request + timeout 8s), lalu agregasi: `deleted = any(found)`.
Hint `_session_worker[session_id]` juga langsung dihapus di VPS, apa pun
hasilnya, supaya request berikutnya untuk `session_id` itu pasti dianggap
baru (tidak coba pakai worker affinity yang basi).

```bash
curl -X DELETE http://VPS_HOST:PORT/v1/sessions/sess-421a9c7e1b2c3d4f
# -> {"session_id": "sess-421a9c7e1b2c3d4f", "deleted": true, "workers_checked": 1}
```

`deleted: false` BUKAN error (idempotent) — cuma berarti session tidak
ditemukan di worker mana pun (sudah terhapus / tidak pernah ada / worker
pemiliknya sedang offline). Tidak ada auth tambahan di endpoint ini,
konsisten dengan endpoint REST lain di gateway ini (lihat `API_USAGE.md`
§3) — belum ada pengecekan token client-facing di gateway ini sama sekali.

Worker menerima pesan `delete_session` lewat message-loop WS yang sudah
ada (tipe pesan baru, paralel dengan `"task"`/`"ping"`), dan membalas
`session_deleted` setelah menghapus dari `SessionStore` (memory + disk).

#### 4. Race condition saat DELETE bersamaan dengan task CONTINUE yang sedang berjalan

**Masalah:** task `mode="continue"` yang sedang berjalan untuk session X,
di akhir eksekusinya akan memanggil `session_store.update()` /
`get_or_create()` untuk menyimpan `conversation_url` terbaru. Jika request
`DELETE` untuk session X datang DI TENGAH task itu berjalan, delete bisa
selesai lebih dulu, lalu task yang masih berjalan "menghidupkan kembali"
session yang baru saja dihapus saat dia selesai dan menulis ulang.

**Fix:** `_handle_delete_session()` (di kedua worker) mengambil **lock
per-session yang sama** yang sudah dipakai untuk serialisasi task
CONTINUE (`_get_session_lock(session_id)`) SEBELUM memanggil
`session_store.delete()`. Ini memaksa delete menunggu task yang sedang
berjalan untuk session tersebut selesai dulu (task itu akan menulis ulang
session seperti biasa), baru kemudian delete benar-benar dieksekusi —
sehingga hasil akhirnya selalu terhapus, tidak pernah "dihidupkan kembali"
oleh task yang tumpang tindih.

```
Sebelum: DELETE bisa balapan dengan task CONTINUE yang masih menulis ulang
         session → session bisa "hidup lagi" setelah dihapus            ❌
Sesudah: DELETE menunggu lock per-session yang sama dipakai task
         CONTINUE → dijamin urut, tidak ada resurrection               ✅
```

#### 5. Dokumentasi: `API_USAGE.md`

Bagian §6.2 (session continuity) mendapat sub-bagian baru **§6.2.1 "Sessions
have NO automatic TTL — you manage their lifetime"** yang menjelaskan:
- Session hidup selamanya sampai dihapus eksplisit.
- Cara implementasi TTL versi client sendiri: simpan timestamp `last_used`
  per `session_id` di sisi klien, panggil `DELETE /v1/sessions/{id}` sendiri
  begitu dianggap basi (atau cukup berhenti memakai ulang `X-Session-ID`
  itu — turn berikutnya otomatis jadi percakapan baru).
- Endpoint `DELETE /v1/sessions/{session_id}` didaftarkan di §4.5 (daftar
  endpoint) dan tabel otentikasi di §3 diperbarui untuk menyertakannya.

#### Test

Ditambahkan `tests/test_delete_session_e2e.py` — uji e2e nyata (uvicorn +
websockets worker asli, pola sama dengan `tests/test_http_e2e.py`):
tidak ada worker terhubung, worker terhubung tapi session tidak dikenal,
worker menemukan & menghapus session (dan memverifikasi hint
`_session_worker` ikut terhapus), serta delete berulang (idempoten). Semua
skenario lolos.

**Catatan:** `tests/test_http_e2e.py` ditemukan hang di sandbox ini —
dikonfirmasi lewat `git stash` bahwa ini adalah masalah environment yang
SUDAH ADA SEBELUM sesi ini (terjadi juga pada `vps_server.py` versi asli,
tidak disebabkan oleh perubahan sesi ini).

---

## [Unreleased] — 2026-09-03 (sesi 4)

### Fix — DeepSeek scraper menangkap output "Thought for N seconds" / partial-stream sebagai jawaban final (5 lapis bug berantai)

**File:** `scrapers/base_deepseek.py`
**Dipicu oleh:** Screenshot + log worker menunjukkan scraper berulang kali gagal
validasi JSON (`JSON parse error: Expecting value: line 1 column 1 (char 0)`) dan
melakukan corrective-retry berulang, kadang berujung proses "stuck" tanpa respons
sama sekali. Investigasi dilakukan bertahap — setiap fix menutup satu jalur
kegagalan sekaligus membuka gejala baru, sampai akhirnya seluruh rantai tertutup.

Semua fix di bawah ini berada di `BaseAIChatScraper._get_last_response_text()`
dan `BaseAIChatScraper.wait_for_response()`.

#### Root Cause (lapis 1) — thought-block ikut terbaca sebagai respons

DeepSeek merender bullet chain-of-thought pada panel **"Thought for N seconds"**
(mode DeepThink/Expert) dengan class DOM (`div.ds-markdown`) yang **sama** dengan
elemen jawaban final. Selama model masih "thinking", elemen jawaban final belum
ada di DOM — `_get_last_response_text()` yang naif mengambil elemen `div.ds-markdown`
**terakhir** di DOM sehingga menangkap baris reasoning, bukan jawaban sesungguhnya.
Teks reasoning ini bukan JSON valid → corrective-retry loop.

```
Sebelum: ambil div.ds-markdown TERAKHIR di DOM, apa pun isinya         ❌
Sesudah: skip elemen yang berada di dalam blok "Thought for N seconds",
         kembalikan "" jika SEMUA kandidat masih di dalam blok thought  ✅
```

Deteksi blok thought dipakai lewat heuristik teks header produk yang selalu
Inggris ("Thought for N seconds…") — bukan class minified yang memang sudah
ditandai "WILL change between builds" di `config/deepseek.py` — dicek pada
`firstElementChild` tiap ancestor (dibatasi panjang teks, bukan `textContent`
penuh) supaya tidak salah tangkap container gabungan thought+jawaban-final,
dengan batas kedalaman ancestor 25 level sebagai margin aman untuk DOM nyata.

#### Root Cause (lapis 2) — timeout tetap trigger retry walau masih "Thinking"

Setelah lapis 1 diperbaiki, `wait_for_response()` benar mengembalikan `""` selama
masih thinking — tapi fungsi ini punya **hard timeout 60 detik** (`response_wait`)
tanpa mengecek apakah model masih aktif memproses. DeepThink + web-search dengan
banyak ronde pencarian bisa berjalan >60 detik secara legitimate. Saat deadline
tercapai padahal model masih "Thinking", kode langsung menganggap gagal dan
mengirim **corrective prompt** — yang justru **memutus/menginterupsi** proses
"Thought for N seconds" yang sedang berjalan di browser, memicu retry-loop.

```
Sebelum: deadline 60s tercapai → langsung timeout → kirim corrective prompt
         (memutus generation yang masih berjalan)                      ❌
Sesudah: deadline tercapai → cek _is_generating() (stop-button /
         loading-indicator / thought-panel tanpa jawaban) → jika masih
         aktif, perpanjang deadline alih-alih menyerah                 ✅
```

#### Root Cause (lapis 3) — deadline diperpanjang tanpa batas saat benar-benar macet

Fix lapis 2 menimbulkan gejala baru: log menunjukkan teks reasoning beku
**persis sama** selama 4+ menit sementara `_is_generating()` terus melaporkan
"masih generating", sehingga deadline diperpanjang berkali-kali menuju
`hard_ceiling` (6x timeout) — dari sisi pengguna terlihat "stuck, tidak ada
respons". Sinyal "masih generating" hanya membuktikan indikator visible di
DOM, bukan bukti ada progres nyata.

```
Sebelum: masih "generating" → perpanjang terus sampai hard_ceiling (~6 menit) ❌
Sesudah: tambahkan stall watchdog — jika teks BENAR-BENAR tidak berubah
         > max(timeout*1.5, 90s), anggap macet (frozen page / backend hang)
         dan berhenti menunggu lebih cepat, sekaligus trigger dump
         diagnostik + screenshot untuk debugging                         ✅
```

#### Root Cause (lapis 4) — jawaban final yang masih di-*stream* tertangkap belum lengkap

Setelah "Thought for N seconds" selesai, DeepSeek men-*stream* jawaban
kata-per-kata/chunk-per-chunk. Mekanisme stability check (`stability_polls=2`,
`poll_interval=0.5s` → hanya butuh teks sama 1 detik) terlalu longgar: jeda
sesaat antar-chunk streaming (mis. baru ketik `"The"` lalu pause) bisa dianggap
"stabil" secara prematur, sehingga teks setengah-jadi lolos dan gagal validasi
JSON → corrective retry dikirim **selagi jawaban asli masih di-stream**.

```
Sebelum: teks sama 1 detik (2 poll) → langsung diterima sebagai final    ❌
         → menangkap "The" (3 karakter) sebagai jawaban lengkap
Sesudah: sebelum menerima "STABLE", cek _is_generating() — jika masih
         streaming, JANGAN terima, terus tunggu                          ✅
```

#### Root Cause (lapis 5) — sinyal `_is_generating()` false-positive permanen memblokir jawaban yang SUDAH selesai

Fix lapis 4 mengekspos bahwa selector `stop_button` / `loading_indicator` di
`config/deepseek.py` (yang memang belum pernah diverifikasi ke DOM asli, masih
bertanda `# TODO: verify`) bisa melaporkan "masih generating" **selamanya**,
bahkan untuk teks yang sudah 100% lengkap dan tidak berubah selama 18+ poll
(~9 detik). Gate lapis 4 yang bersifat hard-block membuat respons yang sudah
tuntas tertahan tanpa akhir.

```
Sebelum: _is_generating()==True → tunggu terus tanpa batas waktu          ❌
Sesudah: _is_generating() jadi sinyal ADVISORY dengan grace period
         terbatas (~5 detik) — jika teks diam > grace period, terima
         apa pun kata sinyal tersebut (anggap basi/tidak reliabel)        ✅
```

#### Test

Ditambahkan 5 skrip verifikasi standalone (semua lolos, tidak saling konflik):
`test_thought_fix.py`, `test_deadline_extension.py`, `test_stall_watchdog.py`,
`test_streaming_gate.py`, `test_stuck_signal_regression.py`.

**Catatan lanjutan:** Selector `stop_button` / `loading_indicator` di
`config/deepseek.py` masih belum terverifikasi terhadap DOM live DeepSeek
(diberi `# TODO: verify` sejak awal). Fix lapis 5 membuat sistem tahan
terhadap kesalahan selector tersebut lewat grace period, tapi bila selector
yang benar berhasil diverifikasi manual (mis. lewat DevTools saat model
sedang "Thinking"), deteksi `_is_generating()` bisa dibuat presisi tanpa
bergantung pada grace period sebagai pengaman.

---

## [Unreleased] — 2026-07-26 (sesi 3)

### Fix — DeepSeek warmup masih diam di `/sign_in` (fix sesi 2 belum menutup celah)

**File:** `scrapers/deepseek_scraper.py`
**Dipicu oleh:** Setelah fix sesi 2 (Fix 1) diterapkan, bug yang sama masih muncul:
browser warmup berhenti di `https://chat.deepseek.com/sign_in` tanpa login terpicu.

#### Root Cause

Fix sesi 2 menambahkan "URL-stabilisation loop" yang menganggap SPA sudah selesai
routing begitu satu sample URL (tiap 0.3s) sama dengan sample sebelumnya. Ini
adalah asumsi yang salah:

```
t=0.0s : goto() selesai (domcontentloaded) → _prev_url = "https://chat.deepseek.com"
t=0.3s : SPA belum sempat redirect ke /sign_in (masih proses cek auth via API)
         → _curr_url masih sama dengan _prev_url → loop mengira "stabil" → break ❌
         (padahal redirect sebenarnya baru terjadi di t=0.5s, SETELAH loop keluar)
```

Karena loop keluar terlalu dini, `ensure_authenticated()` → `is_session_expired()`
dipanggil saat URL **masih** base_url (bukan `/sign_in` yang sebenarnya akan
dituju). Pada momen itu:
- Check 1 (URL == sign_in) → belum match, URL masih base_url
- Check 2 (password field visible) → belum ada, halaman belum selesai render
- Check 3 (chat_input present) → belum ada juga
- Check 4 (phrase match di body) → body masih kosong/loading, tidak match

Hasilnya `is_session_expired()` return `False` ("dikira sudah login") → login
di-skip → slot ditandai READY padahal browser baru mendarat di `/sign_in`
beberapa ratus ms kemudian.

#### Fix di `scrapers/deepseek_scraper.py` (`_ensure_loaded()`)

Ganti pendekatan "tebak dari stabilitas URL" (heuristik, rawan race condition)
dengan **polling langsung ke state akhir yang definitif** — menunggu salah satu
dari tiga kondisi berikut muncul (bounded, maks 10 detik, cek tiap 0.25s):

1. URL sudah mengandung `/sign_in` → session expired
2. Password field terlihat (`input[type="password"]` visible) → login DOM tampil
3. Salah satu selector `chat_input` (dari config) ditemukan → sudah terautentikasi

```
Sebelum: goto() → tunggu 1 sample URL sama dgn sebelumnya → is_session_expired() ← masih bisa fooled ❌
Sesudah: goto() → poll sampai salah satu dari 3 state definitif muncul (maks 10s) → is_session_expired() ✅
```

Deadline di-cap fixed 10 detik (independen dari timeout `page_load` 60 detik)
supaya kasus halaman benar-benar stuck tidak membuat warmup menunggu semenit
penuh per slot.

---

## [Unreleased] — 2026-07-26 (sesi 2)

### Fix 1 — DeepSeek warmup diam di `/sign_in` (false-valid session)

**File:** `scrapers/deepseek_scraper.py`
**Dipicu oleh:** Browser tampil di `https://chat.deepseek.com/sign_in` setelah warmup,
padahal log menunjukkan "Slot ready".

#### Root Cause
`_ensure_loaded()` memanggil `goto(base_url, wait_until="domcontentloaded")` lalu
langsung memanggil `ensure_authenticated()` dengan jeda hanya 0.1s. DeepSeek adalah
React SPA — event `domcontentloaded` fire **sebelum** JS routing selesai. Saat
`is_session_expired()` dipanggil, URL masih `https://chat.deepseek.com` (bukan `/sign_in`),
sehingga semua check lolos → fungsi return `False` (dikira valid). Slot ditandai READY,
padahal browser baru kemudian menyelesaikan redirect ke `/sign_in`.

Ada juga bug kecil di cek `base_url NOT IN page.url`: karena `base_url =
"https://chat.deepseek.com"` adalah substring dari
`"https://chat.deepseek.com/sign_in"`, kondisi skip `goto()` bisa salah terpicu.

#### Fix di `scrapers/deepseek_scraper.py`

1. **Perbaiki cek `on_app`** — ganti substring check menjadi prefix check yang tepat
   sehingga `/sign_in`, `/settings`, dll. tidak dianggap "sudah di app".

2. **Tambah URL-stabilisation loop** — setelah `goto()`, poll URL setiap 0.3s hingga
   URL tidak berubah lagi (maks 5s). Ini memastikan React router sudah selesai
   redirect sebelum `is_session_expired()` dipanggil.

```
Sebelum: goto(base_url) → sleep(0.1s) → is_session_expired() ← SPA belum selesai ❌
Sesudah: goto(base_url) → poll URL sampai stabil (maks 5s) → is_session_expired() ✅
```

---

### Fix 2 — Qwen `showheadless` crash `TargetClosedError` (profile lock)

**File:** `browser_pool_qwen.py`
**Dipicu oleh:** `showheadless <account>` gagal dengan
`BrowserType.launch_persistent_context: Target page, context or browser has been closed`.

#### Root Cause
`restart_slot_no_headless()` memanggil `close_browser()` lalu **langsung**
`launch_browser()` tanpa jeda. `close_browser()` menutup context Playwright secara
async, tapi proses Chrome di OS masih hidup sebentar dan memegang **`SingletonLock`**
pada direktori profile (`profiles/<account>/`). Browser baru yang mencoba membuka
profile yang sama gagal karena lock belum dilepas.

Masalah yang sama ada di `stop_all_no_headless()`.

#### Fix di `browser_pool_qwen.py`

Tambah `await asyncio.sleep(2.0)` setelah `close_browser()` di dua fungsi:
- `restart_slot_no_headless()` — saat toggle ke visible
- `stop_all_no_headless()` — saat kembalikan ke headless

```
Sebelum: close_browser() → launch_browser() ← Chrome masih lock profile ❌
Sesudah: close_browser() → sleep(2.0s) → launch_browser() ✅
```

---

### Fix 3 — Profile collision antar backend (DeepSeek vs Qwen)

**File:** `scrapers/base_deepseek.py`, `scrapers/base_qwen.py`
**Dipicu oleh:** Kedua backend berjalan bersamaan; salah satunya crash atau mengalami
`SingletonLock` error meski account berbeda.

#### Root Cause
Kedua backend menyimpan profile browser ke direktori yang **sama**:
```
PROFILES_DIR / account   →   profiles/account1/
```
Jika DeepSeek worker dan Qwen worker sama-sama punya `account1`, keduanya membuka
`profiles/account1/` secara bersamaan → konflik lock di OS level.

#### Fix

Profile dipisah ke subdirektori per backend:

| Backend | Sebelum | Sesudah |
|---|---|---|
| DeepSeek | `profiles/account1/` | `profiles/deepseek/account1/` |
| Qwen | `profiles/account1/` | `profiles/qwen/account1/` |

> **Migrasi:** Profile lama di `profiles/<account>/` tidak otomatis dipindah.
> Hapus direktori `profiles/` dan biarkan worker login ulang saat pertama kali jalan,
> atau pindah manual: `profiles/account1/` → `profiles/deepseek/account1/` (DeepSeek)
> dan `profiles/qwen/account1/` (Qwen).

---

## [Unreleased] — 2026-07-26 (sesi 1)

### Fix — Round-Robin Account Rotation di Mode `new` (kedua backend)


**File:** `browser_pool_deepseek.py`, `browser_pool_qwen.py`
**Masalah:** Kedua backend tidak merotasi account saat mode `new`. Request selalu
mendapat slot pertama yang idle, yang dalam praktiknya selalu `account1`.

#### Root Cause

**DeepSeek (`browser_pool_deepseek.py`):**
`_acquire_with_account()` di second pass (mode NEW, tanpa `preferred_account`)
melakukan `for slot in self.slots` dan langsung return slot IDLE pertama yang
ditemukan — selalu slot index 0 (`account1`) selama slot itu tidak sedang BUSY.

**Qwen (`browser_pool_qwen.py`):**
`_wait_for_idle_slot()` di Prioritas 3 (mode NEW) menggunakan
`min(idle_slots, key=lambda s: s.last_used)` — memilih slot yang paling lama
idle. Karena semua slot memiliki `last_used` yang hampir sama saat traffic rendah,
slot 0 (`account1`) cenderung selalu terpilih.

#### Fix

Ditambah field `_rr_index: int = 0` di `BrowserPool.__init__()` pada kedua file.
Field ini bertindak sebagai pointer giliran (round-robin cursor).

**Logika baru (identik di kedua backend):**
```
request NEW masuk → iterasi mulai dari _rr_index
  → temukan slot[(_rr_index + i) % n] yang IDLE
  → _rr_index = (index_slot_terpilih + 1) % n   ← geser untuk request berikutnya
  → return slot
```

| Skenario | Sebelum | Sesudah |
|---|---|---|
| 3 account, 3 request NEW berurutan | acc1, acc1, acc1 | acc1, acc2, acc3 |
| acc1 sedang BUSY, ada request NEW | tunggu acc1 | langsung ambil acc2 |
| mode CONTINUE | tidak berubah (pinned ke account asal) | tidak berubah |
| `preferred_account` eksplisit | tidak berubah | tidak berubah |

---

## [Unreleased] — 2026-07-25

### Ringkasan Sesi

Sesi ini mencakup **4 iterasi perbaikan** pada backend Qwen, mulai dari
crash saat startup hingga unifikasi sistem autentikasi agar identik dengan
backend DeepSeek.

---

### Fix 1 — `AttributeError: 'NoneType' object has no attribute 'stem'`

**File:** `browser_pool_qwen.py`  
**Dipicu oleh:** Worker crash saat pertama kali connect ke VPS (`list_accounts()` dipanggil saat register).

#### Root Cause
`BrowserSlot` mendukung dua mode auth:
- **Email+password mode** → `slot.account_name = "account1"`, `slot.cookie_file = None`
- **Legacy cookie-file mode** → `slot.cookie_file = Path(...)`, `slot.account_name = None`

Tiga fungsi (`list_accounts`, `restart_slot_no_headless`, `stop_all_no_headless`) selalu
mengakses `slot.cookie_file.stem` tanpa null-check, menyebabkan crash di email+password mode.

#### Perubahan
- **Ditambah** static helper `_slot_account_name(slot) -> str`:
  - Prioritas: `account_name` → `cookie_file.stem` → `"slot{id}"` (fallback aman)
- **Diperbaiki** `list_accounts()` — ganti `slot.cookie_file.stem` → `_slot_account_name(slot)`
- **Diperbaiki** `restart_slot_no_headless()` — ganti `slot.cookie_file.stem` → `_slot_account_name(slot)`
- **Diperbaiki** `stop_all_no_headless()` — ganti `slot.cookie_file.stem` → `_slot_account_name(slot)`

---

### Fix 2 — `AttributeError: 'NoneType' object has no attribute 'name'`

**File:** `browser_pool_qwen.py`  
**Dipicu oleh:** Error saat task pertama diproses (`acquire()` dipanggil).

#### Root Cause
Masalah serupa Fix 1 namun pada akses `.name` (bukan `.stem`). Empat lokasi di
`acquire()`, `_wait_for_idle_slot()`, dan `get_cookie_path()` mengakses
`slot.cookie_file.name` tanpa null-check.

#### Perubahan
- **Ditambah** static helper `_slot_cookie_name(slot) -> str`:
  - Prioritas: `cookie_file.name` → `account_name + ".json"` → `"slot{id}.json"` (fallback)
  - Dipakai sebagai identifier untuk routing dan session affinity di pool
- **Diperbaiki** `acquire()` — log debug dan yield ganti ke `_slot_cookie_name(slot)`
- **Diperbaiki** `_wait_for_idle_slot()` — matching preferred_cookie ganti ke `_slot_cookie_name(s)`
- **Diperbaiki** `get_cookie_path()` — iterasi slot ganti ke `_slot_cookie_name(slot)`, tambah guard `None`
- **Dihapus** import `discover_cookie_files` yang tidak lagi digunakan

---

### Feature — Unifikasi Auth Qwen → Model DeepSeek (Email+Password)

**File:** `browser_pool_qwen.py`, `public_qwen.py`  
**Latar belakang:** Backend Qwen sebelumnya mendukung dua mode auth yang bisa aktif
bersamaan (email+password via `authqwen.json` *atau* legacy cookie-file). Ini menyebabkan
ambiguitas, code path yang tidak ter-cover, dan tidak konsisten dengan DeepSeek.

#### Tujuan
Menyamakan sepenuhnya alur auth Qwen dengan DeepSeek:
```
authqwen.json → AuthStore → account list
_init_slot()  → QwenScraper(account=...) → launch_browser(account)
              → ensure_authenticated()   → login jika session tidak valid
```

#### Perubahan di `browser_pool_qwen.py`

| Komponen | Sebelum | Sesudah |
|---|---|---|
| `BrowserPool.__init__()` | Deteksi mode (auth.json atau cookie-file) | SELALU baca dari `authqwen.json`; raise `RuntimeError` jika kosong |
| `BrowserPool.start()` | `if _use_auth_mode / else cookie-file` | Satu path: round-robin account dari `authqwen.json` |
| `BrowserPool._init_slot()` | `if account_name / else cookie-file` | Satu path: `QwenScraper(account=...)` → `launch_browser()` → `ensure_authenticated()` |
| `BrowserPool.add_account()` | Terima `cookie_filename: str` (path file) | Terima `account_name: str`; validasi account ada di `authqwen.json` |
| `BrowserPool.restart_slot_no_headless()` | Init ulang via `cookies_path=...` | Init ulang via `account=...` → `ensure_authenticated()` |
| Import | `from scrapers.utils import AuthStore, discover_cookie_files, setup_logger` | `discover_cookie_files` dihapus (tidak dipakai) |

#### Perubahan di `public_qwen.py`

| Komponen | Sebelum | Sesudah |
|---|---|---|
| `Session.cookie_file: Path` | Menyimpan path file cookie | **Diganti** `account_name: str` |
| `SessionStore._to_dict()` | Serialize `cookie_file` sebagai string path | Serialize `account_name` |
| `SessionStore._from_dict()` | `Path(d["cookie_file"])` | Baca `account_name`; backward compat: derive dari `cookie_file` stem jika session lama |
| `SessionStore.create()` | Param `cookie_file: Path` | Param `account_name: str` |
| `SessionStore.get_or_create()` | Param `cookie_file: Path` | Param `account_name: str` |
| `TaskProcessor._run()` | `session_cookie_file: Path` untuk session affinity | `session_account: str` |
| `preferred_cookie` resolution | Dari `existing.cookie_file.name` | Dari `f"{existing.account_name}.json"` |
| CLI `addaccount` | Prompt "Contoh: addaccount account3.json" | Prompt "Contoh: addaccount account3" + info `authqwen.json` |

#### Backward Compatibility
- `BrowserSlot.cookie_file: Path | None` dipertahankan sebagai field opsional
- `_slot_account_name()` dan `_slot_cookie_name()` menangani kedua mode
- `_from_dict()` dapat membaca session lama dari disk yang masih menyimpan format `cookie_file`

#### Format `authqwen.json` (sama dengan `auth.json` DeepSeek)
```json
[
  {"name": "account1", "email": "user@email.com", "password": "secret"},
  {"name": "account2", "email": "user2@email.com", "password": "secret2"}
]
```

---

### Fix 3 — Login tidak terpicu saat warmup + Warning `Cookie 'qwen.json' tidak ditemukan`

**File:** `scrapers/qwen_scraper.py`, `public_qwen.py`  
**Dipicu oleh:** Worker startup berhasil tapi browser tidak melakukan login; setiap request memunculkan warning routing.

#### Bug A — `ensure_authenticated()` tidak trigger login

**Root Cause:**  
Setelah `launch_browser()`, page masih `about:blank` (profile kosong) atau halaman
terakhir yang tersimpan oleh Playwright. `_is_unauthenticated()` hanya memeriksa URL dan
DOM *halaman saat ini* — karena tidak ada tombol Login di `about:blank`, fungsi ini
mengembalikan `False` (dikira sudah login) padahal session belum diverifikasi.

Perbandingan dengan DeepSeek: DeepSeek menavigasi ke `chat.deepseek.com` di
`is_session_expired()` sehingga bisa mendeteksi redirect ke `/sign_in`.

**Fix di `ensure_authenticated()` (`qwen_scraper.py`):**
```
Sebelum: launch_browser → cek DOM (about:blank) → "sudah login" ❌
Sesudah: launch_browser → goto chat.qwen.ai → cek DOM → login jika perlu ✅
```

Logika lengkap:
1. Jika sudah di `chat.qwen.ai` → skip navigate (optimasi untuk re-check)
2. Jika belum → `goto("https://chat.qwen.ai")` + `sleep(1.5s)` untuk SPA render
3. `_is_unauthenticated()` → cek URL patterns dan tombol Login
4. Jika tidak terautentikasi → `login()` → isi form email+password

**Log yang diharapkan setelah fix:**
```
Slot#0: warming up account 'account1' …
ensure_authenticated: navigasi ke chat.qwen.ai (current url: about:blank)
ensure_authenticated: tombol Login/Sign Up terdeteksi → memulai login otomatis
Login ke Qwen sebagai email@gmail.com (account: account1)
Slot#0 ✅ siap (account: account1, auth: email+password via authqwen.json)
```

#### Bug B — `Cookie 'qwen.json' tidak ditemukan di pool`

**Root Cause:**  
Mapping `model → preferred_cookie` dilakukan secara naif:
```python
# Sebelum (salah):
payload["preferred_cookie"] = payload["model"]   # "qwen" → "qwen.json"
```
Model `"qwen"` (tanpa account spesifik) berubah jadi `"qwen.json"` yang tidak cocok
dengan nama slot manapun (slot bernama `"account1.json"`, bukan `"qwen.json"`).

**Fix di `public_qwen.py`:**
```python
# Sesudah (benar):
_m = re.match(r"qwen\(([^)]+)\)", model_val, re.IGNORECASE)
preferred_cookie = _m.group(1) if _m else None
```

| Input model | Sebelum | Sesudah |
|---|---|---|
| `"qwen"` | `"qwen.json"` ❌ | `None` (any slot) ✅ |
| `"qwen-max"` | `"qwen-max.json"` ❌ | `None` (any slot) ✅ |
| `"qwen(account1)"` | `"qwen(account1).json"` ❌ | `"account1"` ✅ |

---

## File yang Dimodifikasi

### Sesi 2026-07-26 (sesi 2)

| File | Fix 1 (DS warmup) | Fix 2 (Qwen lock) | Fix 3 (profile collision) |
|---|:---:|:---:|:---:|
| `scrapers/deepseek_scraper.py` | ✅ | — | — |
| `browser_pool_qwen.py` | — | ✅ | — |
| `scrapers/base_deepseek.py` | — | — | ✅ |
| `scrapers/base_qwen.py` | — | — | ✅ |

### Sesi 2026-07-26 (sesi 1)

| File | Round-Robin Rotation |
|---|:---:|
| `browser_pool_deepseek.py` | ✅ |
| `browser_pool_qwen.py` | ✅ |

### Sesi 2026-07-25

| File | Fix 1 | Fix 2 | Auth Unifikasi | Fix 3 |
|---|:---:|:---:|:---:|:---:|
| `browser_pool_qwen.py` | ✅ | ✅ | ✅ | — |
| `public_qwen.py` | — | — | ✅ | ✅ |
| `scrapers/qwen_scraper.py` | — | — | — | ✅ |

---

## Catatan Migrasi

### Dari legacy cookie-file mode ke auth.json mode

Jika sebelumnya menggunakan cookie files (`cookies/account1.json`, dst.):

1. **Buat `cookies/authqwen.json`** dengan format:
   ```json
   [{"name": "account1", "email": "...", "password": "..."}]
   ```

2. **Hapus session lama** (format tidak kompatibel):
   ```bash
   # Windows
   del dataSession\*.json
   # Linux/Mac
   rm dataSession/*.json
   ```

3. **Hapus profile lama** jika ada (opsional, untuk first-run login bersih):
   ```bash
   rmdir /s /q profiles\account1
   ```

4. **Jalankan ulang worker:**
   ```bash
   python public.py --backend qwen --vps ws://VPS_IP:9000/ws/worker --workers 2
   ```

---

*Terakhir diperbarui: 2026-09-03.*