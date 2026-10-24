"""
scrapers/tool_protocol.py — shared prompt/response protocol for ALL backends
(DeepSeek, Qwen, Grok).

Why this exists
---------------
The backends are browser automation, not native APIs, so two things must be
emulated through the prompt:

  1. A stable "API mode" system context ([SYSTEM CONTEXT] / [USER REQUEST]).
  2. OpenAI-style function ("tool") calling.

The old design forced EVERY reply into a JSON envelope, which made the model
responsible for JSON-escaping long answers (quotes, newlines, code) — the
main source of "invalid JSON" failures, retries and account rotations.

New design
----------
* Plain chat            → the model answers in plain text. Nothing to validate.
* Function calling      → the model answers in plain text UNLESS it needs the
                          client to run a function, in which case it emits a
                          marker block with RAW (unescaped) argument values:

      @@PAF-CALL:write_file@@
      @@PAF-ARG:path@@
      main.py
      @@PAF-ARG:content@@
      print("raw", 'text')
      @@PAF-END@@

  This module (not the model) builds the OpenAI ``tool_calls`` JSON with
  ``json.dumps()``, so escaping can never be wrong.

Marker design notes
-------------------
* Markers contain no ``<``/``>`` (a markdown renderer may treat angle
  brackets as HTML and hide them from ``innerText``) and no markdown-special
  characters (``_``, ``*``, ``#``, backticks).
* The model is asked to wrap the block in a 4-backtick fenced code block:
  rendered markdown preserves newlines/indentation inside ``<pre>``, while a
  plain paragraph would collapse them.  The parser works with or without the
  fence and ignores any UI chrome (language label, "Copy") before the block.
* Legacy JSON envelopes (``{"status": "success" | "tool_calls", ...}``) are
  still understood, so conversations that began under the old protocol keep
  working.

This module is pure Python (no browser / network deps) and fully unit-tested
offline (``tests/test_tool_protocol.py``).
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# Marker syntax
# --------------------------------------------------------------------------- #
CALL_PREFIX = "@@PAF-CALL:"
ARG_PREFIX = "@@PAF-ARG:"
END_MARKER = "@@PAF-END@@"

_CALL_RE = re.compile(r"@@PAF-CALL:([^@\s]+)@@")
_ARG_RE = re.compile(r"@@PAF-ARG:([^@\s]+)@@")
# Any marker-ish token (used to detect a malformed / truncated block).
_ANY_MARKER_RE = re.compile(r"@@PAF-(?:CALL:[^@\s]*|ARG:[^@\s]*|END)@@")

_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})[^\n]*$")

# Legacy envelope: {"status":"success"|"tool_calls"|"error", ...}
_LEGACY_START_RE = re.compile(r'^\{\s*"status"\s*:\s*"(success|tool_calls|error)"')

# In-session corrective retries when a function-call block is unusable.
MAX_CORRECTIVE_RETRIES = 2

KIND_FINAL = "final"
KIND_TOOL_CALLS = "tool_calls"
KIND_INVALID = "invalid"


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class ParsedOutput:
    """Outcome of parsing one raw model reply.

    kind:
      "final"       → ``text`` is the answer (plain text, as the model wrote it)
      "tool_calls"  → ``tool_calls`` is an OpenAI-format list
      "invalid"     → the reply tried to be a function call (or a legacy
                      envelope) but is unusable; ``error`` says why.  Callers
                      should send ``build_corrective_prompt(error)`` and retry.
    """
    kind: str
    text: str = ""
    tool_calls: list = field(default_factory=list)
    error: str = ""
    legacy: bool = False  # True when parsed from a legacy JSON envelope

    @property
    def ok(self) -> bool:
        return self.kind != KIND_INVALID


# --------------------------------------------------------------------------- #
# Prompt builders
# --------------------------------------------------------------------------- #
_BASE_RULE = (
    "You are operating in API mode. Answer the request directly in plain "
    "text (markdown is allowed). Do NOT wrap your answer in JSON."
)

_CALL_FORMAT = (
    "@@PAF-CALL:<function_name>@@\n"
    "@@PAF-ARG:<argument_name>@@\n"
    "<raw value of that argument>\n"
    "@@PAF-ARG:<another_argument_name>@@\n"
    "<raw value of that argument>\n"
    "@@PAF-END@@"
)

_CALL_EXAMPLE = (
    "@@PAF-CALL:example_function@@\n"
    "@@PAF-ARG:path@@\n"
    "notes/todo.txt\n"
    "@@PAF-ARG:content@@\n"
    'He said "hello" and left.\n'
    "Second line, with a \\ backslash and 'quotes'.\n"
    "@@PAF-END@@"
)


def tool_names(tools: Optional[list]) -> list[str]:
    """Function names declared in an OpenAI ``tools`` list."""
    names: list[str] = []
    for t in tools or []:
        fn = t.get("function", {}) if isinstance(t, dict) else {}
        n = fn.get("name")
        if n:
            names.append(n)
    return names


def _call_format_rules() -> list[str]:
    return [
        "WHEN YOU NEED THE CLIENT TO RUN A FUNCTION, reply with the block "
        "below and NOTHING else (no text before or after it). Put the whole "
        "block inside ONE fenced code block made with FOUR backticks, so it "
        "is displayed exactly as written:",
        "",
        "````",
        _CALL_FORMAT,
        "````",
        "",
        "Example of the format (illustration only — NOT an available function):",
        "",
        "````",
        _CALL_EXAMPLE,
        "````",
        "",
        "RULES FOR FUNCTION CALLS:",
        "- Write every argument value RAW, exactly as it should be used. Do "
        "NOT add quotes around it and do NOT escape quotes, backslashes or "
        "newlines. Multi-line values (code, files, long text) are fine.",
        "- Booleans: true / false. Numbers: digits only. For an object or "
        "array argument, write compact JSON.",
        "- A function without arguments is just the CALL line followed by "
        "the END line.",
        "- To call several functions, put several CALL…END blocks one after "
        "another inside the same fenced block.",
        "- If you do NOT need a function, answer normally in plain text and "
        "do not use any @@PAF markers.",
    ]


def build_system_context(
    *,
    tools: Optional[list] = None,
    expect_calls: bool = False,
    client_system: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Build the [SYSTEM CONTEXT] section.

    tools          → full function-calling context (function list + format).
    expect_calls   → no function list available (e.g. a tool-result turn where
                     the client did not resend ``tools``) but calls are still
                     allowed: emit only the call-format reminder.
    client_system  → optional client-supplied system prompt, appended verbatim.
    """
    parts: list[str] = ["[SYSTEM CONTEXT]"]

    if tools:
        names = tool_names(tools)
        parts += [
            _BASE_RULE,
            "",
            "You do NOT have any built-in capabilities (no web search, no "
            "code execution, no file access, no image generation). The "
            "client has its own external executor that can run the named "
            "functions below on your behalf.",
            "",
            "AVAILABLE FUNCTIONS (client-side, executed externally):",
            json.dumps(tools, ensure_ascii=False, indent=2),
            "",
            f"You may ONLY request these function names: {names}. Do NOT "
            "invent or use any other function name.",
            "",
        ]
        parts += _call_format_rules()
    elif expect_calls:
        parts += [
            _BASE_RULE,
            "",
            "The client may run functions you were given earlier in this "
            "conversation. Use only those function names.",
            "",
        ]
        parts += _call_format_rules()
    else:
        parts.append(_BASE_RULE)

    if max_tokens is not None:
        parts += ["", f"Keep your answer within roughly {int(max_tokens)} tokens."]

    if client_system and client_system.strip():
        parts += ["", client_system.strip()]

    return "\n".join(parts)


