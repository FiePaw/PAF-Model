"""Ad-hoc check for login_chatgpt.confirm_close() -- verifies it always
waits for an explicit answer and never decides to close on its own.

Run: python3 tests/_manual_confirm_close_check.py
"""
import asyncio
import builtins
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from login_chatgpt import confirm_close


async def _case(answer_or_exc, default_yes, expected, label):
    if isinstance(answer_or_exc, BaseException):
        def fake_input(prompt=""):
            raise answer_or_exc
    else:
        def fake_input(prompt=""):
            return answer_or_exc

    with patch.object(builtins, "input", fake_input):
        result = await confirm_close("test prompt.", default_yes=default_yes)
    assert result == expected, f"{label}: expected {expected}, got {result}"
    print(f"{label}: OK -> {result}")


async def main():
    await _case("y", True, True, "explicit 'y'")
    await _case("yes", True, True, "explicit 'yes'")
    await _case("n", True, False, "explicit 'n' overrides default_yes=True")
    await _case("no", False, False, "explicit 'no'")
    await _case("", True, True, "empty (Enter) with default_yes=True")
    await _case("", False, False, "empty (Enter) with default_yes=False")
    await _case(EOFError(), True, False, "EOFError (Ctrl+D) never auto-closes")
    await _case(KeyboardInterrupt(), True, False, "KeyboardInterrupt (Ctrl+C) never auto-closes")
    print("\nAll confirm_close() checks passed -- it never closes without an explicit answer.")


if __name__ == "__main__":
    asyncio.run(main())
