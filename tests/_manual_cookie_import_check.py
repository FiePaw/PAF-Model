"""Ad-hoc check for import_grok_cookies.load_and_convert_cookies() and
_diagnose() — parsing/conversion of Cookie-Editor exports (no browser needed).

Run: python3 tests/_manual_cookie_import_check.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from import_grok_cookies import load_and_convert_cookies, _diagnose
from scrapers.utils import cookie_editor_json_to_playwright


def _write(tmp: str, name: str, content) -> Path:
    p = Path(tmp) / name
    if isinstance(content, str):
        p.write_text(content, encoding="utf-8")
    else:
        import json
        p.write_text(json.dumps(content), encoding="utf-8")
    return p


def main():
    with tempfile.TemporaryDirectory() as tmp:
        # Case 1: valid Cookie-Editor export (list form) — using grok.com's
        # real essential cookie name "sso" (confirmed from a live export,
        # see GROK_BACKEND.md), analogous to ChatGPT's session-token check.
        good = [
            {"name": "sso", "value": "abc",
             "domain": ".grok.com", "path": "/", "secure": True,
             "httpOnly": True, "sameSite": "lax"},
            {"name": "sso-rw", "value": "xyz",
             "domain": ".grok.com", "path": "/", "secure": True,
             "httpOnly": True, "sameSite": "strict"},
        ]
        p1 = _write(tmp, "good.json", good)
        converted, raw = load_and_convert_cookies(p1)
        assert len(converted) == 2, converted
        assert converted[0]["sameSite"] in ("Lax", "lax"), converted[0]
        print("Case 1 (valid list export) -> converted 2 cookies   OK")

        # Case 2: wrapped {"cookies": [...]} form is unwrapped.
        p2 = _write(tmp, "wrapped.json", {"cookies": good})
        converted2, _ = load_and_convert_cookies(p2)
        assert len(converted2) == 2, converted2
        print("Case 2 (wrapped {'cookies': [...]}) -> unwrapped    OK")

        # Case 3: empty list -> clear ValueError.
        p3 = _write(tmp, "empty.json", [])
        try:
            load_and_convert_cookies(p3)
            raise AssertionError("expected ValueError for empty list")
        except ValueError as e:
            assert "non-empty JSON list" in str(e)
        print("Case 3 (empty list) -> clear ValueError             OK")

        # Case 4: wrong shape (header string) -> clear ValueError.
        p4 = _write(tmp, "header.txt", "cf_clearance=xyz; Path=/; Secure")
        try:
            load_and_convert_cookies(p4)
            raise AssertionError("expected ValueError for header-string export")
        except ValueError as e:
            assert "Export JSON" in str(e)
        print("Case 4 (header-string export) -> clear ValueError   OK")

        # Case 5: missing file -> FileNotFoundError with actionable message.
        try:
            load_and_convert_cookies(Path(tmp) / "nope.json")
            raise AssertionError("expected FileNotFoundError")
        except FileNotFoundError as e:
            assert "Cookie-Editor" in str(e)
        print("Case 5 (missing file) -> actionable error           OK")

        # Case 6: _diagnose hints (wrong domain + missing 'sso' cookie).
        hints = _diagnose([{"name": "foo", "domain": ".example.com"}])
        assert any("grok.com" in h for h in hints), hints
        assert any("sso" in h for h in hints), hints
        print("Case 6 (_diagnose hints for a bad export) -> OK")

    print("\nAll cookie-import checks passed.")


if __name__ == "__main__":
    main()