def build_wrapped_prompt(
    prompt: str,
    *,
    tools: Optional[list] = None,
    client_system: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """[SYSTEM CONTEXT] + [USER REQUEST] envelope for a normal user turn.

    The context is sent on EVERY turn (it is short for plain chat): clients may
    change ``tools`` between requests, and sessions that began under the old
    JSON protocol would otherwise never see the new rules.
    """
    ctx = build_system_context(
        tools=tools, client_system=client_system, max_tokens=max_tokens
    )
    return f"{ctx}\n\n[USER REQUEST]\n{prompt}"


def build_tool_result_prompt(
    tool_messages: list[dict],
    next_user_msg: Optional[str] = None,
    *,
    tools: Optional[list] = None,
    client_system: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Prompt that hands function results back to the model (CONTINUE turn).

    tool_messages: list of ``{tool_call_id, name, content}`` dicts.
    Function calls remain allowed on this turn (the model may chain calls).
    """
    ctx = build_system_context(
        tools=tools,
        expect_calls=True,
        client_system=client_system,
        max_tokens=max_tokens,
    )
    parts: list[str] = [ctx, ""]
    for tm in tool_messages:
        content = tm.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        parts += [
            "[TOOL RESULT]",
            f"tool_call_id: {tm.get('tool_call_id') or ''}",
            f"function: {tm.get('name') or ''}",
            "content:",
            content,
            "[END TOOL RESULT]",
            "",
        ]
    parts.append("[USER REQUEST]")
    parts.append(
        next_user_msg
        if next_user_msg
        else "Continue, using the function result(s) above. Answer the "
        "original request, or request another function if you still need one."
    )
    return "\n".join(parts)


def build_corrective_prompt(error: str = "") -> str:
    """Short in-session correction when a function-call block was unusable."""
    why = f" Problem: {error}." if error else ""
    return (
        "Your previous reply contained a function-call block that could not "
        f"be parsed.{why}\n"
        "Reply again with ONLY the corrected block, inside ONE fenced code "
        "block made with FOUR backticks, in exactly this format (raw values, "
        "no escaping):\n\n````\n" + _CALL_FORMAT + "\n````\n\n"
        "If you do not actually need a function, answer in plain text "
        "without any @@PAF markers."
    )


def corrective_prompt_for(out: "ParsedOutput") -> str:
    """Pick the right in-session correction for an INVALID ``ParsedOutput``."""
    err = out.error or ""
    if err == "empty response" or err.startswith("model returned an error"):
        return (
            "Your previous reply was empty or unusable. Please answer the "
            "last request again in plain text."
        )
    return build_corrective_prompt(err)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def _clean_value(raw: str) -> str:
    """Strip the single newline that separates a marker from its value, keep
    everything else (indentation, inner blank lines) untouched."""
    v = raw
    if v.startswith("\r\n"):
        v = v[2:]
    elif v.startswith("\n"):
        v = v[1:]
    v = v.rstrip("\r\n")
    # A closing code fence can sit right before the END marker if the model
    # closed the fence early; drop a trailing fence-only line.
    lines = v.split("\n")
    if lines and _FENCE_LINE_RE.match(lines[-1]) and len(lines) > 1:
        # only when the fence has no matching opener inside the value
        fence_count = sum(1 for ln in lines if _FENCE_LINE_RE.match(ln))
        if fence_count % 2 == 1:
            lines = lines[:-1]
            v = "\n".join(lines).rstrip("\r\n")
    return v


def _schema_props(tools: Optional[list], name: str) -> dict:
    for t in tools or []:
        fn = t.get("function", {}) if isinstance(t, dict) else {}
        if fn.get("name") == name:
            params = fn.get("parameters") or {}
            props = params.get("properties") if isinstance(params, dict) else None
            return props if isinstance(props, dict) else {}
    return {}


def _declared_type(prop: Any) -> Optional[str]:
    if not isinstance(prop, dict):
        return None
    t = prop.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    return t if isinstance(t, str) else None


def _coerce(value: str, declared: Optional[str]) -> Any:
    """Convert a raw string to the type the function schema declares."""
    if declared in (None, "string"):
        return value
    s = value.strip()
    try:
        if declared in ("integer", "number"):
            try:
                return int(s)
            except ValueError:
                f = float(s)
                if declared == "integer" and f.is_integer():
                    return int(f)
                return f
        if declared == "boolean":
            low = s.lower()
            if low in ("true", "yes", "1"):
                return True
            if low in ("false", "no", "0"):
                return False
            return value
        if declared in ("object", "array"):
            return json.loads(s)
        if declared == "null":
            return None
    except (ValueError, json.JSONDecodeError):
        return value
    return value


def _parse_call_blocks(
    text: str, tools: Optional[list]
) -> "tuple[list, str]":
    """Parse every @@PAF-CALL … @@PAF-END@@ block. Returns (calls, error)."""
    calls: list = []
    pos = 0
    allowed = tool_names(tools)

    while True:
        m = _CALL_RE.search(text, pos)
        if not m:
            break
        fn_name = m.group(1)
        end_idx = text.find(END_MARKER, m.end())
        if end_idx == -1:
            return [], (
                f"function-call block for '{fn_name}' is not terminated "
                f"(missing {END_MARKER})"
            )
        body = text[m.end():end_idx]
        pos = end_idx + len(END_MARKER)

        if allowed and fn_name not in allowed:
            return [], f"unknown function '{fn_name}'; allowed: {allowed}"

        props = _schema_props(tools, fn_name)
        args: dict = {}
        arg_matches = list(_ARG_RE.finditer(body))
        if arg_matches:
            for i, am in enumerate(arg_matches):
                start = am.end()
                stop = arg_matches[i + 1].start() if i + 1 < len(arg_matches) else len(body)
                value = _clean_value(body[start:stop])
                args[am.group(1)] = _coerce(value, _declared_type(props.get(am.group(1))))
        else:
            # Tolerate a bare JSON object as the body: {"city": "Jakarta"}
            stripped = _strip_fence_lines(body).strip()
            if stripped:
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    return [], (
                        f"function-call block for '{fn_name}' has a body "
                        "but no @@PAF-ARG:<name>@@ markers"
                    )
                if not isinstance(obj, dict):
                    return [], f"arguments for '{fn_name}' are not an object"
                args = obj

        calls.append(
            {
                "id": _new_call_id(),
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        )

    if not calls:
        return [], "no complete function-call block found"
    return calls, ""


def _strip_fence_lines(text: str) -> str:
    return "\n".join(
        ln for ln in text.split("\n") if not _FENCE_LINE_RE.match(ln)
    )


def _strip_json_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _normalize_legacy_tool_calls(tcs: list) -> list:
    """Legacy envelope → OpenAI shape (arguments as JSON string, ids set)."""
    out = []
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        args = fn.get("arguments")
        if isinstance(args, dict):
            fn["arguments"] = json.dumps(args, ensure_ascii=False)
        elif args is None:
            fn["arguments"] = "{}"
        tc.setdefault("type", "function")
        if not tc.get("id"):
            tc["id"] = _new_call_id()
        out.append(tc)
    return out


def _parse_legacy(
    text: str,
    tools_enabled: bool,
    legacy_validator: Optional[Callable[[str], "tuple[bool, Optional[dict], str]"]],
) -> Optional[ParsedOutput]:
    """Understand the old JSON envelope. Returns None if ``text`` is not one."""
    candidate = _strip_json_fences(text)
    if not _LEGACY_START_RE.match(candidate):
        return None

    data: Optional[dict] = None
    err = ""
    try:
        parsed = json.loads(candidate)
        data = parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError as exc:
        err = str(exc)
        if legacy_validator is not None:
            try:
                ok, parsed_dict, verr = legacy_validator(text)
            except Exception as exc2:  # noqa: BLE001
                ok, parsed_dict, verr = False, None, str(exc2)
            if ok and isinstance(parsed_dict, dict):
                data = parsed_dict
            else:
                err = verr or err

    if data is None:
        # Broken legacy JSON. A broken tool_calls envelope is retryable; a
        # broken "success" envelope is returned as-is rather than failing chat.
        if tools_enabled and '"tool_calls"' in candidate:
            return ParsedOutput(
                KIND_INVALID,
                error=f"legacy JSON envelope could not be parsed: {err}",
                legacy=True,
            )
        return ParsedOutput(KIND_FINAL, text=text.strip(), legacy=True)

    status = data.get("status")
    if status == "tool_calls":
        tcs = data.get("tool_calls")
        if isinstance(tcs, list) and tcs:
            norm = _normalize_legacy_tool_calls(tcs)
            if norm:
                return ParsedOutput(KIND_TOOL_CALLS, tool_calls=norm, legacy=True)
        return ParsedOutput(
            KIND_INVALID, error="status=tool_calls but tool_calls missing/empty",
            legacy=True,
        )
    if status == "success":
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return ParsedOutput(KIND_FINAL, text=msg["content"], legacy=True)
        # Looks like a user-requested JSON that merely has status=success.
        return None
    if status == "error":
        err_obj = data.get("error") or {}
        msg = err_obj.get("message") if isinstance(err_obj, dict) else None
        return ParsedOutput(
            KIND_INVALID,
            error=msg or "model returned an error envelope",
            legacy=True,
        )
    return None


def parse_model_output(
    raw: str,
    *,
    tools: Optional[list] = None,
    expect_calls: bool = False,
    legacy_validator: Optional[Callable[[str], "tuple[bool, Optional[dict], str]"]] = None,
) -> ParsedOutput:
    """Classify a raw model reply.

    tools / expect_calls  → whether @@PAF-CALL blocks are interpreted at all.
                            With neither, markers in the text are just text.
    legacy_validator      → optional ``raw -> (ok, dict, err)`` used to repair a
                            broken legacy JSON envelope (DeepSeek/Qwen helpers).
    """
    if raw is None or not raw.strip():
        return ParsedOutput(KIND_INVALID, error="empty response")

    text = raw.strip()
    calls_enabled = bool(tools) or expect_calls

    if calls_enabled:
        if _ANY_MARKER_RE.search(text):
            calls, err = _parse_call_blocks(text, tools)
            if calls:
                return ParsedOutput(KIND_TOOL_CALLS, tool_calls=calls)
            return ParsedOutput(KIND_INVALID, error=err)

    legacy = _parse_legacy(text, calls_enabled, legacy_validator)
    if legacy is not None:
        return legacy

    return ParsedOutput(KIND_FINAL, text=text)
