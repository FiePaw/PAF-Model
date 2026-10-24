"""
Offline integration tests: the REAL scrape() / scrape_with_tool_result() loops of
the Grok, DeepSeek and Qwen scrapers, with the browser replaced by scripted
stubs (no Playwright launch, no network). Verifies that every backend:

  * wraps prompts with the shared [SYSTEM CONTEXT]/[USER REQUEST] envelope,
  * returns plain-text answers untouched (no JSON requirement),
  * turns @@PAF-CALL blocks into OpenAI tool_calls with byte-exact arguments,
  * sends a short corrective prompt for broken blocks, then gives up cleanly,
  * hands tool results back in a CONTINUE turn.

NOTE: this cannot prove how the live sites render the model's output; it proves
the plumbing around it. See tests/_manual_* for live checks.

Run: python3 tests/test_protocol_scrapers.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Make retry sleeps instant.
_real_sleep = asyncio.sleep


async def _fast_sleep(_delay, *a, **k):
    await _real_sleep(0)


asyncio.sleep = _fast_sleep  # type: ignore[assignment]

from scrapers import tool_protocol as tp  # noqa: E402
from scrapers.deepseek_scraper import DeepSeekScraper  # noqa: E402
from scrapers.grok_scraper import GrokScraper  # noqa: E402
from scrapers.qwen_scraper import QwenScraper  # noqa: E402

TOOLS = [
    {"type": "function", "function": {
        "name": "write_file", "description": "Write a file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "retries": {"type": "integer"}}}}},
]

NASTY = 'say "hi" and \'bye\'\n    indented\\path C:\\x\n\n{"k": 1}\n# heading-like\n- bullet-like'
GOOD_BLOCK = (
    "````\n@@PAF-CALL:write_file@@\n@@PAF-ARG:path@@\na.py\n"
    "@@PAF-ARG:content@@\n" + NASTY + "\n@@PAF-ARG:retries@@\n3\n@@PAF-END@@\n````"
)
BROKEN_BLOCK = "@@PAF-CALL:write_file@@\n@@PAF-ARG:path@@\na.py\n"  # no END

_passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {detail}")
        raise SystemExit(1)
    _passed += 1
    print(f"ok   {name}")


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def args_of(tc):
    return json.loads(tc["function"]["arguments"])


async def _noop(*a, **k):
    return None


# =========================================================================== #
# GROK
# =========================================================================== #
class _Page:
    url = "https://grok.com/c/abc-123"


def make_grok(replies: list[str]):
    g = GrokScraper.__new__(GrokScraper)
    g.account = "acc1"
    g._tools = None
    g._max_tokens = None
    g.logger = logging.getLogger("grok-test")
    g._page = _Page()
    g.sent = []
    g.cleaned_calls = 0

    async def ensure_authenticated():
        return True

    async def send_prompt(prompt, mode="new", **kw):
        g.sent.append((prompt, mode, kw))
        return replies.pop(0)

    async def _extract_response(prompt):
        g.cleaned_calls += 1
        return "CLEANED:" + "plain"

    async def _extract_canvas_blocks():
        return []

    g.ensure_authenticated = ensure_authenticated
    g.send_prompt = send_prompt
    g._extract_response = _extract_response
    g._extract_canvas_blocks = _extract_canvas_blocks
    g.save_to_json = lambda r: None
    g.take_debug_screenshot = _noop
    return g


g = make_grok(["Halo dunia"])
r = run(g.scrape("apa kabar?"))
check("grok: plain chat ok", r["ok"] and r["finish_reason"] == "stop" and "tool_calls" not in r)
check("grok: plain chat text goes through the UI cleaner", r["text"] == "CLEANED:plain" and g.cleaned_calls == 1)
check("grok: prompt wrapped with SYSTEM CONTEXT / USER REQUEST",
      g.sent[0][0].startswith("[SYSTEM CONTEXT]") and "[USER REQUEST]\napa kabar?" in g.sent[0][0]
      and "JSON" in g.sent[0][0] and '"status"' not in g.sent[0][0])
check("grok: usage counted on the outgoing prompt", r["usage"]["prompt_tokens"] > 5)

g = make_grok([GOOD_BLOCK])


async def _boom(prompt):
    raise AssertionError("UI cleaner must NOT run on a tool call (it would corrupt arguments)")


g._extract_response = _boom
r = run(g.scrape("tulis file", tools=TOOLS))
check("grok: tool call -> finish_reason tool_calls", r["ok"] and r["finish_reason"] == "tool_calls" and r["text"] == "")
a = args_of(r["tool_calls"][0])
check("grok: arguments byte-exact (quotes/backslashes/blank lines/markdown)", a["content"] == NASTY, repr(a))
check("grok: schema coercion int", a["retries"] == 3 and a["path"] == "a.py")
check("grok: tools listed in prompt", "write_file" in g.sent[0][0] and "@@PAF-CALL:" in g.sent[0][0])

g = make_grok([BROKEN_BLOCK, GOOD_BLOCK])
g._extract_response = _boom
r = run(g.scrape("tulis file", tools=TOOLS))
check("grok: corrective retry recovers", r["ok"] and r["finish_reason"] == "tool_calls" and len(g.sent) == 2)
check("grok: corrective is sent as CONTINUE on the same /c/ URL",
      g.sent[1][1] == "continue" and g.sent[1][2].get("continue_url") == _Page.url
      and "could not be parsed" in g.sent[1][0] and "[SYSTEM CONTEXT]" not in g.sent[1][0])

g = make_grok([BROKEN_BLOCK, BROKEN_BLOCK, BROKEN_BLOCK])
r = run(g.scrape("tulis file", tools=TOOLS))
check("grok: gives up after MAX_CORRECTIVE_RETRIES", (not r["ok"]) and "invalid_response" in r["error"]
      and len(g.sent) == 1 + tp.MAX_CORRECTIVE_RETRIES)

g = make_grok(["Hasilnya 29C, cerah."])
msgs = [{"role": "tool", "tool_call_id": "call_1", "name": "write_file", "content": '{"ok": true}'}]
r = run(g.scrape("x", mode="continue", continue_url=_Page.url, tools=TOOLS, tool_messages=msgs))
check("grok: tool-result turn is a CONTINUE with [TOOL RESULT]",
      g.sent[0][1] == "continue" and "[TOOL RESULT]" in g.sent[0][0] and "tool_call_id: call_1" in g.sent[0][0])
check("grok: tool-result turn yields final answer", r["ok"] and r["finish_reason"] == "stop")

legacy = json.dumps({"status": "success", "choices": [{"index": 0, "message": {"role": "assistant", "content": "jawaban lama"}}]})
g = make_grok([legacy])
r = run(g.scrape("x"))
check("grok: legacy JSON envelope from an old session is unwrapped", r["ok"] and r["text"] == "jawaban lama")

# =========================================================================== #
# DEEPSEEK
# =========================================================================== #
def make_deepseek(replies: list[str]):
    d = DeepSeekScraper.__new__(DeepSeekScraper)
    d.account = "ds1"
    d._tools = None
    d._max_tokens = None
    d._system_prompt = None
    d._last_prompt = None
    d.page = object()
    d._account_index = 0
    d._browser_restarts = 0
    d.sent = []

    async def launch_browser(*a, **k): return None
    async def _is_page_crashed(): return False
    async def ensure_authenticated(): return True
    async def _capture_pre_send_text(): return ""
    async def _count_response_elements(): return {}
    async def is_rate_limited(): return False

    async def send_prompt(prompt, mode="new", **kw):
        d.sent.append((prompt, mode, kw))
        return "sel"

    async def wait_for_response(**kw):
        return replies.pop(0)

    d.launch_browser = launch_browser
    d._is_page_crashed = _is_page_crashed
    d.ensure_authenticated = ensure_authenticated
    d._capture_pre_send_text = _capture_pre_send_text
    d._count_response_elements = _count_response_elements
    d.is_rate_limited = is_rate_limited
    d.send_prompt = send_prompt
    d.wait_for_response = wait_for_response
    d._response_selectors = lambda: ["x"]
    d.take_debug_screenshot = _noop
    d._profile_dir_for = lambda a: "/tmp/p"
    return d


d = make_deepseek([])
d._tools = TOOLS
d._system_prompt = "Jawab singkat."
p = d._build_wrapped_prompt("halo")
check("deepseek: wrapper uses shared protocol + client system prompt",
      "[SYSTEM CONTEXT]" in p and "@@PAF-CALL:" in p and "Jawab singkat." in p and p.endswith("[USER REQUEST]\nhalo"))

d = make_deepseek(["Jawaban biasa dengan \"kutip\" dan\nbaris baru."])
r = run(d.scrape("halo", mode="new"))
check("deepseek: plain chat ok, text untouched",
      r["ok"] and r["text"] == 'Jawaban biasa dengan "kutip" dan\nbaris baru.' and r["finish_reason"] == "stop" and len(d.sent) == 1)

d = make_deepseek([GOOD_BLOCK])
r = run(d.scrape("tulis", mode="new", tools=TOOLS))
check("deepseek: tool call parsed", r["ok"] and r["finish_reason"] == "tool_calls" and r["text"] == "")
check("deepseek: arguments byte-exact", args_of(r["tool_calls"][0])["content"] == NASTY)

d = make_deepseek([BROKEN_BLOCK, GOOD_BLOCK])
r = run(d.scrape("tulis", mode="new", tools=TOOLS))
check("deepseek: corrective retry recovers",
      r["ok"] and r["finish_reason"] == "tool_calls" and len(d.sent) == 2
      and d.sent[1][1] == "continue" and d.sent[1][2].get("wrap_as_user_request") is False)

d = make_deepseek([""] + ["Setelah dikoreksi: halo"])
r = run(d.scrape("halo", mode="new"))
check("deepseek: empty reply -> neutral corrective, then ok",
      r["ok"] and r["text"].startswith("Setelah dikoreksi") and "empty or unusable" in d.sent[1][0])

d = make_deepseek([GOOD_BLOCK])
msgs = [{"role": "tool", "tool_call_id": "call_1", "name": "write_file", "content": "done"}]
d = make_deepseek(["Selesai, file ditulis."])
r = run(d.scrape_with_tool_result(msgs, None, tools=TOOLS))
check("deepseek: tool-result turn -> final text", r["ok"] and r["finish_reason"] == "stop" and r["text"] == "Selesai, file ditulis.")
check("deepseek: tool-result prompt is CONTINUE + verbatim",
      d.sent[0][1] == "continue" and d.sent[0][2].get("wrap_as_user_request") is False and "[TOOL RESULT]" in d.sent[0][0])

d = make_deepseek([BROKEN_BLOCK, "@@PAF-CALL:write_file@@\n@@PAF-ARG:path@@\nb.py\n@@PAF-ARG:retries@@\n2\n@@PAF-END@@"])
r = run(d.scrape_with_tool_result(msgs, None, tools=TOOLS))
check("deepseek: tool-result turn can chain another call (with corrective)",
      r["ok"] and r["finish_reason"] == "tool_calls" and args_of(r["tool_calls"][0])["retries"] == 2 and len(d.sent) == 2)

d = make_deepseek([BROKEN_BLOCK] * 3)
r = run(d.scrape_with_tool_result(msgs, None, tools=TOOLS))
check("deepseek: tool-result turn gives up cleanly", (not r["ok"]) and "Invalid response after tool result" in r["error"])

# =========================================================================== #
# QWEN
# =========================================================================== #
def make_qwen(replies: list[str]):
    q = QwenScraper.__new__(QwenScraper)
    q.account = None
    q._tools = None
    q._max_tokens = None
    q._think_mode = None
    q._persistent_mode = True
    q._cookie_files = []
    q._cookie_index = 0
    q.cookies_path = None
    q.logger = logging.getLogger("qwen-test")
    q.sent = []

    async def _is_page_crashed(): return False
    async def is_session_expired(): return False

    async def send_prompt(prompt, mode="new", **kw):
        q.sent.append((prompt, mode, kw))
        return replies.pop(0)

    q._is_page_crashed = _is_page_crashed
    q.is_session_expired = is_session_expired
    q.send_prompt = send_prompt
    q._extra_send_kwargs = lambda: {}
    q.take_debug_screenshot = _noop
    return q


q = make_qwen([])
q._tools = TOOLS
p = q._build_wrapped_prompt("halo")
check("qwen: wrapper uses shared protocol", "[SYSTEM CONTEXT]" in p and "@@PAF-CALL:" in p and p.endswith("[USER REQUEST]\nhalo"))
q._tools = None
p = q._build_wrapped_prompt("halo")
check("qwen: plain wrapper has no JSON demand", "Do NOT wrap your answer in JSON" in p and '"status"' not in p)

q = make_qwen(["Ini jawaban biasa.\n```python\nprint(\"x\")\n```"])
r = run(q.scrape("halo", mode="new"))
check("qwen: plain chat ok, untouched", r["success"] and r["response"] == 'Ini jawaban biasa.\n```python\nprint("x")\n```'
      and r["finish_reason"] == "stop" and len(q.sent) == 1)

q = make_qwen([GOOD_BLOCK])
q._tools = TOOLS
r = run(q.scrape("tulis", mode="new"))
check("qwen: tool call parsed", r["success"] and r["finish_reason"] == "tool_calls" and r["response"] is None)
check("qwen: arguments byte-exact + JSON string (OpenAI shape)",
      args_of(r["tool_calls"][0])["content"] == NASTY and isinstance(r["tool_calls"][0]["function"]["arguments"], str))

q = make_qwen([BROKEN_BLOCK, GOOD_BLOCK])
q._tools = TOOLS
r = run(q.scrape("tulis", mode="new"))
check("qwen: corrective retry recovers in the same session",
      r["success"] and r["finish_reason"] == "tool_calls" and len(q.sent) == 2
      and q.sent[1][1] == "continue" and q.sent[1][2].get("wrap_as_user_request") is False)

q = make_qwen([legacy])
r = run(q.scrape("x", mode="new"))
check("qwen: legacy JSON envelope unwrapped", r["success"] and r["response"] == "jawaban lama")

msgs = [{"role": "tool", "tool_call_id": "call_1", "name": "write_file", "content": "done"}]
q = make_qwen(["Selesai."])
q._tools = TOOLS
r = run(q.scrape_with_tool_result(msgs))
check("qwen: tool-result turn -> final", r["success"] and r["finish_reason"] == "stop" and r["response"] == "Selesai."
      and q.sent[0][1] == "continue" and "[TOOL RESULT]" in q.sent[0][0])

q = make_qwen([BROKEN_BLOCK, GOOD_BLOCK])
q._tools = TOOLS
r = run(q.scrape_with_tool_result(msgs))
check("qwen: tool-result turn corrective -> chained call", r["success"] and r["finish_reason"] == "tool_calls")

q = make_qwen([BROKEN_BLOCK] * 3)
q._tools = TOOLS
r = run(q.scrape_with_tool_result(msgs))
check("qwen: tool-result turn gives up cleanly", (not r["success"]) and "Invalid response after tool result" in r["error"])

print(f"\nALL {_passed} CHECKS PASSED")
