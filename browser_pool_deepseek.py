"""
browser_pool.py — Enhanced BrowserPool with preferred_account routing & conversation_url navigation.

Each slot owns a logged-in DeepSeekScraper so that incoming tasks never pay the
cold-start cost of launching Chromium + injecting cookies. Dead slots are
respawned (rotating to another account where possible).

NEW FEATURES:
  - preferred_account routing
  - conversation_url navigation (CONTINUE mode)
  - Per-account headless control
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from config import COOKIES_DIR, ROTATION_CONFIG
from scrapers.deepseek_scraper import DeepSeekScraper
from scrapers.utils import get_logger

log = get_logger("paf_deepseek.pool")


class SlotStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    DEAD = "dead"


@dataclass
class BrowserSlot:
    index: int
    scraper: Optional[DeepSeekScraper] = None
    status: SlotStatus = SlotStatus.DEAD
    account: Optional[str] = None
    last_used: float = 0.0
    fail_count: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def mark_busy(self) -> None:
        self.status = SlotStatus.BUSY
        self.last_used = asyncio.get_event_loop().time()

    def mark_idle(self) -> None:
        self.status = SlotStatus.IDLE
        self.last_used = asyncio.get_event_loop().time()

    def mark_dead(self) -> None:
        self.status = SlotStatus.DEAD


class BrowserPool:
    """Manager for N pre-warmed browser slots with enhanced routing."""

    def __init__(
        self,
        num_slots: int = 1,
        headless: bool = True,
        accounts: Optional[list[str]] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self.num_slots = max(1, num_slots)
        self.headless = headless
        self.email = email
        self.password = password
        self._accounts: list[str] = accounts or self._discover_account_names()
        self.slots: list[BrowserSlot] = []
        self._idle_event = asyncio.Event()
        self._started = False
        self._account_headless: dict[str, bool] = {}  # Per-account headless state
        self._rr_index: int = 0  # Round-robin counter untuk mode NEW

    # ------------------------------------------------------------------ #
    # Accounts
    # ------------------------------------------------------------------ #
    @staticmethod
    def _discover_account_names() -> list[str]:
        from scrapers.utils import AuthStore
        from config import AUTH_CONFIG
        names = AuthStore(AUTH_CONFIG["auth_file"]).account_names()
        return names or ["account1"]

    def _account_for_slot(self, index: int) -> Optional[str]:
        if not self._accounts:
            return None
        return self._accounts[index % len(self._accounts)]

    def list_accounts(self) -> list[dict]:
        """
        Return per-slot account info (parity with PAF-ModelQwen).

        Each entry: {"account", "status", "slot_id", "no_headless"}.
        `no_headless` is True when the account's effective headless mode is
        False (i.e. the browser is running visible).
        """
        result: list[dict] = []
        for slot in self.slots:
            effective_headless = self._account_headless.get(slot.account, self.headless)
            result.append(
                {
                    "account":     slot.account or "",
                    "status":      slot.status.value,
                    "slot_id":     slot.index,
                    "no_headless": effective_headless is False,
                }
            )
        return result

    def busy_accounts(self) -> list[str]:
        return [s.account for s in self.slots if s.status == SlotStatus.BUSY and s.account]

    def add_account(self, account_name: str) -> None:
        """Add an account to the pool dynamically (no full restart)."""
        if account_name not in self._accounts:
            self._accounts.append(account_name)
            log.info("Added account to pool: %s", account_name)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._started:
            log.warning("Pool already started")
            return
        self._started = True
        self.slots = [BrowserSlot(index=i) for i in range(self.num_slots)]
        tasks = [self._init_slot(slot) for slot in self.slots]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_idle_event()
        log.info("BrowserPool started: %d slots", self.num_slots)

    async def stop(self) -> None:
        if not self._started:
            return
        log.info("Stopping BrowserPool...")
        tasks = [self._close_slot(slot) for slot in self.slots]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.slots.clear()
        self._started = False

    async def _init_slot(
        self, slot: BrowserSlot, account: Optional[str] = None, headless: Optional[bool] = None
    ) -> None:
        """Initialize a slot with a logged-in browser."""
        if account is None:
            account = self._account_for_slot(slot.index)
        if headless is None:
            headless = self._account_headless.get(account, self.headless)

        try:
            await self._close_slot(slot)
            scraper = DeepSeekScraper(
                headless=headless,
                account=account,
                email=self.email,
                password=self.password,
            )
            await scraper.launch_browser(account)
            # Warm the page
            await scraper._ensure_loaded()
            slot.scraper = scraper
            slot.account = account
            slot.fail_count = 0
            slot.mark_idle()
            log.info("Slot %d ready (account=%s, headless=%s)", slot.index, account, headless)
        except Exception as exc:
            slot.fail_count += 1
            slot.mark_dead()
            log.error("Slot %d failed to init: %s", slot.index, exc)

    async def _close_slot(self, slot: BrowserSlot) -> None:
        """Close browser in slot."""
        if slot.scraper:
            try:
                await slot.scraper.close_browser()
            except Exception as exc:
                log.warning("Error closing slot %d: %s", slot.index, exc)
            slot.scraper = None

    # ------------------------------------------------------------------ #
    # Respawn
    # ------------------------------------------------------------------ #
    def _schedule_respawn(self, slot: BrowserSlot) -> None:
        asyncio.create_task(self._respawn_slot(slot.index))

    async def _respawn_slot(self, index: int, headless: Optional[bool] = None) -> None:
        if index >= len(self.slots):
            return
        slot = self.slots[index]
        log.warning("Respawning dead slot %d", slot.index)
        await self._close_slot(slot)
        await asyncio.sleep(ROTATION_CONFIG["browser_restart_delay"])
        # Rotate to different account if slot failed repeatedly
        account = None
        if slot.fail_count > ROTATION_CONFIG["max_browser_restarts"]:
            current_idx = (
                self._accounts.index(slot.account)
                if slot.account in self._accounts
                else -1
            )
            account = self._accounts[(current_idx + 1) % len(self._accounts)]
            log.info("Rotating slot %d from %s to %s", slot.index, slot.account, account)
        await self._init_slot(slot, account=account, headless=headless)
        self._refresh_idle_event()

    # ------------------------------------------------------------------ #
    # Slot acquisition
    # ------------------------------------------------------------------ #
    async def acquire(self, timeout: float = 120.0) -> BrowserSlot:
        """Acquire any idle slot."""
        return await self._acquire_with_account(timeout=timeout, preferred_account=None)

    async def acquire_pinned(
        self, slot_index: int, timeout: float = 120.0
    ) -> BrowserSlot:
        """
        Acquire a SPECIFIC slot by index.
        Used by CONTINUE mode to guarantee the same browser slot (and therefore
        the same persistent-profile session) as the original Turn 1.
        Falls back to any idle slot if the pinned slot is unavailable.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        if 0 <= slot_index < len(self.slots):
            target = self.slots[slot_index]
            while asyncio.get_event_loop().time() < deadline:
                if target.status == SlotStatus.IDLE:
                    async with target._lock:
                        if target.status == SlotStatus.IDLE:
                            target.mark_busy()
                            return target
                self._idle_event.clear()
                try:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.wait_for(
                        self._idle_event.wait(), timeout=min(remaining, 1.0)
                    )
                except asyncio.TimeoutError:
                    continue
            log.warning(
                "Pinned slot %d unavailable after %.0fs; falling back to any idle slot",
                slot_index, timeout,
            )
        return await self._acquire_with_account(timeout=timeout, preferred_account=None)

    async def release(self, slot: BrowserSlot, reset: bool = True) -> None:
        """
        Release a slot back to the pool.

        Args:
            reset: When True (NEW mode) the slot goes back to IDLE so the next
                   request can reuse it freely.  When False (CONTINUE mode) the
                   browser page stays alive in its current conversation thread,
                   ready for the next turn without a page.goto().
        """
        if slot.status == SlotStatus.BUSY:
            slot.mark_idle()
        self._refresh_idle_event()

    async def _acquire_with_account(
        self, timeout: float = 120.0, preferred_account: Optional[str] = None
    ) -> BrowserSlot:
        """
        Acquire a slot, optionally filtering by account.

        Args:
            preferred_account: If specified, prefer a slot with this account.
                               If none is available, fall back to any idle slot.
        """
        deadline = asyncio.get_event_loop().time() + timeout

        # --- First pass: honour preferred_account -----------------------
        if preferred_account:
            while asyncio.get_event_loop().time() < deadline:
                for slot in self.slots:
                    if slot.status != SlotStatus.IDLE:
                        continue
                    if slot.account != preferred_account:
                        continue
                    async with slot._lock:
                        if slot.status == SlotStatus.IDLE:
                            slot.mark_busy()
                            return slot
                self._idle_event.clear()
                try:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.wait_for(self._idle_event.wait(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    continue
            # preferred account unavailable — fall through to any-slot
            log.warning(
                "No idle slot for preferred_account=%s; using any idle slot",
                preferred_account,
            )

        # --- Second pass: round-robin across idle slots (mode NEW) ------
        # Mulai iterasi dari _rr_index agar beban tersebar merata antar account.
        # Setiap request NEW yang berhasil mendapat slot akan menggeser _rr_index
        # ke slot berikutnya, sehingga request selanjutnya mulai pencarian dari sana.
        deadline2 = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline2:
            n = len(self.slots)
            for i in range(n):
                slot = self.slots[(self._rr_index + i) % n]
                if slot.status != SlotStatus.IDLE:
                    continue
                async with slot._lock:
                    if slot.status == SlotStatus.IDLE:
                        # Geser counter ke slot berikutnya untuk request berikutnya
                        self._rr_index = (self.slots.index(slot) + 1) % n
                        slot.mark_busy()
                        log.debug(
                            "Round-robin NEW: slot %d (account=%s), next_rr=%d",
                            slot.index, slot.account, self._rr_index,
                        )
                        return slot
            self._idle_event.clear()
            try:
                remaining = deadline2 - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                await asyncio.wait_for(self._idle_event.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                continue

        raise TimeoutError(f"No idle slot within {timeout}s")

    def _refresh_idle_event(self) -> None:
        """Signal that at least one slot is idle."""
        if any(s.status == SlotStatus.IDLE for s in self.slots):
            self._idle_event.set()

    # ------------------------------------------------------------------ #
    # URL comparison helper
    # ------------------------------------------------------------------ #
    @staticmethod
    def _urls_match(current_url: str, target_url: str) -> bool:
        """
        Check whether *current_url* points to the same conversation as
        *target_url* using a normalised path comparison.

        FIX: The old bidirectional substring check
          ``current_url in conversation_url or conversation_url in current_url``
        produced false positives when the browser was at the homepage
        (e.g. ``https://chat.deepseek.com/``) because the homepage string
        IS a substring of every conversation URL.

        The new check strips the scheme, host, trailing slashes, and query
        strings, then compares the path components.  Two URLs match only
        when their path portions are identical (or one is empty — meaning
        the browser is on the bare domain, which should NEVER be treated as
        "already at the conversation").
        """
        from urllib.parse import urlparse

        if not current_url or not target_url:
            return False

        cur = urlparse(current_url)
        tgt = urlparse(target_url)

        cur_path = cur.path.rstrip("/")
        tgt_path = tgt.path.rstrip("/")

        # An empty path means the browser is on the bare domain (homepage).
        # This should NEVER match a conversation URL.
        if not cur_path or not tgt_path:
            return False

        return cur_path == tgt_path

    async def _wait_for_spa_ready(
        self,
        scraper: "DeepSeekScraper",
        timeout_ms: int = 10_000,
        conversation_url: Optional[str] = None,
    ) -> None:
        """
        Wait for the DeepSeek React SPA to finish hydrating after a page.goto().

        FIX (Bug #7): After domcontentloaded the SPA still needs to hydrate
        and render existing message history before _count_response_elements() /
        _capture_pre_send_text() is called.

        FIX (Bug #9 / SPA-ready always-timeout): _wait_for_spa_ready was
        wasting ~10 s on every Skip goto() call because the chat-input selector
        apparently doesn't attach reliably once the virtual list is active.
        New strategy: if the chat input is NOT found within timeout_ms AND a
        conversation_url is available, force a hard page.goto() to reset the
        DOM to a clean state, then wait once more. This eliminates the 10 s
        penalty AND guarantees the virtual list is fully rendered before the
        prompt is sent.

        Strategy:
          1. Wait for chat input attached (cheap, fast).
          2. On timeout + conversation_url → force page.goto(), wait again.
          3. On timeout + no URL → log warning, proceed best-effort.
        """
        if scraper.page is None:
            return
        from config import DEEPSEEK_CONFIG
        chat_selectors = DEEPSEEK_CONFIG["selectors"].get("chat_input", [])

        async def _try_find(t_ms: int) -> bool:
            for sel in chat_selectors:
                try:
                    await scraper.page.locator(sel).first.wait_for(
                        state="attached", timeout=t_ms
                    )
                    log.debug("_wait_for_spa_ready: chat input attached (%s)", sel)
                    return True
                except Exception:
                    continue
            return False

        # First attempt.
        if await _try_find(timeout_ms):
            return

        # Timeout — try forced goto if we know the URL.
        if conversation_url:
            log.warning(
                "_wait_for_spa_ready: chat input not found within %dms — "
                "forcing page.goto(%s) to reset DOM",
                timeout_ms, conversation_url,
            )
            try:
                await scraper.page.goto(
                    conversation_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                if await _try_find(timeout_ms):
                    log.info(
                        "_wait_for_spa_ready: chat input found after forced goto()"
                    )
                    return
            except Exception as exc:
                log.warning("_wait_for_spa_ready forced goto() failed: %s", exc)

        log.warning(
            "_wait_for_spa_ready: chat input not found after %dms — "
            "proceeding with possibly incomplete DOM",
            timeout_ms,
        )

    # ------------------------------------------------------------------ #
    # Enhanced run_task with routing & navigation
    # ------------------------------------------------------------------ #
    async def run_task(
        self,
        prompt: str,
        mode: str = "new",
        attachments: Optional[list] = None,
        acquire_timeout: float = 120.0,
        preferred_account: Optional[str] = None,
        conversation_url: Optional[str] = None,
        **send_kwargs,
    ) -> dict:
        """
        Run a single task (scrape). Acquires slot, runs scrape(), releases.

        Features:
          - preferred_account: acquire slot with this account (CONTINUE pinning)
          - conversation_url:  navigate to saved URL before scrape (CONTINUE mode)
            • Skip-goto optimisation: if the browser is already on that URL,
              marks _conversation_started=True and skips page.goto() entirely.
          - reset: CONTINUE turns leave the page alive (reset=False on release)

        FIX (Bug #3): Navigation (goto + SPA-ready wait) now happens BEFORE
        scrape() is called. scrape() → _count_response_elements() therefore
        sees the fully hydrated DOM, and the skip_count baseline is correct.
        """
        t_acquire_start = time.monotonic()
        slot = await self._acquire_with_account(
            timeout=acquire_timeout, preferred_account=preferred_account
        )
        t_acquire_elapsed = time.monotonic() - t_acquire_start
        log.debug(
            "run_task: slot %d acquired in %.2fs (account=%s mode=%s)",
            slot.index, t_acquire_elapsed, slot.account, mode,
        )
        t_nav_start = time.monotonic()

        try:
            # CONTINUE navigation ----------------------------------------
            # OPTIMISATION: t_scrape_start is now set AFTER navigation so
            # that goto + SPA-ready wait are NOT counted in scrape= timing.
            # This gives accurate per-mode latency in the logs.
            if conversation_url and slot.scraper and slot.scraper.page:
                current_url = slot.scraper.page.url or ""
                # Skip-goto optimisation: already on that URL → no reload needed.
                # FIX: Use normalised exact-path comparison instead of
                # bidirectional substring matching. The old check
                #   current_url in conversation_url
                # produced false positives when the browser was at the
                # homepage ("https://chat.deepseek.com/") because that
                # string IS a substring of every conversation URL.
                # After a worker restart, the persistent profile often
                # opens to the homepage, triggering the false positive
                # and skipping the goto entirely → the prompt is sent to
                # a blank new-chat page instead of the saved conversation.
                already_there = self._urls_match(current_url, conversation_url)
                if already_there:
                    log.info(
                        "Skip goto() — slot %d already at conversation URL", slot.index
                    )
                    # Signal scraper that a conversation is already in progress.
                    slot.scraper._conversation_started = True
                    # OPTIMISATION (Bug #9 fix): Skip-goto case uses a much
                    # shorter SPA-ready timeout (2s vs 10s). If the browser is
                    # already on the correct URL the React SPA is virtually
                    # always hydrated; 2s is more than enough to confirm the
                    # chat input is present. The old 10s budget was the main
                    # reason CONTINUE was ~7s slower than NEW.
                    # conversation_url is still passed so a forced goto() can
                    # recover the rare case where the DOM is stale.
                    from config import DEEPSEEK_CONFIG as _cfg
                    _t = _cfg["timeouts"]
                    await self._wait_for_spa_ready(
                        slot.scraper,
                        timeout_ms=_t.get("skip_goto_spa_ready", 2_000),
                        conversation_url=conversation_url,
                    )
                else:
                    log.info(
                        "CONTINUE: navigating slot %d to %s", slot.index, conversation_url
                    )
                    try:
                        await slot.scraper.page.goto(
                            conversation_url,
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                        # FIX (Bug #3): Replace blind sleep(2) with a
                        # deterministic SPA-ready check. Wait for the chat
                        # input to be attached, confirming the React app has
                        # hydrated and rendered existing message history.
                        # Full goto needs the longer budget (10s default).
                        from config import DEEPSEEK_CONFIG as _cfg
                        _t = _cfg["timeouts"]
                        await self._wait_for_spa_ready(
                            slot.scraper,
                            timeout_ms=_t.get("full_goto_spa_ready", 10_000),
                            conversation_url=conversation_url,
                        )
                        slot.scraper._conversation_started = True
                    except Exception as nav_exc:
                        log.warning(
                            "Failed to navigate to conversation URL: %s — "
                            "proceeding without navigation",
                            nav_exc,
                        )

            # Start scrape timer AFTER navigation so goto+SPA-ready cost is
            # reported separately in the navigate= field below.
            t_scrape_start = time.monotonic()

            result = await slot.scraper.scrape(
                prompt, mode=mode, attachments=attachments, **send_kwargs
            )
            t_scrape_elapsed = time.monotonic() - t_scrape_start
            t_nav_elapsed = t_scrape_start - t_nav_start
            t_total_elapsed = time.monotonic() - t_acquire_start

            # Attach the current page URL so public.py can persist it in the session.
            if result.get("ok") and slot.scraper and slot.scraper.page:
                result.setdefault("conversation_url", slot.scraper.page.url)

            log.info(
                "run_task done | slot=%d account=%s mode=%s ok=%s | "
                "acquire=%.2fs navigate=%.2fs scrape=%.2fs total=%.2fs",
                slot.index,
                slot.account,
                mode,
                result.get("ok"),
                t_acquire_elapsed,
                t_nav_elapsed,
                t_scrape_elapsed,
                t_total_elapsed,
            )
            return result
        except Exception as exc:
            log.error("run_task failed on slot %d: %s", slot.index, exc, exc_info=True)
            slot.mark_dead()
            self._schedule_respawn(slot)
            return {"ok": False, "error": str(exc)}
        finally:
            # CONTINUE: keep page alive (reset=False).
            # NEW: standard idle release (reset=True, default behaviour).
            reset = (mode != "continue")
            await self.release(slot, reset=reset)

    async def run_task_with_tool_result(
        self,
        tool_messages: list[dict],
        next_user_msg: Optional[str] = None,
        conversation_url: Optional[str] = None,
        preferred_account: Optional[str] = None,
        acquire_timeout: float = 120.0,
    ) -> dict:
        """
        Turn 2: inject tool results into an existing CONTINUE conversation.

        Acquires the same account slot used for Turn 1, optionally navigates
        back to the conversation URL, then calls
        scraper.scrape_with_tool_result() which builds the structured
        [TOOL RESULT] / [USER REQUEST] prompt and sends it.

        FIX (Bug #3): Same as run_task — sleep(2) replaced with
        _wait_for_spa_ready() so _count_response_elements() inside
        scrape_with_tool_result() sees the correct baseline element count.
        """
        t_acquire_start = time.monotonic()
        slot = await self._acquire_with_account(
            timeout=acquire_timeout, preferred_account=preferred_account
        )
        t_acquire_elapsed = time.monotonic() - t_acquire_start
        log.debug(
            "run_task_with_tool_result: slot %d acquired in %.2fs (account=%s)",
            slot.index, t_acquire_elapsed, slot.account,
        )
        t_nav_start = time.monotonic()

        try:
            # OPTIMISATION: same as run_task — t_scrape_start moved to after
            # navigation so goto+SPA-ready time is excluded from scrape= timing.
            if conversation_url and slot.scraper and slot.scraper.page:
                current_url = slot.scraper.page.url or ""
                # FIX: Use normalised exact-path comparison (same as run_task).
                already_there = self._urls_match(current_url, conversation_url)
                if already_there:
                    log.info(
                        "Tool Turn 2: skip goto() — slot %d already at URL", slot.index
                    )
                    slot.scraper._conversation_started = True
                    # OPTIMISATION: short timeout for skip-goto (same as run_task).
                    from config import DEEPSEEK_CONFIG as _cfg
                    _t = _cfg["timeouts"]
                    await self._wait_for_spa_ready(
                        slot.scraper,
                        timeout_ms=_t.get("skip_goto_spa_ready", 2_000),
                        conversation_url=conversation_url,
                    )
                else:
                    log.info(
                        "Tool Turn 2: navigating slot %d to %s",
                        slot.index, conversation_url,
                    )
                    try:
                        await slot.scraper.page.goto(
                            conversation_url,
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                        # FIX (Bug #3): deterministic SPA-ready wait instead of sleep(2).
                        from config import DEEPSEEK_CONFIG as _cfg
                        _t = _cfg["timeouts"]
                        await self._wait_for_spa_ready(
                            slot.scraper,
                            timeout_ms=_t.get("full_goto_spa_ready", 10_000),
                            conversation_url=conversation_url,
                        )
                        slot.scraper._conversation_started = True
                    except Exception as nav_exc:
                        log.warning("Tool Turn 2 nav failed: %s", nav_exc)

            t_scrape_start = time.monotonic()

            result = await slot.scraper.scrape_with_tool_result(
                tool_messages=tool_messages,
                next_user_msg=next_user_msg,
            )

            t_scrape_elapsed = time.monotonic() - t_scrape_start
            t_nav_elapsed = t_scrape_start - t_nav_start
            t_total_elapsed = time.monotonic() - t_acquire_start

            if result.get("ok") and slot.scraper and slot.scraper.page:
                result.setdefault("conversation_url", slot.scraper.page.url)

            log.info(
                "run_task_with_tool_result done | slot=%d account=%s ok=%s | "
                "acquire=%.2fs navigate=%.2fs scrape=%.2fs total=%.2fs",
                slot.index,
                slot.account,
                result.get("ok"),
                t_acquire_elapsed,
                t_nav_elapsed,
                t_scrape_elapsed,
                t_total_elapsed,
            )
            return result
        except Exception as exc:
            log.error(
                "run_task_with_tool_result failed on slot %d: %s",
                slot.index, exc, exc_info=True,
            )
            slot.mark_dead()
            self._schedule_respawn(slot)
            return {"ok": False, "error": str(exc)}
        finally:
            await self.release(slot, reset=False)  # always keep page alive after Turn 2

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    def status_summary(self) -> dict:
        """Return slot status counts as a dict (parity with PAF-ModelQwen)."""
        counts = {s: 0 for s in SlotStatus}
        for slot in self.slots:
            counts[slot.status] += 1
        return {
            "total": len(self.slots),
            "idle":  counts[SlotStatus.IDLE],
            "busy":  counts[SlotStatus.BUSY],
            "dead":  counts[SlotStatus.DEAD],
        }

    # ------------------------------------------------------------------ #
    # Per-account headless control
    # ------------------------------------------------------------------ #
    async def set_account_headless(self, account_name: str, headless: bool) -> None:
        """
        Set headless mode for a specific account.
        Restarts any slots using this account.
        """
        self._account_headless[account_name] = headless
        log.info("Set headless=%s for account %s", headless, account_name)
        
        # Restart slots with this account
        for slot in self.slots:
            if slot.account == account_name:
                slot.mark_dead()
                await self._respawn_slot(slot.index, headless=headless)