"""
Offline tests for scrapers/tool_protocol.py (no browser, no network).

Run: python3 tests/test_tool_protocol.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the module file directly so the test does not need playwright etc.
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "tool_protocol",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scrapers", "tool_protocol.py"),
)
tp = importlib.util.module_from_spec(_spec)
sys.modules["tool_protocol"] = tp
_spec.loader.exec_module(tp)

TOOLS = [
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean"},
            "retries": {"type": "integer"},
            "options": {"type": "object"},
        }}}},
    {"type": "function", "function": {
        "name": "get_time", "description": "time", "parameters": {"type": "object", "properties": {}}}},
]

_passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {detail}")
        raise SystemExit(1)
    _passed += 1
    print(f"ok   {name}")


# --------------------------------------------------------------------------- #
# Plain chat
# --------------------------------------------------------------------------- #
out = tp.parse_model_output("Halo! Ini jawaban biasa.", tools=None)
check("plain chat is final text", out.kind == "final" and out.text == "Halo! Ini jawaban biasa.")

code_answer = 'Gunakan:\n```python\nprint("hi \\n there")\n```\nSelesai, "kutip" dan \\ backslash.'
out = tp.parse_model_output(code_answer)
check("plain chat keeps quotes/backslash/code untouched", out.kind == "final" and out.text == code_answer)

out = tp.parse_model_output("   \n  ")
check("empty reply is invalid", out.kind == "invalid" and "empty" in out.error)

out = tp.parse_model_output("Sebut format @@PAF-CALL:x@@ ... @@PAF-END@@ sebagai teks", tools=None)
check("markers are plain text when calls are not enabled", out.kind == "final")

# --------------------------------------------------------------------------- #
# Tool calls — the cases that used to break JSON
# --------------------------------------------------------------------------- #
nasty = 'line1 "quoted" and \'single\'\n    indented\\path C:\\Users\\x\n\n{"json": "inside"}\n# not a heading\n- not a bullet'
block = (
    "````\n@@PAF-CALL:write_file@@\n@@PAF-ARG:path@@\nsrc/main.py\n"
    "@@PAF-ARG:content@@\n" + nasty + "\n@@PAF-END@@\n````"
)
out = tp.parse_model_output(block, tools=TOOLS)
check("fenced call parses", out.kind == "tool_calls" and len(out.tool_calls) == 1, str(out))
tc = out.tool_calls[0]
args = json.loads(tc["function"]["arguments"])
check("openai shape", tc["type"] == "function" and tc["id"].startswith("call_")
      and isinstance(tc["function"]["arguments"], str))
check("path arg", args["path"] == "src/main.py")
check("content arg is byte-exact (quotes, backslashes, blank lines, markdown)", args["content"] == nasty, repr(args["content"]))

unfenced = "@@PAF-CALL:write_file@@\n@@PAF-ARG:path@@\na.txt\n@@PAF-ARG:content@@\nhello\n@@PAF-END@@"
out = tp.parse_model_output(unfenced, tools=TOOLS)
check("unfenced call parses", out.kind == "tool_calls"
      and json.loads(out.tool_calls[0]["function"]["arguments"]) == {"path": "a.txt", "content": "hello"})

chrome = "text\nCopy code\n" + unfenced
out = tp.parse_model_output(chrome, tools=TOOLS)
check("UI chrome / preamble before the block is ignored", out.kind == "tool_calls")

out = tp.parse_model_output("@@PAF-CALL:get_time@@\n@@PAF-END@@", tools=TOOLS)
check("no-arg call -> {}", out.kind == "tool_calls" and out.tool_calls[0]["function"]["arguments"] == "{}")

two = unfenced + "\n" + "@@PAF-CALL:get_time@@\n@@PAF-END@@"
out = tp.parse_model_output(two, tools=TOOLS)
check("multiple calls", out.kind == "tool_calls" and len(out.tool_calls) == 2
      and out.tool_calls[1]["function"]["name"] == "get_time")
check("call ids are unique", out.tool_calls[0]["id"] != out.tool_calls[1]["id"])

typed = ("@@PAF-CALL:write_file@@\n@@PAF-ARG:path@@\nx\n@@PAF-ARG:overwrite@@\nTrue\n"
         "@@PAF-ARG:retries@@\n3\n@@PAF-ARG:options@@\n{\"a\": [1, 2]}\n@@PAF-END@@")
out = tp.parse_model_output(typed, tools=TOOLS)
a = json.loads(out.tool_calls[0]["function"]["arguments"])
check("schema-based coercion (bool/int/object)", a["overwrite"] is True and a["retries"] == 3 and a["options"] == {"a": [1, 2]}, str(a))

bare_json = '@@PAF-CALL:write_file@@\n{"path": "p", "content": "c"}\n@@PAF-END@@'
out = tp.parse_model_output(bare_json, tools=TOOLS)
check("bare JSON body tolerated", out.kind == "tool_calls"
      and json.loads(out.tool_calls[0]["function"]["arguments"]) == {"path": "p", "content": "c"})

# Content that itself contains a fenced code block (outer fence uses 4 backticks).
inner = "````\n@@PAF-CALL:write_file@@\n@@PAF-ARG:content@@\n```python\nprint(1)\n```\n@@PAF-END@@\n````"
out = tp.parse_model_output(inner, tools=TOOLS)
check("content with inner ``` fence survives",
      json.loads(out.tool_calls[0]["function"]["arguments"])["content"] == "```python\nprint(1)\n```")

# --------------------------------------------------------------------------- #
# Invalid blocks -> retryable
# --------------------------------------------------------------------------- #
out = tp.parse_model_output("@@PAF-CALL:write_file@@\n@@PAF-ARG:path@@\nx\n", tools=TOOLS)
check("unterminated block is invalid", out.kind == "invalid" and "not terminated" in out.error)

out = tp.parse_model_output("@@PAF-CALL:delete_everything@@\n@@PAF-END@@", tools=TOOLS)
check("unknown function is invalid", out.kind == "invalid" and "unknown function" in out.error)

out = tp.parse_model_output("@@PAF-CALL:write_file@@\nsome prose without args\n@@PAF-END@@", tools=TOOLS)
check("body without ARG markers is invalid", out.kind == "invalid")

# expect_calls (tool-result turn, tools not resent): no name validation
out = tp.parse_model_output("@@PAF-CALL:whatever@@\n@@PAF-END@@", tools=None, expect_calls=True)
check("expect_calls parses without a tools list", out.kind == "tool_calls")

# --------------------------------------------------------------------------- #
# Legacy JSON envelopes (sessions started under the old protocol)
# --------------------------------------------------------------------------- #
legacy_ok = json.dumps({"status": "success", "choices": [{"index": 0, "message": {"role": "assistant", "content": "jawaban lama"}, "finish_reason": "stop"}]})
out = tp.parse_model_output(legacy_ok)
check("legacy success envelope unwrapped", out.kind == "final" and out.text == "jawaban lama" and out.legacy)

fenced_legacy = "```json\n" + legacy_ok + "\n```"
out = tp.parse_model_output(fenced_legacy)
check("fenced legacy envelope unwrapped", out.kind == "final" and out.text == "jawaban lama")

legacy_tc = json.dumps({"status": "tool_calls", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_time", "arguments": {"tz": "UTC"}}}]})
out = tp.parse_model_output(legacy_tc, tools=TOOLS)
check("legacy tool_calls normalised (arguments -> JSON string)",
      out.kind == "tool_calls" and out.tool_calls[0]["function"]["arguments"] == '{"tz": "UTC"}')

user_json = '{"status": "success", "data": [1, 2, 3]}'
out = tp.parse_model_output(user_json)
check("user-requested JSON with status=success is NOT unwrapped", out.kind == "final" and out.text == user_json)

broken_success = '{"status":"success","choices":[{"message":{"content":"abc "oops" def'
out = tp.parse_model_output(broken_success)
check("broken legacy success never fails plain chat", out.kind == "final")

broken_tc = '{"status":"tool_calls","tool_calls":[{"function":{"name":"get_time","arguments":{"x":"a "b" c"}}'
out = tp.parse_model_output(broken_tc, tools=TOOLS)
check("broken legacy tool_calls is retryable", out.kind == "invalid" and out.legacy)

# legacy_validator hook is used to repair
def fake_validator(raw):
    return True, {"status": "success", "choices": [{"message": {"content": "repaired"}}]}, ""
out = tp.parse_model_output(broken_success, legacy_validator=fake_validator)
check("legacy_validator repair hook", out.kind == "final" and out.text == "repaired")

# --------------------------------------------------------------------------- #
# Prompt builders
# --------------------------------------------------------------------------- #
p = tp.build_wrapped_prompt("Apa kabar?")
check("plain wrapper keeps [SYSTEM CONTEXT]/[USER REQUEST]", p.startswith("[SYSTEM CONTEXT]") and "[USER REQUEST]\nApa kabar?" in p)
check("plain wrapper no longer demands JSON", "Do NOT wrap your answer in JSON" in p and '"status"' not in p and "@@PAF" not in p)

p = tp.build_wrapped_prompt("x", max_tokens=500, client_system="Jawab singkat.")
check("max_tokens + client system included", "roughly 500 tokens" in p and "Jawab singkat." in p)

p = tp.build_wrapped_prompt("cek waktu", tools=TOOLS)
check("tools wrapper lists functions + format", "get_time" in p and "@@PAF-CALL:" in p and "@@PAF-END@@" in p and "FOUR backticks" in p)
check("tools wrapper avoids the word 'tool' in instructions", "tool" not in p.split("AVAILABLE FUNCTIONS")[0].lower().replace("tools", ""))

p = tp.build_tool_result_prompt([{"tool_call_id": "call_1", "name": "get_time", "content": '{"t": "10:00"}'}], None, tools=TOOLS)
check("tool-result prompt", "[TOOL RESULT]" in p and "tool_call_id: call_1" in p and '{"t": "10:00"}' in p and "[USER REQUEST]" in p)

p = tp.build_tool_result_prompt([{"tool_call_id": "c", "name": "n", "content": "x"}], "lanjut", tools=None)
check("tool-result prompt without tools still explains call format", "@@PAF-CALL:" in p and p.rstrip().endswith("lanjut"))

c = tp.build_corrective_prompt("not terminated")
check("corrective prompt is short and states the format", "not terminated" in c and "@@PAF-END@@" in c and len(c) < 900)

print(f"\nALL {_passed} CHECKS PASSED")
