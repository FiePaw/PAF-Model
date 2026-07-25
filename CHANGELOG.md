# CHANGELOG

Semua perubahan penting pada proyek PAF-Model didokumentasikan di sini.  
Format mengikuti [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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

*Changelog ini dibuat pada 2026-07-25 untuk sesi development PAF-Model.*
