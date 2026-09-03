# CHANGELOG

Semua perubahan penting pada proyek PAF-Model didokumentasikan di sini.  
Format mengikuti [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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