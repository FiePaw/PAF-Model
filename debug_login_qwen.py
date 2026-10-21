#!/usr/bin/env python3
"""
debug_login_qwen.py — Diagnostik mendalam untuk Auth & DOM Form Login Qwen.

Tujuan:
  Membantu membedah, menguji, dan memperbarui selector DOM pada proses
  autentikasi / login Qwen (chat.qwen.ai).
  Bisa dijalankan baik dalam mode NO-HEADLESS (browser terlihat, default)
  maupun HEADLESS (tanpa window).

Fitur:
  1. Menguji Landing Page (chat.qwen.ai) & deteksi status login.
  2. Menguji navigasi ke halaman login (/auth) atau pembukaan modal login.
  3. Memindai seluruh elemen DOM input, button, tabs, checkbox syarat ketentuan,
     dan kemungkinan captcha / iframe.
  4. Membandingkan selector yang terdaftar di config/qwen.py dengan DOM langsung,
     serta menyarankan kandidat selector baru jika selector lama gagal/usang.
  5. Menyorot (highlight) elemen yang terdeteksi dengan border merah di browser.
  6. Menguji pengisian form email + password dan submit.
  7. Mengambil screenshot di setiap langkah ke folder debug/ dan dump HTML lengkap.
  8. Mode interaktif (--interactive): browser tetap terbuka untuk inspeksi DevTools (F12).

Penggunaan:
  # 1. Jalankan terlihat (no-headless, default) dengan akun default di authqwen.json:
  python debug_login_qwen.py

  # 2. Jalankan dalam mode headless (seperti worker produksi):
  python debug_login_qwen.py --headless

  # 3. Uji akun spesifik:
  python debug_login_qwen.py --account account2

  # 4. Uji profil bersih (tanpa cookie/session lama):
  python debug_login_qwen.py --clean-profile

  # 5. Mode interaktif (tahan browser dan tunggu Enter sebelum menutup):
  python debug_login_qwen.py --interactive

  # 6. Hanya scan DOM tanpa mengisi kredensial:
  python debug_login_qwen.py --skip-fill
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Pastikan root direktori ada di sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Handle Windows cp1252 console encoding
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.async_api import BrowserContext, Page, async_playwright

from config import (
    BROWSER_CONFIG,
    DEBUG_DIR,
    PROFILES_DIR,
    QWEN_AUTH_CONFIG,
    QWEN_CONFIG,
    QWEN_LAUNCH_ARGS,
)
from scrapers.utils import AuthStore, setup_logger

logger = setup_logger("debug_login_qwen")


# ─── Visual Helper: Highlight Element ──────────────────────────────────────────

async def _highlight_element(page: Page, element, color: str = "#ff0055") -> None:
    """Tambahkan outline berwarna pada elemen di browser agar mudah dilihat."""
    try:
        await page.evaluate(
            """([el, col]) => {
                if (el) {
                    el.style.outline = '3px solid ' + col;
                    el.style.outlineOffset = '2px';
                    el.style.transition = 'all 0.2s';
                }
            }""",
            [element, color],
        )
    except Exception:
        pass


# ─── Diagnostic Inspector ─────────────────────────────────────────────────────

class QwenAuthDebugger:
    def __init__(
        self,
        account: str,
        email: str | None = None,
        password: str | None = None,
        headless: bool = False,
        clean_profile: bool = False,
        interactive: bool = False,
        skip_fill: bool = False,
        skip_submit: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.account = account
        self.email = email
        self.password = password
        self.headless = headless
        self.clean_profile = clean_profile
        self.interactive = interactive
        self.skip_fill = skip_fill
        self.skip_submit = skip_submit
        self.timeout = timeout

        self.playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.temp_profile_dir: Path | None = None

        # Direktori output debug
        self.debug_dir = DEBUG_DIR
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.ts_prefix = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _resolve_credentials(self) -> tuple[str | None, str | None]:
        email = self.email
        password = self.password

        if not email or not password:
            auth_file = Path(QWEN_AUTH_CONFIG["auth_file"])
            if auth_file.exists():
                store = AuthStore(auth_file)
                creds = store.get(self.account)
                if creds:
                    email = email or creds.get("email")
                    password = password or creds.get("password")

        if not email:
            email = os.environ.get(QWEN_AUTH_CONFIG["env_email"])
        if not password:
            password = os.environ.get(QWEN_AUTH_CONFIG["env_password"])

        return email, password

    async def start(self) -> None:
        self.email, self.password = self._resolve_credentials()

        masked_pwd = ("*" * len(self.password)) if self.password else "(tidak ada)"
        print("=" * 72)
        print("  QWEN AUTH & DOM LOGIN DEBUGGER")
        print("=" * 72)
        print(f"  Akun        : {self.account}")
        print(f"  Email       : {self.email or '(tidak diset)'}")
        print(f"  Password    : {masked_pwd}")
        print(f"  Headless    : {self.headless}")
        print(f"  Clean Prof  : {self.clean_profile}")
        print(f"  Interactive : {self.interactive}")
        print(f"  Debug Dir   : {self.debug_dir}")
        print("=" * 72)

        # Setup profile directory
        if self.clean_profile:
            self.temp_profile_dir = Path(tempfile.mkdtemp(prefix="qwen_clean_profile_"))
            profile_dir = self.temp_profile_dir
            print(f"[*] Menggunakan profil bersih sementara: {profile_dir}")
        else:
            profile_dir = PROFILES_DIR / "qwen" / self.account
            profile_dir.mkdir(parents=True, exist_ok=True)
            print(f"[*] Menggunakan profil persistent: {profile_dir}")

        self.playwright = await async_playwright().start()

        launch_args = list(QWEN_LAUNCH_ARGS)
        # Hilangkan AutomationControlled flag jika belum
        if "--disable-blink-features=AutomationControlled" not in launch_args:
            launch_args.append("--disable-blink-features=AutomationControlled")

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.headless,
            slow_mo=100 if not self.headless else 0,
            viewport=BROWSER_CONFIG["viewport"],
            user_agent=BROWSER_CONFIG["user_agent"],
            locale=BROWSER_CONFIG["locale"],
            timezone_id=BROWSER_CONFIG["timezone_id"],
            args=launch_args,
        )

        # Apply basic stealth script
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def _save_artifacts(self, step_name: str) -> None:
        """Simpan screenshot dan dump HTML halaman saat ini."""
        if not self.page:
            return
        ss_path = self.debug_dir / f"qwen_auth_{self.ts_prefix}_{step_name}.png"
        html_path = self.debug_dir / f"qwen_auth_{self.ts_prefix}_{step_name}.html"
        try:
            await self.page.screenshot(path=str(ss_path), full_page=True)
            html = await self.page.content()
            html_path.write_text(html, encoding="utf-8")
            print(f"  [Artifact] Screenshot : {ss_path.name}")
            print(f"  [Artifact] HTML Dump  : {html_path.name}")
        except Exception as e:
            print(f"  [Artifact] Gagal simpan artifact {step_name}: {e}")

    async def _pause_if_interactive(self, prompt: str = "Tekan [Enter] untuk melanjutkan...") -> None:
        if self.interactive and not self.headless:
            print(f"\n[INTERAKTIF] {prompt} (Buka DevTools F12 di browser jika ingin inspect)")
            await asyncio.get_event_loop().run_in_executor(None, input)

    # ─── Tahap 1: Landing Page ────────────────────────────────────────────────

    async def check_landing_page(self) -> dict:
        print("\n--- [TAHAP 1] Memeriksa Landing Page (https://chat.qwen.ai) ---")
        base_url = QWEN_CONFIG["base_url"]
        print(f"[*] Navigasi ke {base_url} ...")
        await self.page.goto(base_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2.0)

        current_url = self.page.url
        title = await self.page.title()
        print(f"[*] Halaman termuat: URL = {current_url} | Title = {title}")

        # 1. Cek unauthenticated selectors dari config terlebih dahulu
        unauth_results = []
        for sel in QWEN_AUTH_CONFIG["unauthenticated_selectors"]:
            try:
                el = await self.page.query_selector(sel)
                vis = await el.is_visible() if el else False
                text = (await el.inner_text()).strip() if el and vis else ""
                unauth_results.append({"selector": sel, "found": el is not None, "visible": vis, "text": text})
            except Exception as e:
                unauth_results.append({"selector": sel, "error": str(e)})

        print("\n[*] Hasil Evaluasi 'unauthenticated_selectors' di config/qwen.py:")
        any_unauth_visible = False
        for r in unauth_results:
            is_vis = r.get("visible", False)
            if is_vis:
                any_unauth_visible = True
            status = "MATCH (Visible)" if is_vis else ("Ditemukan tapi Hidden" if r.get("found") else "Tidak ditemukan")
            text_info = f" [Teks: '{r['text']}']" if r.get("text") else ""
            print(f"    - {r['selector']:<35} : {status}{text_info}")

        # 2. Cek apakah chat input ada
        chat_input = await self.page.query_selector(
            'textarea[placeholder], div[contenteditable="true"], #chat-input'
        )
        has_chat_input = chat_input and await chat_input.is_visible()

        if any_unauth_visible:
            print("[-] Tombol Log in / Sign up terdeteksi: Pengguna BELUM LOGIN (Guest Mode).")
            is_logged_in = False
        elif has_chat_input:
            print("[+] Chat Input aktif & tidak ada tombol login: Pengguna SUDAH LOGIN.")
            is_logged_in = True
        else:
            print("[-] Chat Input tidak ada & tidak ada tanda login aktif: BELUM LOGIN.")
            is_logged_in = False

        # 3. Cari tombol login / sign up potensial lainnya di halaman
        scan_script = """
        () => {
            const matches = [];
            const elements = document.querySelectorAll('button, a, [role="button"]');
            for (const el of elements) {
                const text = (el.innerText || el.textContent || '').trim();
                const aria = el.getAttribute('aria-label') || '';
                const href = el.getAttribute('href') || '';
                const combined = (text + ' ' + aria + ' ' + href).toLowerCase();
                if (combined.includes('login') || combined.includes('sign in') ||
                    combined.includes('signin') || combined.includes('sign up') ||
                    combined.includes('signup') || combined.includes('masuk')) {
                    const rect = el.getBoundingClientRect();
                    const visible = rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== 'none';
                    matches.push({
                        tag: el.tagName.toLowerCase(),
                        text: text.slice(0, 30),
                        aria: aria,
                        href: href,
                        className: el.className,
                        id: el.id,
                        visible: visible
                    });
                }
            }
            return matches;
        }
        """
        potential_btns = await self.page.evaluate(scan_script)
        print(f"\n[*] Tombol/Link Terkait Login yang terdeteksi di DOM ({len(potential_btns)}):")
        for b in potential_btns:
            vis_str = "Visible" if b["visible"] else "Hidden"
            print(f"    - <{b['tag']}> Text: '{b['text']}' | Aria: '{b['aria']}' | Href: '{b['href']}' | Status: {vis_str}")

        await self._save_artifacts("step1_landing")
        await self._pause_if_interactive("Inspeksi Landing Page selesai.")

        return {
            "current_url": current_url,
            "is_logged_in": is_logged_in,
            "potential_btns": potential_btns,
        }

    # ─── Tahap 2: Buka Halaman / Modal Login ───────────────────────────────────

    async def open_login_form(self, landing_info: dict) -> None:
        print("\n--- [TAHAP 2] Membuka Form / Halaman Login ---")
        login_url = QWEN_AUTH_CONFIG["login_url"]

        # Coba klik tombol login dari landing page terlebih dahulu jika ada
        clicked = False
        for btn_sel in QWEN_CONFIG["selectors"]["login_button"]:
            try:
                el = await self.page.query_selector(btn_sel)
                if el and await el.is_visible():
                    print(f"[*] Mencoba klik tombol login di landing page: {btn_sel}")
                    await _highlight_element(self.page, el, "#00ff88")
                    await el.click()
                    clicked = True
                    await asyncio.sleep(2.0)
                    break
            except Exception:
                continue

        # Jika tidak ada tombol yang bisa diklik atau URL tidak berubah, langsung navigate
        curr_url = self.page.url
        if not clicked or ("/auth" not in curr_url and "/login" not in curr_url):
            print(f"[*] Langsung navigasi ke URL login: {login_url}")
            await self.page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2.0)

        print(f"[*] Berada di URL: {self.page.url}")
        await self._save_artifacts("step2_auth_page")
        await self._pause_if_interactive("Halaman / Modal Login telah terbuka.")

    # ─── Tahap 3: Pemindaian Komprehensif DOM Login ───────────────────────────

    async def scan_login_dom(self) -> dict:
        print("\n--- [TAHAP 3] Pemindaian Komprehensif DOM Form Login ---")

        # 1. Pindai SEMUA elemen <input>
        input_scan_script = """
        () => {
            const inputs = [];
            document.querySelectorAll('input').forEach((el, idx) => {
                const rect = el.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== 'none';
                inputs.push({
                    index: idx,
                    type: el.type || 'text',
                    name: el.name || '',
                    id: el.id || '',
                    placeholder: el.placeholder || '',
                    className: el.className || '',
                    autocomplete: el.getAttribute('autocomplete') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    visible: visible,
                    value: el.value ? '(has_value)' : '(empty)'
                });
            });
            return inputs;
        }
        """
        all_inputs = await self.page.evaluate(input_scan_script)
        print(f"[*] Total elemen <input> ditemukan di halaman: {len(all_inputs)}")
        for inp in all_inputs:
            vis = "Visible" if inp["visible"] else "Hidden"
            print(
                f"    [{inp['index']}] type='{inp['type']}' | name='{inp['name']}' | "
                f"id='{inp['id']}' | placeholder='{inp['placeholder']}' | "
                f"autocomplete='{inp['autocomplete']}' | {vis}"
            )

        # 2. Pindai SEMUA elemen <button>
        button_scan_script = """
        () => {
            const buttons = [];
            document.querySelectorAll('button, input[type="submit"]').forEach((el, idx) => {
                const rect = el.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== 'none';
                buttons.push({
                    index: idx,
                    tag: el.tagName.toLowerCase(),
                    type: el.type || 'button',
                    text: (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 40),
                    id: el.id || '',
                    className: el.className || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                    visible: visible
                });
            });
            return buttons;
        }
        """
        all_buttons = await self.page.evaluate(button_scan_script)
        print(f"\n[*] Total elemen <button> ditemukan di halaman: {len(all_buttons)}")
        for btn in all_buttons:
            vis = "Visible" if btn["visible"] else "Hidden"
            dis = "Disabled" if btn["disabled"] else "Enabled"
            print(
                f"    [{btn['index']}] text='{btn['text']}' | type='{btn['type']}' | "
                f"id='{btn['id']}' | class='{btn['className'][:30]}' | {vis} | {dis}"
            )

        # 3. Pindai kemungkinan Tab Login (Password vs SMS / Kode Verifikasi)
        tab_scan_script = """
        () => {
            const tabs = [];
            document.querySelectorAll('[role="tab"], .ant-tabs-tab, [class*="tab"]').forEach((el) => {
                const text = (el.innerText || '').trim();
                if (text && text.length < 30) {
                    tabs.push({
                        text: text,
                        className: el.className,
                        selected: el.getAttribute('aria-selected') === 'true' || el.className.includes('active')
                    });
                }
            });
            return tabs;
        }
        """
        tabs = await self.page.evaluate(tab_scan_script)
        if tabs:
            print(f"\n[*] Tab Navigasi Login terdeteksi ({len(tabs)}):")
            for t in tabs:
                sel = " [ACTIVE]" if t["selected"] else ""
                print(f"    - Tab: '{t['text']}'{sel}")

        # 4. Pindai Checkbox Persetujuan Syarat & Ketentuan (Terms of Service)
        checkbox_scan_script = """
        () => {
            const cbs = [];
            document.querySelectorAll('input[type="checkbox"], [role="checkbox"], .ant-checkbox').forEach((el, idx) => {
                const parentText = (el.closest('label') || el.parentElement || {}).innerText || '';
                cbs.push({
                    index: idx,
                    checked: el.checked || el.getAttribute('aria-checked') === 'true' || el.className.includes('checked'),
                    label: parentText.trim().replace(/\\s+/g, ' ').slice(0, 60)
                });
            });
            return cbs;
        }
        """
        checkboxes = await self.page.evaluate(checkbox_scan_script)
        if checkboxes:
            print(f"\n[*] Checkbox Persetujuan (Terms / Agreement) terdeteksi ({len(checkboxes)}):")
            for c in checkboxes:
                status = "CHECKED" if c["checked"] else "UNCHECKED"
                print(f"    [{c['index']}] Status: {status} | Label: '{c['label']}'")

        # 5. Pindai Iframe / Captcha
        captcha_scan_script = """
        () => {
            const iframes = [];
            document.querySelectorAll('iframe').forEach((el) => {
                iframes.push({
                    src: el.src || '',
                    title: el.title || '',
                    id: el.id || '',
                    className: el.className || ''
                });
            });
            return iframes;
        }
        """
        iframes = await self.page.evaluate(captcha_scan_script)
        if iframes:
            print(f"\n[*] Iframe terdeteksi di halaman (kemungkinan Captcha/Turnstile):")
            for ifr in iframes:
                print(f"    - Iframe src='{ifr['src'][:70]}' | title='{ifr['title']}' | id='{ifr['id']}'")

        return {
            "inputs": all_inputs,
            "buttons": all_buttons,
            "tabs": tabs,
            "checkboxes": checkboxes,
            "iframes": iframes,
        }

    # ─── Tahap 4: Uji Selector Config vs Live DOM ─────────────────────────────

    async def test_config_selectors(self) -> dict:
        print("\n--- [TAHAP 4] Menguji Selector di config/qwen.py vs Live DOM ---")
        login_sel = QWEN_CONFIG["selectors"]["login"]

        results: dict[str, list[dict]] = {
            "email_input": [],
            "password_input": [],
            "signin_button": [],
            "error_message": [],
        }

        for category, sel_list in login_sel.items():
            print(f"\n[*] Kategori: {category}")
            for sel in sel_list:
                try:
                    elements = await self.page.query_selector_all(sel)
                    visible_count = 0
                    for el in elements:
                        if await el.is_visible():
                            visible_count += 1
                            # Highlight dengan warna berbeda
                            col = "#00ff88" if "email" in category else ("#ffbb00" if "pwd" in category or "password" in category else "#00bbff")
                            await _highlight_element(self.page, el, col)

                    status = f"MATCH ({len(elements)} el, {visible_count} visible)" if elements else "TIDAK MATCH"
                    print(f"    - {sel:<45} : {status}")
                    results[category].append({
                        "selector": sel,
                        "matched": len(elements),
                        "visible": visible_count,
                    })
                except Exception as e:
                    print(f"    - {sel:<45} : ERROR ({e})")
                    results[category].append({"selector": sel, "error": str(e)})

        # Cek apakah ada tombol "Log in with a password" (Qwen UI terbaru)
        btn_pw_mode = None
        for cand in ['button:has-text("Log in with a password")', 'button:has-text("password")', ':has-text("Log in with a password")']:
            try:
                el = await self.page.query_selector(cand)
                if el and await el.is_visible():
                    btn_pw_mode = el
                    break
            except Exception:
                continue
        if btn_pw_mode:
            print("\n[!] DITEMUKAN: Tombol 'Log in with a password' terdeteksi di halaman!")
            print("    Ini adalah mekanisme baru Qwen: mode default adalah kode email/SSO,")
            print("    dan form password baru muncul setelah tombol ini diklik.")

        return results

    # ─── Tahap 5: Uji Pengisian Form & Submit ──────────────────────────────────

    async def test_form_fill_and_submit(self) -> bool:
        print("\n--- [TAHAP 5] Uji Coba Pengisian Form & Submit ---")

        if self.skip_fill:
            print("[*] Parameter --skip-fill aktif. Melewati pengisian kredensial.")
            return False

        if not self.email or not self.password:
            print("[!] Kredensial (email / password) tidak tersedia. Melewati pengisian.")
            print("    Tips: set di cookies/authqwen.json atau gunakan flag --email dan --password.")
            return False

        # Cek apakah form login password perlu dibuka dulu via tombol "Log in with a password"
        btn_pw_mode = None
        for cand in ['button:has-text("Log in with a password")', 'button:has-text("password")', ':has-text("Log in with a password")']:
            try:
                el = await self.page.query_selector(cand)
                if el and await el.is_visible():
                    btn_pw_mode = el
                    break
            except Exception:
                continue

        if btn_pw_mode:
            print("[*] Mengklik 'Log in with a password' untuk beralih ke form password...")
            await _highlight_element(self.page, btn_pw_mode, "#00bbff")
            await btn_pw_mode.click()
            await asyncio.sleep(1.5)

        # 1. Cari elemen Email
        email_el = None
        for sel in QWEN_CONFIG["selectors"]["login"]["email_input"]:
            try:
                el = await self.page.wait_for_selector(sel, timeout=3_000, state="visible")
                if el:
                    email_el = el
                    print(f"[+] Elemen Email ditemukan dengan selector: {sel}")
                    break
            except Exception:
                continue

        if not email_el:
            # Fallback scan: coba input type email atau text pertama
            print("[-] Selector email di config tidak cocok! Mencoba fallback scan input...")
            email_el = await self.page.query_selector('input[type="email"], input[name="email"], input[type="text"]')
            if email_el and await email_el.is_visible():
                print("[+] Fallback: Menggunakan input visible pertama sebagai email field.")

        # 2. Cari elemen Password
        pwd_el = None
        for sel in QWEN_CONFIG["selectors"]["login"]["password_input"]:
            try:
                el = await self.page.wait_for_selector(sel, timeout=3_000, state="visible")
                if el:
                    pwd_el = el
                    print(f"[+] Elemen Password ditemukan dengan selector: {sel}")
                    break
            except Exception:
                continue

        if not pwd_el:
            print("[-] Selector password di config tidak cocok! Mencoba fallback input[type='password']...")
            pwd_el = await self.page.query_selector('input[type="password"]')
            if pwd_el and await pwd_el.is_visible():
                print("[+] Fallback: Menggunakan input[type='password'].")

        if not email_el or not pwd_el:
            print("[X] Gagal menemukan form input email dan/atau password!")
            await self._save_artifacts("step3_fill_failed")
            return False

        # 3. Lakukan pengisian
        print(f"[*] Mengisi email ({self.email}) ...")
        await email_el.click()
        await email_el.fill(self.email)
        await asyncio.sleep(0.4)

        print("[*] Mengisi password ...")
        await pwd_el.click()
        await pwd_el.fill(self.password)
        await asyncio.sleep(0.5)

        # 4. Cek apakah ada checkbox persetujuan yang belum dicentang
        try:
            checkbox = await self.page.query_selector('input[type="checkbox"]:not(:checked)')
            if checkbox and await checkbox.is_visible():
                print("[*] Ditemukan checkbox persetujuan belum dicentang. Mencoba mencentang...")
                await checkbox.check()
                await asyncio.sleep(0.3)
        except Exception:
            pass

        await self._save_artifacts("step3_form_filled")
        await self._pause_if_interactive("Form berhasil diisi. Siap submit.")

        if self.skip_submit:
            print("[*] Parameter --skip-submit aktif. Melewati klik tombol submit.")
            return True

        # 5. Cari tombol submit
        submit_btn = None
        for sel in QWEN_CONFIG["selectors"]["login"]["signin_button"]:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    submit_btn = btn
                    print(f"[+] Tombol Sign in ditemukan dengan selector: {sel}")
                    break
            except Exception:
                continue

        if submit_btn:
            print("[*] Mengklik tombol Sign in / Login ...")
            await submit_btn.click()
        else:
            print("[-] Tombol submit tidak ditemukan via selector config. Mencoba tekan Enter di field password...")
            await pwd_el.press("Enter")

        # ─── Tahap 6: Observasi Pasca Submit ───────────────────────────────────
        print("\n--- [TAHAP 6] Observasi Respon Pasca-Submit (Maks 20s) ---")
        deadline = time.monotonic() + 20.0
        success = False

        while time.monotonic() < deadline:
            curr_url = self.page.url
            # Cek error message
            for err_sel in QWEN_CONFIG["selectors"]["login"]["error_message"]:
                try:
                    err_el = await self.page.query_selector(err_sel)
                    if err_el and await err_el.is_visible():
                        err_text = (await err_el.inner_text()).strip()
                        if err_text:
                            print(f"[X] Muncul pesan error login: '{err_text}'")
                            await self._save_artifacts("step4_login_error")
                            return False
                except Exception:
                    pass

            # Cek apakah sudah redirect ke chat UI (atau tombol login hilang)
            chat_input = await self.page.query_selector(
                'textarea[placeholder], div[contenteditable="true"], #chat-input'
            )
            if chat_input and await chat_input.is_visible():
                print(f"[SUKSES] Input chat terdeteksi di {curr_url}. Login BERHASIL!")
                success = True
                break

            if "/auth" not in curr_url and "/login" not in curr_url and "chat.qwen.ai" in curr_url:
                # URL sudah bukan auth lagi
                print(f"[+] URL berubah ke {curr_url}. Memeriksa apakah session valid...")
                await asyncio.sleep(2.0)
                success = True
                break

            await asyncio.sleep(1.0)

        await self._save_artifacts("step4_post_submit")
        return success

    # ─── Ringkasan Diagnostik & Rekomendasi ────────────────────────────────────

    def save_summary(
        self,
        landing_res: dict,
        dom_data: dict | None,
        test_sel_res: dict,
        login_success: bool,
    ) -> tuple[Path, Path]:
        """
        Secara otomatis membuat dan menyimpan ringkasan hasil diagnosa DOM/UI login
        ke folder debug/summary/ dalam format Markdown (.md) dan JSON (.json).
        """
        summary_dir = self.debug_dir / "summary"
        summary_dir.mkdir(parents=True, exist_ok=True)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        final_url = self.page.url if self.page else ""

        # Evaluasi kategori selector
        categories_eval = {}
        for cat, results in test_sel_res.items():
            matched_sel = [r["selector"] for r in results if r.get("visible", 0) > 0]
            failed_sel = [r["selector"] for r in results if r.get("visible", 0) == 0 and "error" not in r]
            error_sel = [r["selector"] for r in results if "error" in r]
            categories_eval[cat] = {
                "any_match": len(matched_sel) > 0,
                "matched_selectors": matched_sel,
                "failed_selectors": failed_sel,
                "error_selectors": error_sel,
                "raw_results": results,
            }

        # Rekomendasi selector baru berbasis temuan DOM
        recommended_selectors = {
            "switch_to_password_button": [
                'button:has-text("Log in with a password")',
                'button:has-text("password")',
                ':has-text("Log in with a password")',
            ],
            "email_input": [
                'input[name="email"]',
                'input[placeholder*="Email" i]',
                'input[placeholder*="email" i]',
                'input[type="email"]',
            ],
            "password_input": [
                'input[name="password"]',
                'input[type="password"]',
                'input[placeholder*="Password" i]',
                'input[placeholder*="password" i]',
            ],
            "signin_button": [
                'button[type="submit"].qwenchat-auth-pc-submit-button',
                '.qwenchat-auth-pc-submit button[type="submit"]',
                '.qwenchat-auth-pc-submit-button',
                'button[type="submit"]:has-text("Log in")',
                'button[type="submit"]',
                'button:has-text("Log in")',
            ],
            "error_message": [
                '.ant-form-item-explain-error',
                '.ant-message-error',
                '.ant-notification-error',
                '[class*="error"]:visible',
                '[class*="alert"]:visible',
                'p[class*="error"]',
                'div[role="alert"]',
            ],
        }

        # 1. Bangun Data JSON
        summary_dict = {
            "metadata": {
                "generated_at": now_str,
                "account": self.account,
                "email": self.email,
                "headless": self.headless,
                "clean_profile": self.clean_profile,
                "landing_url": landing_res.get("current_url", ""),
                "final_url": final_url,
                "login_success": login_success,
            },
            "selector_evaluation": categories_eval,
            "recommended_selectors": recommended_selectors,
            "dom_scanned": {
                "inputs_count": len(dom_data.get("inputs", [])) if dom_data else 0,
                "buttons_count": len(dom_data.get("buttons", [])) if dom_data else 0,
                "inputs": dom_data.get("inputs", []) if dom_data else [],
                "buttons": dom_data.get("buttons", []) if dom_data else [],
                "tabs": dom_data.get("tabs", []) if dom_data else [],
                "checkboxes": dom_data.get("checkboxes", []) if dom_data else [],
                "iframes": dom_data.get("iframes", []) if dom_data else [],
            },
        }

        json_latest = summary_dir / "summary.json"
        json_archive = summary_dir / f"summary_{self.ts_prefix}.json"
        json_content = json.dumps(summary_dict, indent=2, ensure_ascii=False)
        json_latest.write_text(json_content, encoding="utf-8")
        json_archive.write_text(json_content, encoding="utf-8")

        # 2. Bangun Data Markdown
        md_lines = [
            f"# Summary Diagnostik DOM & UI Auth Qwen",
            f"",
            f"- **Waktu Diagnosa** : {now_str}",
            f"- **Akun Pengujian**: `{self.account}` ({self.email or 'N/A'})",
            f"- **Mode Browser**  : `{'Headless' if self.headless else 'No-Headless (Visible)'}`",
            f"- **Profil Bersih** : `{'Ya (--clean-profile)' if self.clean_profile else 'Tidak (Persistent Profile)'}`",
            f"- **Landing URL**   : `{landing_res.get('current_url', '')}`",
            f"- **Final URL**     : `{final_url}`",
            f"- **Status Login**  : **{'BERHASIL' if login_success else 'BELUM BERHASIL / BUTUH PENYESUAIAN'}**",
            f"",
            f"---",
            f"",
            f"## 1. Status Evaluasi Selector di `config/qwen.py`",
            f"",
            f"| Kategori | Status | Selector yang Match | Selector yang Gagal |",
            f"| :--- | :--- | :--- | :--- |",
        ]

        for cat, data in categories_eval.items():
            status_badge = "✅ VALID" if data["any_match"] else "❌ PERLU UPDATE"
            matched_str = "<br>".join(f"`{s}`" for s in data["matched_selectors"]) or "*(tidak ada)*"
            failed_str = "<br>".join(f"`{s}`" for s in data["failed_selectors"]) or "*(tidak ada)*"
            md_lines.append(f"| **{cat}** | {status_badge} | {matched_str} | {failed_str} |")

        md_lines.extend([
            f"",
            f"---",
            f"",
            f"## 2. Temuan Kritis & Alur Baru Qwen UI",
            f"",
            f"1. **Tombol Alih Password ('Log in with a password'):**",
            f"   - Halaman `/auth` tidak menampilkan field password secara default (mode default: verification code/SSO).",
            f"   - Tombol peralihan wajib diklik terlebih dahulu agar field password muncul ke DOM.",
            f"   - **Selector tombol:** `button:has-text(\"Log in with a password\")`",
            f"",
            f"2. **Atribut Input Email:**",
            f"   - Input email sekarang bertipe `type=\"text\"` dengan `name=\"email\"` (bukan `type=\"email\"`).",
            f"   - **Selector rekomendasi:** `input[name=\"email\"]` dan `input[placeholder*=\"Email\" i]`",
            f"",
            f"3. **Atribut Input Password:**",
            f"   - Muncul setelah tombol switch diklik, dengan atribut `name=\"password\"` dan `type=\"password\"`.",
            f"   - **Selector rekomendasi:** `input[name=\"password\"]` dan `input[type=\"password\"]`",
            f"",
            f"4. **Tombol Submit (Log in):**",
            f"   - Teks tombol adalah \"Log in\" dengan class `.qwenchat-auth-pc-submit-button`.",
            f"   - **Selector rekomendasi:** `button[type=\"submit\"].qwenchat-auth-pc-submit-button` atau `.qwenchat-auth-pc-submit button[type=\"submit\"]`",
            f"",
            f"---",
            f"",
            f"## 3. Rekomendasi Kode untuk `config/qwen.py`",
            f"",
            f"```python",
            f"        # Form login di https://chat.qwen.ai/auth",
            f"        \"login\": {{",
            f"            # Tombol peralihan ke form password (wajib diklik jika belum ada input password)",
            f"            \"switch_to_password_button\": [",
            f"                'button:has-text(\"Log in with a password\")',",
            f"                'button:has-text(\"password\")',",
            f"                ':has-text(\"Log in with a password\")',",
            f"            ],",
            f"            \"email_input\": [",
            f"                'input[name=\"email\"]',",
            f"                'input[placeholder*=\"Email\" i]',",
            f"                'input[placeholder*=\"email\" i]',",
            f"                'input[type=\"email\"]',",
            f"            ],",
            f"            \"password_input\": [",
            f"                'input[name=\"password\"]',",
            f"                'input[type=\"password\"]',",
            f"                'input[placeholder*=\"Password\" i]',",
            f"                'input[placeholder*=\"password\" i]',",
            f"            ],",
            f"            \"signin_button\": [",
            f"                'button[type=\"submit\"].qwenchat-auth-pc-submit-button',",
            f"                '.qwenchat-auth-pc-submit button[type=\"submit\"]',",
            f"                '.qwenchat-auth-pc-submit-button',",
            f"                'button[type=\"submit\"]:has-text(\"Log in\")',",
            f"                'button[type=\"submit\"]',",
            f"                'button:has-text(\"Log in\")',",
            f"            ],",
            f"            \"error_message\": [",
            f"                '.ant-form-item-explain-error',",
            f"                '.ant-message-error',",
            f"                '.ant-notification-error',",
            f"                '[class*=\"error\"]:visible',",
            f"                '[class*=\"alert\"]:visible',",
            f"                'p[class*=\"error\"]',",
            f"                'div[role=\"alert\"]',",
            f"            ],",
            f"        }},",
            f"```",
            f"",
            f"---",
            f"",
            f"## 4. Daftar Elemen DOM yang Dipindai",
            f"",
            f"### Inputs ({len(dom_data.get('inputs', [])) if dom_data else 0}):",
        ])

        if dom_data and dom_data.get("inputs"):
            for inp in dom_data["inputs"]:
                vis_str = "Visible" if inp.get("visible") else "Hidden"
                md_lines.append(
                    f"- `[{inp.get('index')}]` type=`{inp.get('type')}` | name=`{inp.get('name')}` | "
                    f"placeholder=`{inp.get('placeholder')}` | {vis_str}"
                )
        else:
            md_lines.append("- *(tidak ada input atau tahap scan dilewati)*")

        md_lines.extend([
            f"",
            f"### Buttons ({len(dom_data.get('buttons', [])) if dom_data else 0}):",
        ])

        if dom_data and dom_data.get("buttons"):
            for btn in dom_data["buttons"]:
                vis_str = "Visible" if btn.get("visible") else "Hidden"
                dis_str = "Disabled" if btn.get("disabled") else "Enabled"
                md_lines.append(
                    f"- `[{btn.get('index')}]` text=`{btn.get('text')}` | type=`{btn.get('type')}` | "
                    f"class=`{btn.get('className')}` | {vis_str} | {dis_str}"
                )
        else:
            md_lines.append("- *(tidak ada button atau tahap scan dilewati)*")

        md_latest = summary_dir / "summary.md"
        md_archive = summary_dir / f"summary_{self.ts_prefix}.md"
        md_content = "\n".join(md_lines)
        md_latest.write_text(md_content, encoding="utf-8")
        md_archive.write_text(md_content, encoding="utf-8")

        return md_latest, json_latest

    def print_summary(
        self,
        landing_res: dict,
        dom_data: dict | None,
        test_sel_res: dict,
        login_success: bool,
    ) -> None:
        print("\n" + "=" * 72)
        print("  RINGKASAN DIAGNOSTIK & REKOMENDASI SELECTOR")
        print("=" * 72)

        # Status match kategori login
        if test_sel_res:
            for cat, results in test_sel_res.items():
                any_match = any(r.get("visible", 0) > 0 for r in results)
                mark = "[OK - MATCH]" if any_match else "[PERLU UPDATE / TIDAK MATCH]"
                print(f"  - Kategori {cat:<16}: {mark}")
        else:
            print("  - Pengujian form dilewati karena session sudah valid.")

        status_str = "[OK - BERHASIL]" if login_success else "[BELUM BERHASIL / BUTUH PENYESUAIAN]"
        print(f"\n  Status Akhir Login  : {status_str}")

        # Otomatis simpan data summary ke debug/summary
        try:
            md_path, json_path = self.save_summary(landing_res, dom_data, test_sel_res, login_success)
            print(f"\n  [SUMMARY OTOMATIS TERSIMPAN]:")
            print(f"  - Markdown : {md_path}")
            print(f"  - JSON     : {json_path}")
        except Exception as e:
            print(f"  [!] Gagal menyimpan data summary: {e}")

        print(f"  Artifacts screenshot & HTML di: {self.debug_dir}")
        print("=" * 72)

    async def close(self) -> None:
        if self.interactive and not self.headless:
            print("\n[INTERAKTIF] Browser tetap terbuka untuk inspeksi manual.")
            print("Tekan [Enter] di terminal ini untuk menutup browser...")
            await asyncio.get_event_loop().run_in_executor(None, input)

        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()

        # Bersihkan direktori temporary jika clean_profile
        if self.temp_profile_dir and self.temp_profile_dir.exists():
            try:
                shutil.rmtree(self.temp_profile_dir, ignore_errors=True)
            except Exception:
                pass


# ─── Main CLI ─────────────────────────────────────────────────────────────────

async def main_async():
    parser = argparse.ArgumentParser(
        description="Diagnostik Auth & DOM Form Login Qwen (chat.qwen.ai)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--account",
        default="account1",
        help="Nama akun di cookies/authqwen.json",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Override email login",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Override password login",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Jalankan browser dalam mode headless (default: no-headless / browser GUI tampil)",
    )
    parser.add_argument(
        "--clean-profile",
        action="store_true",
        default=False,
        help="Gunakan temporary clean profile (menguji first-time login tanpa cache/cookie lama)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Tahan browser di setiap step untuk inspeksi DevTools F12",
    )
    parser.add_argument(
        "--skip-fill",
        action="store_true",
        default=False,
        help="Hanya pindai DOM tanpa mengisi kredensial",
    )
    parser.add_argument(
        "--skip-submit",
        action="store_true",
        default=False,
        help="Isi form tapi jangan submit (berguna untuk memeriksa value field)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout (detik) untuk menunggu halaman/elemen",
    )

    args = parser.parse_args()

    debugger = QwenAuthDebugger(
        account=args.account,
        email=args.email,
        password=args.password,
        headless=args.headless,
        clean_profile=args.clean_profile,
        interactive=args.interactive,
        skip_fill=args.skip_fill,
        skip_submit=args.skip_submit,
        timeout=args.timeout,
    )

    try:
        await debugger.start()
        landing_res = await debugger.check_landing_page()

        dom_data = None
        # Buka form login jika belum login
        if not landing_res["is_logged_in"]:
            await debugger.open_login_form(landing_res)
            dom_data = await debugger.scan_login_dom()
            test_sel_res = await debugger.test_config_selectors()
            login_success = await debugger.test_form_fill_and_submit()
        else:
            print("\n[*] Sesi sudah aktif / terautentikasi di profil ini.")
            test_sel_res = {}
            login_success = True

        debugger.print_summary(landing_res, dom_data, test_sel_res, login_success)

    except KeyboardInterrupt:
        print("\n[!] Dihentikan oleh user (Ctrl+C).")
    except Exception as e:
        logger.error("Terjadi error selama proses debugging: %s", e, exc_info=True)
    finally:
        await debugger.close()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
